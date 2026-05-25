"""Wave 2 cluster analysis + ship-or-chip decision.

Reads wave 2 probe results, clusters by verdict+fingerprint, and prints
a triage report with explicit action recommendations for each cluster:
  - SHIP_INLINE: small, localized regex/parser tweak (<2 hours, no chip)
  - SPAWN_CHIP: new adapter or larger investigation
  - DEFER: known cohort already-owned or operator-data-gap
  - FOLLOWUP: needs Chrome MCP probe before decisions
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

PROBE_DIR = Path(__file__).parent / "artifacts" / "probe"
OUT = Path(__file__).parent / "artifacts" / "clusters"

# Verdict → suggested action
def _action(verdict: str, fps: list[str], count: int, tier_dist: dict) -> str:
    if verdict in ("BLOCKED_HTTP_403", "BLOCKED_HTTP_429", "BLOCKED_HTTP_503"):
        return f"DEFER (fetcher escalation 59b9102) · {count}p"
    if verdict == "FETCH_ERROR":
        return f"DEFER (DNS / hard fetch fail) · {count}p"
    if verdict == "ANTIBOT_WALL":
        return f"DEFER (anti-bot wall — needs separate fix) · {count}p"
    if verdict.startswith("HAS_UNIT_MARKERS_AT"):
        return f"SHIP_INLINE: drill into {verdict.split('AT_')[1]} · {count}p"
    if verdict == "FLOORPLAN_INDEX_NO_UNITS":
        return f"FOLLOWUP: Chrome MCP click-through · {count}p"
    if verdict == "HAS_GENERIC_API":
        return f"SHIP_INLINE: generic JSON tier-1 · {count}p"
    if verdict == "WORDPRESS_BACKED":
        return f"FLAG operator-data-gap (unless Elementor body has rent text)  · {count}p"
    if verdict == "JS_REFERENCES_API":
        return f"FOLLOWUP: Chrome MCP network panel · {count}p"
    if verdict == "NO_FINGERPRINT_NO_API":
        return f"FOLLOWUP: rendered DOM probe · {count}p"
    if verdict.startswith("FINGERPRINT_"):
        plat = verdict.split("FINGERPRINT_")[1].rsplit("_", 1)[0]
        return f"DEBUG existing {plat} adapter · {count}p"
    return f"TRIAGE · {count}p"


def main(cohort: str = "n_full_zero_w2") -> None:
    path = PROBE_DIR / f"{cohort}_results.jsonl"
    OUT.mkdir(parents=True, exist_ok=True)

    records = []
    with path.open() as f:
        for line in f:
            try:
                records.append(json.loads(line))
            except Exception:
                continue

    groups: dict[tuple[str, tuple], list[dict]] = defaultdict(list)
    for r in records:
        v = r.get("verdict", "?")
        fps = tuple(sorted(r.get("steps", {}).get("5_fingerprints", []) or []))
        groups[(v, fps)].append(r)

    sorted_groups = sorted(groups.items(), key=lambda kv: -len(kv[1]))

    lines = [f"# Wave 2 cluster decisions — {cohort} (160 props)", ""]
    lines.append("| # | Verdict | Fingerprint | Tier (top) | Count | Action |")
    lines.append("|---|---|---|---|---:|---|")
    for i, ((verdict, fps), members) in enumerate(sorted_groups, 1):
        fp_s = ",".join(fps) if fps else "—"
        tier_dist = Counter(m.get("tier_observed") for m in members)
        top_tier = tier_dist.most_common(1)[0][0] if tier_dist else "?"
        action = _action(verdict, list(fps), len(members), dict(tier_dist))
        lines.append(f"| {i} | {verdict} | {fp_s} | {top_tier} | {len(members)} | {action} |")

    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("# Per-cluster sample props")
    for i, ((verdict, fps), members) in enumerate(sorted_groups, 1):
        if len(members) < 2 and not verdict.startswith("HAS_UNIT_MARKERS"):
            continue
        lines.append(f"\n## #{i} — {verdict} · fp={','.join(fps) or '—'} · {len(members)} props")
        tier_dist = Counter(m.get("tier_observed") for m in members)
        lines.append(f"Tier mix: {dict(tier_dist)}")
        for m in members[:5]:
            lines.append(f"  - `{m.get('pid')}` [{m.get('name', '')}]({m.get('url', '')})")
        # JS hints
        js_urls = []
        for m in members:
            js_urls.extend(m.get("steps", {}).get("7_js_urls", []) or [])
        if js_urls:
            hint_keys = ("api", "/api/", "graphql", "wp-json", "admin-ajax")
            hints = sorted({u for u in js_urls if any(k in u.lower() for k in hint_keys)})[:6]
            if hints:
                lines.append("JS URL hints:")
                for h in hints:
                    lines.append(f"  - `{h}`")

    (OUT / f"{cohort}_decisions.md").write_text("\n".join(lines))
    print(f"Wrote {OUT}/{cohort}_decisions.md ({len(sorted_groups)} clusters across {len(records)} props)")

    # Print top-12 to stdout
    print("\nTop 12 clusters:")
    for i, ((verdict, fps), members) in enumerate(sorted_groups[:12], 1):
        fp_s = ",".join(fps) if fps else "—"
        tier_dist = Counter(m.get("tier_observed") for m in members)
        top_tier = tier_dist.most_common(1)[0][0] if tier_dist else "?"
        action = _action(verdict, list(fps), len(members), dict(tier_dist))
        print(f"  #{i}  [{len(members):>2}p]  {verdict:<40}  fp={fp_s:<35}  {action}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "n_full_zero_w2")
