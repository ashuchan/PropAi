"""Sqft=-1 probe cluster analysis.

Reads sqft_neg1_results.jsonl, groups by (verdict, tier), counts
SQFT_FOUND vs SQFT_TRULY_ABSENT. For SQFT_FOUND cases, reports which
path carried the sqft so the adapter can be extended.
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

PROBE_DIR = Path(__file__).parent / "artifacts" / "probe"
OUT = Path(__file__).parent / "artifacts" / "clusters"


def main() -> None:
    path = PROBE_DIR / "sqft_neg1_results.jsonl"
    OUT.mkdir(parents=True, exist_ok=True)
    records = []
    with path.open() as f:
        for line in f:
            try:
                records.append(json.loads(line))
            except Exception:
                continue

    total = len(records)
    by_verdict = Counter(r.get("verdict") for r in records)
    by_tier_verdict = defaultdict(Counter)
    for r in records:
        by_tier_verdict[r.get("tier_observed") or "?"][r.get("verdict") or "?"] += 1

    print(f"Total sqft=-1 props probed: {total}")
    print("\n--- Verdict counts ---")
    for v, c in by_verdict.most_common():
        print(f"  {c:>3}  {v}")

    print("\n--- Per-tier verdict mix ---")
    for tier, vc in sorted(by_tier_verdict.items(), key=lambda kv: -sum(kv[1].values())):
        total_t = sum(vc.values())
        found = sum(v for k, v in vc.items() if k.startswith("SQFT_FOUND"))
        absent = vc.get("SQFT_TRULY_ABSENT", 0)
        pct_found = (100 * found / total_t) if total_t else 0
        print(f"  {tier:<45} total={total_t:>2}  found={found:>2} ({pct_found:>4.0f}%)  absent={absent:>2}  other={total_t - found - absent}")

    # Detailed SQFT_FOUND samples
    print("\n--- SQFT_FOUND samples (adapter misses — fixable) ---")
    found_records = [r for r in records if r.get("verdict", "").startswith("SQFT_FOUND")]
    for r in found_records[:20]:
        path_hits = {p: info["sqft_hits"]["count"] for p, info in r.get("paths", {}).items() if info.get("sqft_hits", {}).get("count", 0) >= 3}
        path_sample = {p: info["sqft_hits"]["samples"][:3] for p, info in r.get("paths", {}).items() if info.get("sqft_hits", {}).get("count", 0) >= 3}
        print(f"  pid={r['pid']:>6} tier={r.get('tier_observed', '?'):<40} {r.get('url', '')}")
        print(f"      paths_with_sqft: {path_hits}")
        print(f"      sample sqft values: {path_sample}")

    # Operator-data-gap samples (true absences)
    print("\n--- SQFT_TRULY_ABSENT samples (operator-data-gap — flag, don't ship) ---")
    absent_records = [r for r in records if r.get("verdict") == "SQFT_TRULY_ABSENT"]
    for r in absent_records[:5]:
        print(f"  pid={r['pid']:>6} tier={r.get('tier_observed', '?'):<40} {r.get('url', '')}")

    # Write markdown report
    lines = [f"# sqft=-1 cluster report ({total} props)", ""]
    lines.append("## Top-level breakdown")
    lines.append("| Verdict | Count | % |")
    lines.append("|---|---:|---:|")
    for v, c in by_verdict.most_common():
        lines.append(f"| {v} | {c} | {100*c/total:.0f}% |")
    lines.append("")
    lines.append("## Per-tier extraction-miss rate")
    lines.append("| Tier | Total | Sqft Found (adapter miss) | Truly Absent (operator-gap) | Other |")
    lines.append("|---|---:|---:|---:|---:|")
    for tier, vc in sorted(by_tier_verdict.items(), key=lambda kv: -sum(kv[1].values())):
        total_t = sum(vc.values())
        found = sum(v for k, v in vc.items() if k.startswith("SQFT_FOUND"))
        absent = vc.get("SQFT_TRULY_ABSENT", 0)
        other = total_t - found - absent
        pct_found = (100 * found / total_t) if total_t else 0
        lines.append(f"| {tier} | {total_t} | {found} ({pct_found:.0f}%) | {absent} | {other} |")
    lines.append("")
    lines.append("## SQFT_FOUND props (adapter misses — fixable)")
    for r in found_records:
        path_hits = [p for p, info in r.get("paths", {}).items() if info.get("sqft_hits", {}).get("count", 0) >= 3]
        samples = []
        for p in path_hits:
            samples.extend(r["paths"][p]["sqft_hits"]["samples"][:3])
        samples = sorted(set(samples))[:6]
        lines.append(f"- `{r['pid']}` [{r.get('name', '')}]({r.get('url', '')}) — tier={r.get('tier_observed')} — sqft at: `{', '.join(path_hits)}` — values: {samples}")
    (OUT / "sqft_neg1_clusters.md").write_text("\n".join(lines))
    print(f"\nWrote {OUT}/sqft_neg1_clusters.md")


if __name__ == "__main__":
    main()
