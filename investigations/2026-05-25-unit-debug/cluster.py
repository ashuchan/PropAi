"""Cluster probe results into actionable signatures.

Reads:  artifacts/probe/{cohort}_results.jsonl
Writes: artifacts/clusters/{cohort}_clusters.md  — human-readable cluster report
        artifacts/clusters/{cohort}_clusters.json — machine-readable groups

A "cluster" = ≥3 properties with the same verdict + same fingerprint set.
Smaller groups go into a "tail" bucket for manual review.

For each cluster, the report emits:
  - cohort tag, verdict, fingerprint
  - prop count, sample URLs (up to 5)
  - tier_observed distribution
  - suggested next action (ship adapter / extend regex / wait for chip / defer)
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

PROBE_DIR = Path(__file__).parent / "artifacts" / "probe"
CLUSTER_DIR = Path(__file__).parent / "artifacts" / "clusters"

# Verdicts that have known chips/fixes already underway — flag, don't re-investigate
KNOWN_OWNED = {
    "ANTIBOT_WALL": "Sucuri/DataDome — separate fetch-layer escalation",
    "BLOCKED_HTTP_403": "Fetcher escalation already shipped (commit 59b9102)",
    "BLOCKED_HTTP_503": "Rate limit — fetcher backoff",
}


def _suggested_action(verdict: str, fps: list[str], count: int) -> str:
    if verdict in KNOWN_OWNED:
        return f"DEFER — {KNOWN_OWNED[verdict]}"
    if verdict.startswith("HAS_UNIT_MARKERS_AT"):
        return f"SHIP — drill into the matched path ({verdict.split('AT_')[1]})"
    if verdict == "FLOORPLAN_INDEX_NO_UNITS":
        if count >= 5:
            return "SHIP — index-page parser + per-plan drill (likely new adapter)"
        return "PROBE DEEPER — Chrome MCP click-through to find unit pages"
    if verdict == "HAS_GENERIC_API":
        return "SHIP — JSON API tier-1 adapter"
    if verdict == "WORDPRESS_BACKED":
        return "PROBE — check wp-json/wp/v2 for custom unit endpoints"
    if verdict == "JS_REFERENCES_API":
        return "PROBE — Chrome MCP network panel to capture XHR"
    if verdict.startswith("FINGERPRINT_"):
        platform = verdict.split("FINGERPRINT_")[1].split("_")[0]
        return f"DEBUG existing {platform} adapter — fingerprint matched but no units"
    if verdict == "NO_FINGERPRINT_NO_API":
        return "PROBE — Chrome MCP rendered DOM; possible client-only React/Vue widget"
    return "TRIAGE"


def cluster_cohort(name: str) -> None:
    path = PROBE_DIR / f"{name}_results.jsonl"
    if not path.exists():
        print(f"⚠ no results for {name} (skipping)")
        return
    CLUSTER_DIR.mkdir(parents=True, exist_ok=True)

    records = []
    with path.open() as f:
        for line in f:
            try:
                records.append(json.loads(line))
            except Exception:
                continue
    print(f"[{name}] {len(records)} probe results")

    # Group by (verdict, fingerprint tuple)
    groups: dict[tuple[str, tuple], list[dict]] = defaultdict(list)
    for r in records:
        v = r.get("verdict", "?")
        fps = tuple(sorted(r.get("steps", {}).get("5_fingerprints", []) or []))
        groups[(v, fps)].append(r)

    # Sort by size desc
    sorted_groups = sorted(groups.items(), key=lambda kv: -len(kv[1]))

    # Build report
    lines = [f"# Cluster report — {name} cohort", "", f"Total probed: {len(records)}", ""]
    lines.append("| Verdict | Fingerprint | # props | Action |")
    lines.append("|---|---|---:|---|")
    cluster_blocks = []
    for (verdict, fps), members in sorted_groups:
        fp_s = ",".join(fps) if fps else "—"
        action = _suggested_action(verdict, list(fps), len(members))
        lines.append(f"| {verdict} | {fp_s} | {len(members)} | {action} |")

        # Cluster detail block
        if len(members) >= 1:
            block = [f"\n## {verdict} · fingerprint={fp_s} · {len(members)} props", ""]
            block.append(f"**Action:** {action}")
            block.append("")
            tier_dist = Counter(m.get("tier_observed") for m in members)
            block.append("**Tier distribution:**")
            for t, c in tier_dist.most_common():
                block.append(f"  - {t}: {c}")
            block.append("")
            block.append("**Sample props (up to 5):**")
            for m in members[:5]:
                pid = m.get("pid")
                name_ = m.get("name") or ""
                url = m.get("url") or ""
                block.append(f"  - `{pid}` [{name_}]({url})")
            # Step-1 status mix
            block.append("")
            status_mix = Counter(m.get("steps", {}).get("1_landing", {}).get("status") for m in members)
            block.append(f"**Landing status mix:** {dict(status_mix)}")
            # JS URL hints
            js_urls_all = []
            for m in members:
                js_urls_all.extend(m.get("steps", {}).get("7_js_urls", []) or [])
            if js_urls_all:
                hint_keys = ("api", "/api/", "graphql", "wp-json", "admin-ajax")
                hints = sorted({u for u in js_urls_all if any(k in u.lower() for k in hint_keys)})[:8]
                if hints:
                    block.append(f"**JS URL hints:**")
                    for h in hints:
                        block.append(f"  - `{h}`")
            cluster_blocks.append("\n".join(block))

    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("# Cluster details")
    out_md = "\n".join(lines + cluster_blocks)
    (CLUSTER_DIR / f"{name}_clusters.md").write_text(out_md)

    # Machine-readable
    out_json = []
    for (verdict, fps), members in sorted_groups:
        out_json.append({
            "verdict": verdict,
            "fingerprint": list(fps),
            "count": len(members),
            "action": _suggested_action(verdict, list(fps), len(members)),
            "pids": [m.get("pid") for m in members],
            "tier_dist": dict(Counter(m.get("tier_observed") for m in members)),
            "samples": [{"pid": m.get("pid"), "name": m.get("name"), "url": m.get("url")} for m in members[:5]],
        })
    (CLUSTER_DIR / f"{name}_clusters.json").write_text(json.dumps(out_json, indent=2, default=str))
    print(f"[{name}] → {CLUSTER_DIR}/{name}_clusters.md  ({len(sorted_groups)} clusters)")


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] != "both":
        cluster_cohort(sys.argv[1])
    else:
        cluster_cohort("n_full_zero")
        cluster_cohort("low_strict")


if __name__ == "__main__":
    main()
