"""Canary residue analyzer.

Cross-references a canary's per-property output (one `properties.json` per
shard) against a manual-validation spreadsheet to surface the actionable
gap: properties where the operator publishes real unit-level data
(`truth_unit=Y`) but the canary produced no rent.

Outputs:
  1. Per-cluster summary table (tier × outcome) of the residue
  2. Per-prop CSV with pid, name, url, tier, verdict, truth_url, errors —
     ready to feed into Chrome MCP probes or hand to a teammate
  3. JSON manifest of the residue cohort for downstream chip fixtures

Usage:
    python ma_poc/scripts/diagnostics/residue_analyzer.py \\
        --canary-glob '/tmp/c612_results/shard_*/properties.json' \\
        --validation-xlsx '/path/to/scrapping validation.xlsx' \\
        --out-dir /tmp/residue_2026-05-28

The script never connects to the network, never re-fetches, never
recomputes verdicts — it's pure observation over already-written outputs.
Safe to run repeatedly.
"""
from __future__ import annotations

import argparse
import collections
import csv
import glob
import json
import sys
from pathlib import Path
from typing import Any


# ─── inputs ─────────────────────────────────────────────────────────


def load_canary_props(canary_glob: str) -> dict[str, dict[str, Any]]:
    """Load one properties.json per shard, keyed by string ``apartment_id``."""
    out: dict[str, dict[str, Any]] = {}
    paths = sorted(glob.glob(canary_glob))
    if not paths:
        print(f"WARN: no files matched {canary_glob!r}", file=sys.stderr)
    for f in paths:
        try:
            for p in json.load(open(f)):
                pid = str(
                    p.get("apartment_id") or p.get("apartmentid") or p.get("property_id") or ""
                )
                if pid:
                    out[pid] = p
        except Exception as e:
            print(f"WARN: failed to read {f}: {e}", file=sys.stderr)
    return out


def load_validation(xlsx_path: str) -> list[dict[str, Any]]:
    """Load all sheets of the validation xlsx into a list of dicts.

    Expects columns: apartment_id, name, website, extraction_tier,
    url where unit data is, has floor plan (Y/N), Has Unit (Y/N).
    Sheet names are coerced into a single concatenated list.
    """
    try:
        import pandas as pd
    except ImportError:
        print("FATAL: pandas required", file=sys.stderr)
        sys.exit(1)
    all_sheets = pd.read_excel(xlsx_path, sheet_name=None)
    rows: list[dict[str, Any]] = []
    for sheet_name, df in all_sheets.items():
        if "apartment_id" not in df.columns:
            continue
        for _, r in df.iterrows():
            rows.append({
                "_sheet": sheet_name,
                "apartment_id": str(r["apartment_id"]).strip()
                    if not pd.isna(r["apartment_id"]) else "",
                "name": r.get("name"),
                "website": r.get("website"),
                "extraction_tier_canary": r.get("extraction_tier"),
                "truth_url": r.get("url where unit data is")
                    if not pd.isna(r.get("url where unit data is")) else None,
                "truth_fp": str(r.get("has floor plan (Y/N)") or "").strip().upper()
                    if not pd.isna(r.get("has floor plan (Y/N)")) else "",
                "truth_unit": str(r.get("Has Unit (Y/N)") or "").strip().upper()
                    if not pd.isna(r.get("Has Unit (Y/N)")) else "",
            })
    return rows


# ─── analysis ───────────────────────────────────────────────────────


def _canary_extraction_signal(p: dict[str, Any]) -> dict[str, Any]:
    """Pull the canary's extraction state for one property.

    Honors both the modern jugnu shape (``_meta.verdict`` +
    ``_extract_result.tier_used``) and the legacy daily_runner shape
    (``_meta.scrape_tier_used``). Falls back to "no extraction" defaults
    so a missing or partially-serialized property doesn't crash the
    analyzer.
    """
    meta = p.get("_meta", {}) or {}
    er = p.get("_extract_result", {}) or {}
    units = p.get("units") or []
    n_rent = sum(
        1 for u in units
        if u.get("rent_low") or u.get("market_rent_low") or u.get("rent")
    )
    return {
        "verdict": meta.get("verdict", "") or "",
        "verdict_reason": meta.get("verdict_reason", "") or "",
        "tier": (
            er.get("tier_used")
            or meta.get("scrape_tier_used")
            or "UNKNOWN"
        ),
        "n_units": len(units),
        "n_rent": n_rent,
        "partial_recovery": bool(meta.get("partial_recovery")),
        "errors_first": (
            str((meta.get("scrape_errors") or [None])[0])[:120]
            if meta.get("scrape_errors") else ""
        ),
    }


def _classify_residue(row: dict[str, Any], sig: dict[str, Any]) -> str:
    """Bucket the residue prop into one of 6 actionable clusters.

    Membership is mutually exclusive — first matching rule wins.
    """
    tier = sig["tier"]
    # 1. Timeout artifact (verdict-on-partial-recovery chip handles or fetcher fix needed)
    if "per_property_timeout" in sig["errors_first"]:
        return "A_TIMEOUT_600s"
    # 2. Fetcher short-circuit (TRANSIENT / BOT_BLOCKED / DEAD_URL)
    if "no_body" in tier:
        return "B_FETCH_SHORT_CIRCUIT"
    # 3. LLM tier (won't fire in LLM-off prod = effectively unhandled)
    if "TIER_4_LLM" in tier:
        return "C_LLM_TIER_PROD_UNHANDLED"
    # 4. PLAN_LEVEL drill failure
    if tier.endswith("_PLAN_LEVEL") or "_PLAN_LEVEL" in tier:
        return "D_PLAN_LEVEL_DRILL_MISS"
    # 5. API NO_RESPONSE (adapter-specific fallback needed)
    if "_NO_RESPONSE" in tier:
        return "E_API_NO_RESPONSE"
    # 6. Long-tail — every other case
    return "F_LONG_TAIL"


def analyze(
    canary: dict[str, dict[str, Any]],
    validation: list[dict[str, Any]],
) -> dict[str, Any]:
    """Cross-reference canary × validation and produce residue stats.

    Returns a dict with:
      - overall: high-level counts
      - residue_rows: list of dicts (the props needing a fix)
      - cluster_summary: {cluster_label: count}
      - cluster_tier_breakdown: {(cluster, tier): count}
    """
    matched = 0
    residue: list[dict[str, Any]] = []
    success_with_rent = 0
    success_plan_only = 0
    truth_unit_total = 0

    for row in validation:
        pid = row["apartment_id"]
        if not pid or pid not in canary:
            continue
        matched += 1
        if row["truth_unit"] != "Y":
            continue
        truth_unit_total += 1
        sig = _canary_extraction_signal(canary[pid])
        if sig["n_rent"] > 0:
            success_with_rent += 1
            continue
        if sig["n_units"] > 0:
            success_plan_only += 1
            # Plan-level-only IS residue when truth says unit-level exists
        cluster = _classify_residue(row, sig)
        residue.append({
            "pid": pid,
            "name": row["name"],
            "website": row["website"],
            "truth_url": row["truth_url"],
            "tier": sig["tier"],
            "verdict": sig["verdict"],
            "verdict_reason": sig["verdict_reason"],
            "n_units": sig["n_units"],
            "n_rent": sig["n_rent"],
            "partial_recovery": sig["partial_recovery"],
            "errors_first": sig["errors_first"],
            "cluster": cluster,
        })

    cluster_summary: collections.Counter = collections.Counter(
        r["cluster"] for r in residue
    )
    cluster_tier: collections.Counter = collections.Counter(
        (r["cluster"], r["tier"]) for r in residue
    )

    return {
        "overall": {
            "validation_rows_total": len(validation),
            "validation_rows_matched_in_canary": matched,
            "truth_unit_Y_total": truth_unit_total,
            "succeeded_with_rent": success_with_rent,
            "succeeded_plan_only": success_plan_only,
            "residue_total": len(residue),
            "recall_with_rent_pct": (
                round(100 * success_with_rent / truth_unit_total, 1)
                if truth_unit_total else 0
            ),
        },
        "cluster_summary": dict(cluster_summary.most_common()),
        "cluster_tier_breakdown": {
            f"{c}|{t}": n for (c, t), n in cluster_tier.most_common()
        },
        "residue_rows": residue,
    }


# ─── output ─────────────────────────────────────────────────────────


_CLUSTER_LABELS = {
    "A_TIMEOUT_600s": "Per-property 600s timeout (verdict-on-partial chip helps; fetcher route-block + curl_cffi salvage are real fix)",
    "B_FETCH_SHORT_CIRCUIT": "Fetcher TRANSIENT/BOT_BLOCKED before extraction (needs curl_cffi salvage hook)",
    "C_LLM_TIER_PROD_UNHANDLED": "LLM tier extracted in canary but LLM is off in prod — these would be zero-extraction failures (need earlier deterministic tier)",
    "D_PLAN_LEVEL_DRILL_MISS": "PLAN_LEVEL emitted; per-unit drill silently failed (AppFolio vanity, Entrata PP)",
    "E_API_NO_RESPONSE": "PMS API returned empty — adapter-specific fallback needed",
    "F_LONG_TAIL": "One-off cases needing per-prop investigation",
}


def write_outputs(result: dict[str, Any], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    # 1. summary.md
    md = ["# Canary residue analysis", ""]
    o = result["overall"]
    md.extend([
        "## Overall",
        "",
        f"- Validation rows total: {o['validation_rows_total']}",
        f"- Matched in canary: {o['validation_rows_matched_in_canary']}",
        f"- truth_unit=Y total (denominator): {o['truth_unit_Y_total']}",
        f"- Succeeded with rent: {o['succeeded_with_rent']} ({o['recall_with_rent_pct']}%)",
        f"- Succeeded plan-only (counted as residue — operator has unit data we missed): {o['succeeded_plan_only']}",
        f"- **Residue (truth=Y, no rent extracted): {o['residue_total']}**",
        "",
        "## Residue clusters",
        "",
        "| Cluster | Count | Description |",
        "|---|---:|---|",
    ])
    for cluster, n in result["cluster_summary"].items():
        md.append(f"| `{cluster}` | {n} | {_CLUSTER_LABELS.get(cluster, '')} |")
    md.extend(["", "## Tier breakdown within each cluster", ""])
    md.append("| Cluster | Tier | Count |")
    md.append("|---|---|---:|")
    for key, n in result["cluster_tier_breakdown"].items():
        cluster, tier = key.split("|", 1)
        md.append(f"| `{cluster}` | `{tier}` | {n} |")
    (out_dir / "summary.md").write_text("\n".join(md), encoding="utf-8")

    # 2. residue.csv — one row per residue prop, ready to inspect / probe
    csv_path = out_dir / "residue.csv"
    if result["residue_rows"]:
        keys = list(result["residue_rows"][0].keys())
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=keys)
            w.writeheader()
            w.writerows(result["residue_rows"])

    # 3. residue.json — full result for downstream tooling
    (out_dir / "residue.json").write_text(
        json.dumps(result, indent=2, default=str), encoding="utf-8"
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--canary-glob", required=True,
                    help="glob pattern for shard properties.json files")
    ap.add_argument("--validation-xlsx", required=True,
                    help="path to manual-validation xlsx")
    ap.add_argument("--out-dir", required=True,
                    help="directory to write summary.md + residue.csv + residue.json")
    args = ap.parse_args()
    canary = load_canary_props(args.canary_glob)
    val = load_validation(args.validation_xlsx)
    result = analyze(canary, val)
    write_outputs(result, Path(args.out_dir))
    print(f"residue: {result['overall']['residue_total']}")
    print(f"recall_with_rent: {result['overall']['recall_with_rent_pct']}%")
    print(f"wrote summary + CSV + JSON to {args.out_dir}")


if __name__ == "__main__":
    main()
