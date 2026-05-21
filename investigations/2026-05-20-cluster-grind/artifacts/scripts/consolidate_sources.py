"""Consolidate property data from three sources into one best-quality dataset.

Sources (highest-priority first):
  1. Current canary (per-shard ``properties.json`` files on disk or GCS-mirrored)
  2. Prior canary (xlsx export, e.g. ``scraped_units_feature_2026-05-19.xlsx``)
  3. Scraping report / main prod (xlsx export, e.g. ``scraped_units_2026-05-20.xlsx``)

Per-property selection (quality-gated, tier-ranked):
  * Quality bar (configurable): ``strict`` (rent + beds + real-uid) or
    ``rent_sqft`` (rent + sqft, no UID restriction).
  * For each source that clears the quality bar, compute its
    extraction-tier score (deterministic Tier 1 = 1, JSON-LD = 2,
    generic DOM = 3, LLM = 4, vision = 5). Lower score = more
    reliable.
  * Pick the source with the lowest tier score. So if scraping
    report has Tier 1 deterministic data but the current canary
    only has Tier 4 LLM extraction, the scraping report wins — even
    though it's later in the source preference order.
  * Tie-break (same tier score): current_canary > prior_canary >
    scraping_report (preference order).
  * If NO source clears the quality bar: pick the source with the
    most-data rows (best-effort low-quality fallback) and tag
    ``Provenance = "<source>_low_quality"`` so consumers can filter
    these out for high-confidence analytics.

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


# ── per-property quality + tier assessment ─────────────────────────────────


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


# Tier score: lower = more reliable. The extraction pipeline emits a wide
# variety of ``Tier Used`` strings (PMS-specific suffixes, sub-tiers,
# merged tiers). This map captures the broad reliability bands:
#
#   1.0  — deterministic Tier-1 API/DOM extraction (PMS-specific or
#          merged from real adapters). Highest-confidence data.
#   1.5  — Tier 1.5 embedded JSON (e.g. ``__NEXT_DATA__``). Still
#          deterministic, still SSR-pulled.
#   2.0  — Tier 2 JSON-LD (Apartment/Offer schema).
#   3.0  — Tier 3 generic DOM scan.
#   4.0  — Tier 4 LLM extraction (less reliable, model-dependent).
#   5.0  — Tier 5 vision LLM.
#   9.0  — empty exits / failure labels (``_EMPTY``, ``_NO_RESPONSE``,
#          ``no_body_short_circuit``, etc.). Treated as "no real tier".
#   99.0 — null / missing / unknown.
_TIER_SCORE_RULES: list[tuple[re.Pattern[str], float]] = [
    (re.compile(r"^(generic:)?no[_-]?body[_-]?short[_-]?circuit$", re.IGNORECASE), 9.0),
    (re.compile(r"_(EMPTY|NO[_-]URN|NO[_-]RESPONSE|SHAPE[_-]REJECTED|PARSE[_-]FAILED|API[_-]ERROR|NO[_-]PLAN[_-]LINKS?)$", re.IGNORECASE), 9.0),
    (re.compile(r"^(NOT[_-]ENCORESKYLINE|ENCORESKYLINE[_-]NO[_-]PLAN|SYNDICATION[_-]ONLY)", re.IGNORECASE), 9.0),
    (re.compile(r"TIER[_-]?5[_-]?VISION", re.IGNORECASE), 5.0),
    (re.compile(r"TIER[_-]?4[_-]?LLM", re.IGNORECASE), 4.0),
    (re.compile(r"TIER[_-]?3[_-]?DOM", re.IGNORECASE), 3.0),
    (re.compile(r"TIER[_-]?2[_-]?JSONLD", re.IGNORECASE), 2.0),
    (re.compile(r"TIER[_-]?1[_-]?5[_-]?EMBEDDED", re.IGNORECASE), 1.5),
    (re.compile(r"TIER[_-]?MERGED", re.IGNORECASE), 1.0),
    (re.compile(r"TIER[_-]?1[_-](API|DOM)", re.IGNORECASE), 1.0),
    (re.compile(r"^TIER[_-]?1[_-]?PROFILE", re.IGNORECASE), 1.0),
    (re.compile(r"^TIER[_-]?1$", re.IGNORECASE), 1.0),
]


def _tier_score(tier_used: Any) -> float:
    """Map a ``Tier Used`` string to a reliability score (lower = better)."""
    if tier_used is None or (isinstance(tier_used, float) and tier_used != tier_used):
        return 99.0
    s = str(tier_used).strip()
    if not s or s.lower() == "nan":
        return 99.0
    for pat, score in _TIER_SCORE_RULES:
        if pat.search(s):
            return score
    return 99.0


def _best_tier_score(prop_rows: pd.DataFrame) -> float:
    """Return the BEST (lowest) tier score across all rows of a property."""
    if prop_rows.empty:
        return 99.0
    if "Tier Used" not in prop_rows.columns:
        return 99.0
    return min(_tier_score(t) for t in prop_rows["Tier Used"]) if len(prop_rows) else 99.0


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
        # When NO source clears the quality bar, we fall back to the source
        # with the most-data rows but tag it as low-quality so analytics
        # can filter these out. Broken out by which source supplied the
        # row for traceability.
        "current_canary_low_quality": 0,
        "prior_canary_low_quality": 0,
        "scraping_report_low_quality": 0,
        "no_source_anywhere": 0,
    }
    # Tier-trumping audit: count cases where the picked source isn't the
    # default preference order winner because a lower-tier (more reliable)
    # source elsewhere outranked it. Surfaces how often the new rule fires.
    tier_overrides: dict[str, int] = {
        "scraping_report_over_current_canary": 0,
        "scraping_report_over_prior_canary":   0,
        "prior_canary_over_current_canary":    0,
    }

    def _stamp(df: pd.DataFrame, prov: str) -> pd.DataFrame:
        """Return a copy of *df* with Provenance rewritten to *prov*."""
        out = df.copy()
        out["Provenance"] = prov
        return out

    # Preference order for tie-breaking on equal tier scores.
    _PREF_ORDER = {"current_canary": 0, "prior_canary": 1, "scraping_report": 2}

    for cid in all_ids:
        cur_g = cur_idx.get(cid)
        pri_g = pri_idx.get(cid)
        scr_g = scr_idx.get(cid)

        # Build (source, group, clears_bar, tier_score) tuples
        candidates_quality: list[tuple[str, pd.DataFrame, float]] = []
        for name, g in (("current_canary", cur_g), ("prior_canary", pri_g),
                        ("scraping_report", scr_g)):
            if g is not None and _property_clears_bar(g, quality_bar):
                candidates_quality.append((name, g, _best_tier_score(g)))

        if candidates_quality:
            # Sort: best tier first (lower score), then preference order.
            candidates_quality.sort(key=lambda t: (t[2], _PREF_ORDER[t[0]]))
            picked_name, picked_g, picked_tier = candidates_quality[0]
            # Audit override cases: when we picked something OTHER than
            # the default preference order winner BECAUSE of tier score.
            default_winner = min(candidates_quality, key=lambda t: _PREF_ORDER[t[0]])
            if default_winner[0] != picked_name:
                key = f"{picked_name}_over_{default_winner[0]}"
                tier_overrides[key] = tier_overrides.get(key, 0) + 1
            chosen_rows.append(picked_g)
            stats[picked_name] += 1
        else:
            # No source cleared the quality bar — pick the source with
            # the most rows as a low-quality fallback (still better than
            # dropping the property entirely, but flag the provenance
            # so consumers can exclude these from high-confidence cuts).
            candidates: list[tuple[int, str, pd.DataFrame]] = []
            if cur_g is not None: candidates.append((len(cur_g), "current_canary", cur_g))
            if pri_g is not None: candidates.append((len(pri_g), "prior_canary", pri_g))
            if scr_g is not None: candidates.append((len(scr_g), "scraping_report", scr_g))
            if not candidates:
                stats["no_source_anywhere"] += 1
                continue
            # Largest-row-count wins — that's the source with the most
            # extractor effort even if it didn't clear quality.
            candidates.sort(key=lambda t: -t[0])
            _rows, src_name, picked = candidates[0]
            chosen_rows.append(_stamp(picked, f"{src_name}_low_quality"))
            stats[f"{src_name}_low_quality"] += 1

    # Pass override audit out via the stats dict for the caller to print.
    stats["_tier_overrides"] = tier_overrides  # type: ignore[assignment]

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

    overrides = stats.pop("_tier_overrides", {})
    total = sum(stats.values())
    print("\n=== Source-selection counts ===")
    for src, c in stats.items():
        pct = (100 * c / total) if total else 0
        print(f"  {src:<32} {c:>5} ({pct:.1f}%)")
    print(f"  total properties               {total:>5}")
    if overrides and any(overrides.values()):
        print("\n=== Tier-trump overrides (lower-tier source beat default preference) ===")
        for k, v in overrides.items():
            if v > 0:
                print(f"  {k:<48} {v:>5}")

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
