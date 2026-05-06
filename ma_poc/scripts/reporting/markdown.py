"""
scripts/reporting/markdown.py
==============================
Markdown report writer for the daily runner pipeline.

Extracted from scripts/daily_runner.py (lines 467-517).
"""
from __future__ import annotations

from pathlib import Path


def _write_markdown_report(path: Path, report: dict) -> None:
    lines: list[str] = []
    lines.append(f"# Daily Run Report — {report['run_date']}")
    lines.append("")
    lines.append(f"- **Started:** {report['started_at']}")
    lines.append(f"- **Finished:** {report['finished_at']}")
    lines.append(f"- **Duration:** {report['duration_s']:.1f}s")
    lines.append(f"- **Exit status:** {report['exit_status']}")
    lines.append("")
    lines.append("## Totals")
    for k, v in report["totals"].items():
        lines.append(f"- {k}: **{v}**")
    lines.append("")
    lines.append("## Identity")
    ids = report["identity"]
    lines.append(f"- Resolved: **{ids['resolved']}** / Unresolved: **{ids['unresolved']}**")
    lines.append(f"- Hard duplicates (same canonical_id): **{len(ids['hard_duplicates'])}**")
    lines.append(f"- Soft duplicates (same address, different id): **{len(ids['soft_duplicates'])}**")
    lines.append("- By source: " + ", ".join(f"{k}={v}" for k, v in ids["by_source"].items()))
    lines.append("")
    lines.append("## Issues")
    lines.append(f"- Total: **{report['issues']['total']}**")
    for sev, n in report["issues"]["by_severity"].items():
        lines.append(f"  - {sev}: {n}")
    lines.append("- Top codes:")
    for code, n in list(report["issues"]["by_code"].items())[:20]:
        lines.append(f"  - `{code}`: {n}")
    lines.append("")
    lines.append("## State diff vs yesterday")
    sd = report["state_diff"]
    lines.append(f"- New properties: **{len(sd['new_properties'])}**")
    lines.append(f"- Disappeared properties: **{len(sd['disappeared_properties'])}**")
    lines.append(f"- Carry-forward used: **{sd['carry_forward_count']}** properties")
    lines.append(
        f"- Unit totals — extracted: {sd['units_extracted']}, "
        f"new: {sd['units_new']}, updated: {sd['units_updated']}, "
        f"unchanged: {sd['units_unchanged']}, disappeared: {sd['units_disappeared']}, "
        f"carried-forward: {sd['units_carried_forward']}"
    )
    lines.append("")
    if report["failed_properties"]:
        lines.append("## Failed properties (first 50)")
        lines.append("| Row | Canonical ID | Reason |")
        lines.append("|---|---|---|")
        for fp in report["failed_properties"][:50]:
            reason = (fp.get("reason") or "").replace("|", "\\|")[:120]
            lines.append(f"| {fp['row_index']} | `{fp.get('canonical_id') or 'unresolved'}` | {reason} |")
        lines.append("")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
