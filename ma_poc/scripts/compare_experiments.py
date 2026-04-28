#!/usr/bin/env python3
"""Aggregate per-property results across multiple experiment runs.

Reads every per_property/*.json under each experiment dir passed on the
command line and produces a single side-by-side comparison: cost,
calls, latency, units, tier_used. Useful when several seeded batches
have run and you want one summary instead of N summary.json files.

Usage:
  python scripts/compare_experiments.py data/experiments/universe_vs_legacy_*/
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def _read_property(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("dirs", nargs="+", help="experiment directories to combine")
    p.add_argument("--out", default=None, help="optional path to write a combined .md report")
    args = p.parse_args()

    rows: list[dict[str, Any]] = []
    for d_str in args.dirs:
        d = Path(d_str)
        per = d / "per_property"
        if not per.is_dir():
            continue
        for f in sorted(per.glob("*.json")):
            rec = _read_property(f)
            s = rec.get("sample") or {}
            leg = rec.get("legacy") or {}
            uni = rec.get("universe") or {}
            ud = rec.get("universe_debug") or {}
            rows.append(
                {
                    "experiment": d.name,
                    "canonical_id": s.get("canonical_id", ""),
                    "name": s.get("property_name", ""),
                    "url": s.get("url", ""),
                    "leg_units": len(leg.get("units") or []),
                    "leg_tier": leg.get("tier_used", ""),
                    "leg_cost": float(leg.get("llm_cost_usd") or 0.0),
                    "leg_calls": int(leg.get("llm_calls") or 0),
                    "leg_ms": int(leg.get("wall_clock_ms") or 0),
                    "uni_units": len(uni.get("units") or []),
                    "uni_tier": uni.get("tier_used", ""),
                    "uni_cost": float(uni.get("llm_cost_usd") or 0.0),
                    "uni_calls": int(uni.get("llm_calls") or 0),
                    "uni_ms": int(uni.get("wall_clock_ms") or 0),
                    "uni_stop": ud.get("stop_reason", ""),
                    "uni_apis_explored": int(ud.get("explored_apis") or 0),
                    "uni_links_explored": int(ud.get("explored_links") or 0),
                }
            )

    if not rows:
        print("no per_property records found", file=sys.stderr)
        return 1

    # Print per-property table.
    header = (
        f"{'EXP':<35} {'ID':<10} {'NAME':<30} "
        f"{'leg-u':>5} {'leg-$':>7} {'leg-ms':>7} "
        f"{'uni-u':>5} {'uni-$':>7} {'uni-ms':>7} "
        f"{'$Δ':>7} {'apis/links':>11} {'stop':<25}"
    )
    print(header)
    print("-" * len(header))
    for r in rows:
        cost_delta = r["uni_cost"] - r["leg_cost"]
        print(
            f"{r['experiment'][:34]:<35} {r['canonical_id'][:9]:<10} {r['name'][:29]:<30} "
            f"{r['leg_units']:>5} ${r['leg_cost']:>5.4f} {r['leg_ms']:>7} "
            f"{r['uni_units']:>5} ${r['uni_cost']:>5.4f} {r['uni_ms']:>7} "
            f"${cost_delta:+>5.4f} {r['uni_apis_explored']:>4}/{r['uni_links_explored']:<5} {r['uni_stop'][:24]:<25}"
        )

    # Aggregates.
    n = len(rows)
    leg_total_cost = sum(r["leg_cost"] for r in rows)
    uni_total_cost = sum(r["uni_cost"] for r in rows)
    leg_total_calls = sum(r["leg_calls"] for r in rows)
    uni_total_calls = sum(r["uni_calls"] for r in rows)
    leg_total_units = sum(r["leg_units"] for r in rows)
    uni_total_units = sum(r["uni_units"] for r in rows)
    leg_avg_ms = sum(r["leg_ms"] for r in rows) / n
    uni_avg_ms = sum(r["uni_ms"] for r in rows) / n

    wins = sum(1 for r in rows if r["uni_units"] > r["leg_units"])
    regressions = sum(1 for r in rows if r["uni_units"] < r["leg_units"])
    ties = n - wins - regressions

    summary = [
        "",
        "=" * 80,
        f"AGGREGATE OVER {n} PROPERTIES",
        "=" * 80,
        f"  legacy   total cost: ${leg_total_cost:.4f}   total calls: {leg_total_calls}   total units: {leg_total_units}   avg latency: {leg_avg_ms/1000:.1f}s",
        f"  universe total cost: ${uni_total_cost:.4f}   total calls: {uni_total_calls}   total units: {uni_total_units}   avg latency: {uni_avg_ms/1000:.1f}s",
        f"  cost delta: ${uni_total_cost - leg_total_cost:+.4f}   ({(uni_total_cost / max(leg_total_cost, 0.0001)):.2f}× legacy)",
        f"  units delta: {uni_total_units - leg_total_units:+d}",
        f"  wins (universe > legacy units): {wins}",
        f"  regressions (universe < legacy units): {regressions}",
        f"  ties: {ties}",
        f"  per-property avg cost — legacy: ${leg_total_cost/n:.4f}   universe: ${uni_total_cost/n:.4f}",
    ]
    print("\n".join(summary))

    if args.out:
        out_path = Path(args.out)
        md = ["# Combined experiment comparison\n"]
        md.append(f"- N properties: **{n}**")
        md.append(f"- Legacy total cost: **${leg_total_cost:.4f}**, total units: **{leg_total_units}**, avg latency: **{leg_avg_ms/1000:.1f}s**")
        md.append(f"- Universe total cost: **${uni_total_cost:.4f}**, total units: **{uni_total_units}**, avg latency: **{uni_avg_ms/1000:.1f}s**")
        md.append(f"- Cost delta: **${uni_total_cost - leg_total_cost:+.4f}** ({(uni_total_cost / max(leg_total_cost, 0.0001)):.2f}× legacy)")
        md.append(f"- Wins / regressions / ties: **{wins} / {regressions} / {ties}**")
        md.append("")
        md.append("| Property | Legacy units | Legacy cost | Universe units | Universe cost | $ delta | Stop reason |")
        md.append("|---|---:|---:|---:|---:|---:|---|")
        for r in rows:
            md.append(
                f"| {r['canonical_id']} {r['name'][:30]} | {r['leg_units']} | ${r['leg_cost']:.4f} | "
                f"{r['uni_units']} | ${r['uni_cost']:.4f} | ${r['uni_cost']-r['leg_cost']:+.4f} | {r['uni_stop']} |"
            )
        out_path.write_text("\n".join(md), encoding="utf-8")
        print(f"\nwrote {out_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
