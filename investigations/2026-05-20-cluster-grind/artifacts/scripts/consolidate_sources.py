"""Consolidate property data from three sources into one best-quality dataset.

Sources (highest-priority first):
  1. Current canary (per-shard ``properties.json`` files on disk or GCS-mirrored)
  2. Prior canary (xlsx export, e.g. ``scraped_units_feature_2026-05-19.xlsx``)
  3. Scraping report / main prod (xlsx export, e.g. ``scraped_units_2026-05-20.xlsx``)

Per-property selection:
  * Quality bar (configurable): ``strict`` (rent + beds + real-uid) or
    ``rent_sqft`` (rent + sqft, no UID restriction).
  * If current canary clears the bar for that ``Canonical ID``, use it.
  * Else if prior canary clears the bar, use it.
  * Else fall back to scraping report (always — it's the last-resort
    floor regardless of its own quality).

Output:
  * Single ``.xlsx`` with the same 19-column schema as the input
    xlsxs PLUS a ``Provenance`` column (= ``current_canary`` /
    ``prior_canary`` / ``scraping_report``) so downstream readers know
    which source each row came from.

Usage:
    python consolidate_sources.py \\
        --current-canary-shards /tmp/njhnm_shards \\
        --prior-canary /Users/ankur/Downloads/scraped_units_feature_2026-05-19.xlsx \\
        --scraping-report /Users/ankur/Downloads/scraped_units_2026-05-20.xlsx \\
        --output /tmp/consolidated_2026-05-20.xlsx \\
        --quality-bar rent_sqft

Designed to be re-runnable as more canary shards finish.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Optional

import pandas as pd

# ── synthetic-UID detection (matches prior canary's strict-quality gate) ────
_INFERRED_RE = re.compile(r"^inferred_", re.IGNORECASE)
_HEX32_RE = re.compile(r"^[0-9a-f]{32}$", re.IGNORECASE)
_ULID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_APT_LEAK_RE = re.compile(r"^apartment[:\s]+#?", re.IGNORECASE)
_LONG_NUMERIC_RE = re.compile(r"^\d{7,}$")


def _is_synthetic_uid(uid: Any) -> bool:
    s = str(uid or "")
    if not s or s == "nan":
        return True
    return bool(
        _INFERRED_RE.match(s)
        or _HEX32_RE.match(s)
        or _ULID_RE.match(s)
        or _UUID_RE.match(s)
        or _APT_LEAK_RE.match(s)
        or _LONG_NUMERIC_RE.match(s)
    )


# ── per-unit quality predicates ─────────────────────────────────────────────


def _unit_has_rent_sqft(unit: dict[str, Any]) -> bool:
    """Looser bar: rent > 0 AND sqft > 0. UID and beds optional."""
    rent = unit.get("Rent Low") if "Rent Low" in unit else unit.get("rent_low")
    sqft = unit.get("Area (sqft)") if "Area (sqft)" in unit else unit.get("area")
    try:
        rent_n = float(rent) if rent not in (None, "", "nan") else 0
    except (TypeError, ValueError):
        rent_n = 0
    try:
        sqft_n = float(sqft) if sqft not in (None, "", "nan") else 0
    except (TypeError, ValueError):
        # sqft may be like "750" string
        try:
            sqft_n = float(re.sub(r"[^\d.]", "", str(sqft or ""))) or 0
        except Exception:
            sqft_n = 0
    return rent_n > 0 and sqft_n > 0


def _unit_is_strict(unit: dict[str, Any]) -> bool:
    """Strict bar: rent > 0 + beds present + non-synthetic uid."""
    uid_key = "Unit ID" if "Unit ID" in unit else "unit_id"
    rent_key = "Rent Low" if "Rent Low" in unit else "rent_low"
    beds_key = "Beds" if "Beds" in unit else "beds"
    rent = unit.get(rent_key)
    beds = unit.get(beds_key)
    try:
        rent_n = float(rent) if rent not in (None, "", "nan") else 0
    except (TypeError, ValueError):
        rent_n = 0
    if rent_n <= 0:
        return False
    if beds is None or beds == "" or str(beds) == "nan":
        return False
    if _is_synthetic_uid(unit.get(uid_key)):
        return False
    return True


# ── source loaders → DataFrame with standard schema ─────────────────────────

_STANDARD_COLS = [
    "Canonical ID", "Property Name", "City", "State", "ZIP",
    "Mgmt Company", "Website", "Verdict", "Tier Used",
    "Unit ID", "Floor Plan", "Beds", "Baths", "Area (sqft)",
    "Rent Low", "Rent High", "Available Date", "Lease Term", "Concessions",
]


def _load_xlsx(path: Path, provenance: str) -> pd.DataFrame:
    """Load a scraped_units xlsx and tag with provenance."""
    if not path.exists():
        print(f"  ⚠ {provenance}: file not found at {path}; using empty frame.")
        return pd.DataFrame(columns=_STANDARD_COLS + ["Provenance"])
    sheet = pd.ExcelFile(path).sheet_names[0]
    df = pd.read_excel(path, sheet_name=sheet)
    # Ensure all standard cols exist (some sheets may add/drop)
    for c in _STANDARD_COLS:
        if c not in df.columns:
            df[c] = None
    df = df[_STANDARD_COLS].copy()
    df["Provenance"] = provenance
    return df


def _load_canary_shards(shards_dir: Path, provenance: str) -> pd.DataFrame:
    """Load per-shard JSON files + flatten to one-row-per-unit (or one-row-per-prop
    when units empty). Maps internal field names → xlsx-schema column names."""
    if not shards_dir.exists():
        print(f"  ⚠ {provenance}: shards dir not found at {shards_dir}; empty frame.")
        return pd.DataFrame(columns=_STANDARD_COLS + ["Provenance"])

    rows: list[dict[str, Any]] = []
    for shard_path in sorted(shards_dir.glob("shard_*.json")):
        try:
            with open(shard_path) as f:
                data = json.load(f)
        except Exception as exc:
            print(f"  ⚠ {provenance}: failed to parse {shard_path.name}: {exc}")
            continue
        for prop in data:
            base = {
                "Canonical ID": prop.get("apartment_id"),
                "Property Name": prop.get("proj_name"),
                "City": prop.get("city"),
                "State": prop.get("state"),
                "ZIP": prop.get("zip_code"),
                "Mgmt Company": prop.get("pmc"),
                "Website": prop.get("website"),
                "Verdict": (prop.get("_meta") or {}).get("verdict"),
                "Tier Used": (prop.get("_extract_result") or {}).get("tier_used"),
                "Concessions": prop.get("concessions"),
                "Provenance": provenance,
            }
            units = prop.get("units") or []
            if not units:
                # Empty-units placeholder row so the property is still in the cohort
                rows.append({**base, "Unit ID": None, "Floor Plan": None,
                             "Beds": None, "Baths": None, "Area (sqft)": None,
                             "Rent Low": None, "Rent High": None,
                             "Available Date": None, "Lease Term": None})
                continue
            for u in units:
                rows.append({**base,
                    "Unit ID":         u.get("unit_id"),
                    "Floor Plan":      u.get("floor_plan_name"),
                    "Beds":            u.get("beds"),
                    "Baths":           u.get("baths"),
                    "Area (sqft)":     u.get("area"),
                    "Rent Low":        u.get("rent_low"),
                    "Rent High":       u.get("rent_high"),
                    "Available Date":  u.get("available_date"),
                    "Lease Term":      u.get("lease_term"),
                })
    return pd.DataFrame(rows, columns=_STANDARD_COLS + ["Provenance"])


# ── per-property quality assessment ─────────────────────────────────────────


def _property_clears_bar(prop_rows: pd.DataFrame, bar: str) -> bool:
    """Returns True iff at least one row in *prop_rows* satisfies the bar.

    *bar* is one of ``strict`` / ``rent_sqft``.
    """
    if prop_rows.empty:
        return False
    predicate = _unit_is_strict if bar == "strict" else _unit_has_rent_sqft
    for _, row in prop_rows.iterrows():
        if predicate(row.to_dict()):
            return True
    return False


# ── consolidation ──────────────────────────────────────────────────────────


def consolidate(
    current_canary: pd.DataFrame,
    prior_canary: pd.DataFrame,
    scraping_report: pd.DataFrame,
    quality_bar: str = "rent_sqft",
) -> tuple[pd.DataFrame, dict[str, int]]:
    """Pick the best-quality source per Canonical ID and return the merged DF.

    Returns (df, stats) where stats tracks the picked-source counts.
    """
    # All canonical IDs across the three sources (use scraping_report as
    # spine since it's the broadest fleet-level snapshot)
    all_ids = pd.Index(
        pd.concat([
            current_canary["Canonical ID"],
            prior_canary["Canonical ID"],
            scraping_report["Canonical ID"],
        ]).dropna().astype(str).unique()
    )

    # Pre-group each source by str(Canonical ID) for fast lookup
    def _group(df: pd.DataFrame) -> dict[str, pd.DataFrame]:
        if df.empty:
            return {}
        d2 = df.copy()
        d2["_cid"] = d2["Canonical ID"].astype(str)
        return {cid: g.drop(columns=["_cid"]) for cid, g in d2.groupby("_cid")}

    cur_idx = _group(current_canary)
    pri_idx = _group(prior_canary)
    scr_idx = _group(scraping_report)

    chosen_rows: list[pd.DataFrame] = []
    stats = {
        "current_canary": 0,
        "prior_canary": 0,
        "scraping_report": 0,
        "no_source": 0,
    }

    for cid in all_ids:
        cur_g = cur_idx.get(cid)
        pri_g = pri_idx.get(cid)
        scr_g = scr_idx.get(cid)

        if cur_g is not None and _property_clears_bar(cur_g, quality_bar):
            chosen_rows.append(cur_g)
            stats["current_canary"] += 1
        elif pri_g is not None and _property_clears_bar(pri_g, quality_bar):
            chosen_rows.append(pri_g)
            stats["prior_canary"] += 1
        elif scr_g is not None:
            chosen_rows.append(scr_g)
            stats["scraping_report"] += 1
        else:
            # No source has this id — should be rare, but bookkeep.
            stats["no_source"] += 1

    merged = pd.concat(chosen_rows, ignore_index=True) if chosen_rows else pd.DataFrame(columns=_STANDARD_COLS + ["Provenance"])
    return merged, stats


# ── CLI ────────────────────────────────────────────────────────────────────


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--current-canary-shards", required=True, type=Path,
                   help="Directory of shard_*.json files from the current canary")
    p.add_argument("--prior-canary", required=True, type=Path,
                   help="Path to prior canary scraped_units xlsx")
    p.add_argument("--scraping-report", required=True, type=Path,
                   help="Path to main/prod scraped_units xlsx (the fallback)")
    p.add_argument("--output", required=True, type=Path, help="Output xlsx path")
    p.add_argument("--quality-bar", choices=["strict", "rent_sqft"],
                   default="rent_sqft",
                   help='Per-property quality bar for source selection. '
                        '"strict" = rent + beds + non-synthetic UID. '
                        '"rent_sqft" = rent + sqft (looser). Default: rent_sqft.')
    args = p.parse_args(argv)

    print("Loading sources …")
    cur = _load_canary_shards(args.current_canary_shards, "current_canary")
    pri = _load_xlsx(args.prior_canary, "prior_canary")
    scr = _load_xlsx(args.scraping_report, "scraping_report")
    print(f"  current_canary  : {len(cur):>7} rows / {cur['Canonical ID'].nunique():>5} props")
    print(f"  prior_canary    : {len(pri):>7} rows / {pri['Canonical ID'].nunique():>5} props")
    print(f"  scraping_report : {len(scr):>7} rows / {scr['Canonical ID'].nunique():>5} props")

    print(f"\nConsolidating (quality bar: {args.quality_bar}) …")
    merged, stats = consolidate(cur, pri, scr, args.quality_bar)

    total = sum(stats.values())
    print("\n=== Source-selection counts ===")
    for src, c in stats.items():
        pct = (100 * c / total) if total else 0
        print(f"  {src:<20} {c:>5} ({pct:.1f}%)")
    print(f"  total properties     {total:>5}")

    print(f"\n=== Output ===")
    print(f"  rows: {len(merged)}")
    print(f"  unique properties: {merged['Canonical ID'].nunique()}")
    print(f"  writing → {args.output}")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(args.output, engine="openpyxl") as xw:
        merged.to_excel(xw, sheet_name="units", index=False)
        # Summary sheet
        summary = pd.DataFrame({
            "source": list(stats.keys()),
            "properties_picked": list(stats.values()),
        })
        summary.to_excel(xw, sheet_name="summary", index=False)

    print(f"  ✓ wrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
