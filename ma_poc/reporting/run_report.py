"""Run-level report builder — produces markdown + JSON summary.

Consumes cost ledger, SLO watcher, and all property results to produce
the run-level summary report.
"""
from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def build(
    properties: list[dict[str, Any]],
    run_dir: Path,
    run_date: str,
    cost_rollup: dict[str, float] | None = None,
    slo_violations: list[Any] | None = None,
) -> dict[str, Any]:
    """Build the run-level report.

    Args:
        properties: List of property result dicts.
        run_dir: Path to today's run directory.
        run_date: Date string for this run.
        cost_rollup: Cost totals from CostLedger.total().
        slo_violations: SLO violations from slo_watcher.check().

    Returns:
        Report dict with summary metrics.
    """
    total = len(properties)
    tier_counts: Counter[str] = Counter()
    failed = 0
    carry_forward = 0

    for p in properties:
        meta = p.get("_meta", {})
        tier = meta.get("scrape_tier_used") or "UNKNOWN"
        tier_counts[tier] += 1
        if "FAIL" in str(tier).upper():
            failed += 1
        if meta.get("carry_forward_used"):
            carry_forward += 1

    success_rate = ((total - failed) / total * 100) if total > 0 else 0

    report = {
        "run_date": run_date,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "totals": {
            "properties": total,
            "succeeded": total - failed,
            "failed": failed,
            "carry_forward": carry_forward,
            "success_rate_pct": round(success_rate, 2),
        },
        "tier_distribution": dict(tier_counts.most_common()),
        "cost": cost_rollup or {},
        "slo_violations": [
            {"name": v.name, "threshold": v.threshold, "observed": v.observed}
            for v in (slo_violations or [])
        ],
    }

    # Write JSON
    json_path = run_dir / "report.json"
    json_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    # Write Markdown
    md_path = run_dir / "report.md"
    md_lines = [
        f"# Run Report — {run_date}",
        "",
        "## Totals",
        "",
        f"- Properties: {total}",
        f"- Succeeded: {total - failed}",
        f"- Failed: {failed}",
        f"- Carry-forward: {carry_forward}",
        f"- Success rate: {success_rate:.1f}%",
        "",
        "## Tier Distribution",
        "",
        "| Tier | Count |",
        "|---|---|",
    ]
    for tier, count in tier_counts.most_common():
        md_lines.append(f"| {tier} | {count} |")

    md_lines.extend([
        "",
        "## Cost",
        "",
    ])
    for cat, amount in (cost_rollup or {}).items():
        md_lines.append(f"- {cat}: ${amount:.4f}")

    md_lines.extend([
        "",
        "## SLO Status",
        "",
    ])
    if slo_violations:
        for v in slo_violations:
            md_lines.append(f"- **{v.name}**: observed={v.observed:.4f}, threshold={v.threshold}")
    else:
        md_lines.append("All SLOs green.")

    md_lines.append("")
    md_path.write_text("\n".join(md_lines), encoding="utf-8")

    log.info("Run report written to %s", run_dir)
    return report
