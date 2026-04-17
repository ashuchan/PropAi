"""Per-property markdown report generator.

Produces a structured markdown report for a single property scrape result,
including verdict, detection info, pipeline steps, changes since last run,
issues, and LLM call transcripts.
"""

from __future__ import annotations

from typing import Any

# Unreachable-error substrings used to distinguish FAILED_UNREACHABLE from FAILED_NO_DATA.
_UNREACHABLE_SIGNALS = (
    "ERR_SSL",
    "ERR_NAME_NOT_RESOLVED",
    "DNS",
    "timeout",
    "ETIMEDOUT",
    "ECONNREFUSED",
    "ENOTFOUND",
    "net::ERR_",
)


def _determine_verdict(scrape_result: dict[str, Any]) -> str:
    """Return one of four verdict literals based on the scrape result.

    Args:
        scrape_result: The full scrape result dict for a property.

    Returns:
        One of SUCCESS, FAILED_UNREACHABLE, FAILED_NO_DATA, CARRY_FORWARD.
    """
    if scrape_result.get("_carry_forward"):
        return "CARRY_FORWARD"

    units = scrape_result.get("units") or []
    if len(units) > 0:
        return "SUCCESS"

    # Check errors for unreachable signals
    errors: list[str] = []
    for err in scrape_result.get("errors", []):
        errors.append(str(err))
    error_text = " ".join(errors)

    # Also check top-level error field
    top_error = scrape_result.get("error", "")
    if top_error:
        error_text += " " + str(top_error)

    for signal in _UNREACHABLE_SIGNALS:
        if signal in error_text:
            return "FAILED_UNREACHABLE"

    return "FAILED_NO_DATA"


def _render_status_table(
    scrape_result: dict[str, Any],
    property_id: str,
    verdict: str,
) -> str:
    """Render the Status section table."""
    units = scrape_result.get("units") or []
    duration = scrape_result.get("scrape_duration_s", "n/a")
    llm_cost = scrape_result.get("llm_cost", 0.0)

    lines = [
        "## Status",
        "| | |",
        "|---|---|",
        f"| **Verdict** | {verdict} |",
        f"| Canonical ID | {property_id} |",
        f"| Units extracted | {len(units)} |",
        f"| Scrape duration | {duration}s |",
        f"| LLM cost | ${llm_cost:.4f} |" if isinstance(llm_cost, (int, float)) else f"| LLM cost | ${llm_cost} |",
    ]
    return "\n".join(lines)


def _render_detection_section(scrape_result: dict[str, Any]) -> str | None:
    """Render the Detection section. Returns None if no detection info available."""
    detection = scrape_result.get("_detected_pms")
    if detection is None:
        return None

    pms_name = detection.get("name", "unknown")
    confidence = detection.get("confidence", "n/a")
    evidence_list = detection.get("evidence", [])
    client_account_id = detection.get("client_account_id", "n/a")
    adapter = detection.get("adapter", "n/a")

    evidence_str = ", ".join(str(e) for e in evidence_list) if evidence_list else "none"

    lines = [
        "## Detection",
        "| | |",
        "|---|---|",
        f"| Detected PMS | {pms_name} (confidence {confidence}) |",
        f"| Evidence | {evidence_str} |",
        f"| Client account ID | {client_account_id} |",
        f"| Adapter used | {adapter} |",
    ]
    return "\n".join(lines)


def _render_pipeline_section(scrape_result: dict[str, Any]) -> str | None:
    """Render the Pipeline section showing each step's outcome."""
    steps = scrape_result.get("pipeline_steps")
    if not steps:
        return None

    lines = [
        "## Pipeline",
        "| Step | Outcome | Notes |",
        "|---|---|---|",
    ]
    for step in steps:
        name = step.get("name", "")
        outcome = step.get("outcome", "")
        notes = step.get("notes", "")
        lines.append(f"| {name} | {outcome} | {notes} |")
    return "\n".join(lines)


def _render_changes_section(
    scrape_result: dict[str, Any],
    prior_units: list[dict[str, Any]] | None,
) -> str | None:
    """Render the Changes since last run section.

    Compares current units to prior_units by unit_id. Returns None if
    prior_units is None (new property with no prior run).
    """
    if prior_units is None:
        return None

    current_units = scrape_result.get("units") or []

    prior_by_id: dict[str, dict[str, Any]] = {}
    for u in prior_units:
        uid = u.get("unit_id") or u.get("unit_number", "")
        if uid:
            prior_by_id[uid] = u

    current_by_id: dict[str, dict[str, Any]] = {}
    for u in current_units:
        uid = u.get("unit_id") or u.get("unit_number", "")
        if uid:
            current_by_id[uid] = u

    new_ids = set(current_by_id) - set(prior_by_id)
    removed_ids = set(prior_by_id) - set(current_by_id)
    common_ids = set(current_by_id) & set(prior_by_id)

    changed_units: list[str] = []
    for uid in sorted(common_ids):
        cur = current_by_id[uid]
        prev = prior_by_id[uid]
        diffs: list[str] = []
        for key in sorted(set(cur.keys()) | set(prev.keys())):
            if key.startswith("_"):
                continue
            if cur.get(key) != prev.get(key):
                diffs.append(f"{key}: {prev.get(key)} -> {cur.get(key)}")
        if diffs:
            changed_units.append(f"- **{uid}**: {'; '.join(diffs)}")

    if not new_ids and not removed_ids and not changed_units:
        return "## Changes since last run\nNo changes detected."

    lines = ["## Changes since last run"]
    if new_ids:
        lines.append(f"- **New units**: {', '.join(sorted(new_ids))}")
    if removed_ids:
        lines.append(f"- **Removed units**: {', '.join(sorted(removed_ids))}")
    if changed_units:
        lines.append("- **Updated units**:")
        lines.extend(f"  {c}" for c in changed_units)

    return "\n".join(lines)


def _render_issues_section(issues: list[dict[str, Any]] | None) -> str | None:
    """Render the Issues section. Returns None if no issues."""
    if not issues:
        return None

    lines = ["## Issues"]
    for issue in issues:
        severity = issue.get("severity", "INFO")
        code = issue.get("code", "")
        message = issue.get("message", "")
        lines.append(f"- **{severity}** {code}: {message}")
    return "\n".join(lines)


def _render_llm_section(scrape_result: dict[str, Any]) -> str | None:
    """Render the LLM calls section inside a collapsed details tag.

    Returns None if no LLM calls recorded.
    """
    llm_calls = scrape_result.get("llm_calls")
    if not llm_calls:
        return None

    lines = [
        "## LLM calls",
        "<details>",
        "<summary>Show LLM transcripts</summary>",
        "",
    ]
    for i, call in enumerate(llm_calls, 1):
        model = call.get("model", "unknown")
        prompt = call.get("prompt", "")
        response = call.get("response", "")
        lines.append(f"### Call {i} ({model})")
        lines.append("")
        lines.append("**Prompt:**")
        lines.append(f"```\n{prompt}\n```")
        lines.append("")
        lines.append("**Response:**")
        lines.append(f"```\n{response}\n```")
        lines.append("")

    lines.append("</details>")
    return "\n".join(lines)


def generate_property_report(
    scrape_result: dict[str, Any],
    property_id: str,
    run_date: str,
    prior_units: list[dict[str, Any]] | None = None,
    issues: list[dict[str, Any]] | None = None,
) -> str:
    """Generate a per-property markdown report.

    Args:
        scrape_result: The full scrape result dict for a property. Expected keys
            include 'units', 'property_name', 'errors', 'error',
            '_carry_forward', '_detected_pms', 'pipeline_steps', 'llm_calls',
            'scrape_duration_s', 'llm_cost'.
        property_id: Canonical property identifier.
        run_date: Date string for the run (e.g. "2026-04-17").
        prior_units: Units from the previous run for diff comparison. None for
            new properties with no prior data.
        issues: List of validation issue dicts with 'severity', 'code', 'message'.

    Returns:
        A markdown string containing the full property report.
    """
    property_name = scrape_result.get("property_name", property_id)
    verdict = _determine_verdict(scrape_result)

    sections: list[str] = []

    # Title
    sections.append(f"# {property_name} — {run_date}")

    # Status (always present)
    sections.append(_render_status_table(scrape_result, property_id, verdict))

    # Detection (omit if unavailable)
    detection = _render_detection_section(scrape_result)
    if detection is not None:
        sections.append(detection)

    # Pipeline (omit if no steps)
    pipeline = _render_pipeline_section(scrape_result)
    if pipeline is not None:
        sections.append(pipeline)

    # Changes since last run (omit for new properties)
    changes = _render_changes_section(scrape_result, prior_units)
    if changes is not None:
        sections.append(changes)

    # Issues (omit if none)
    issues_section = _render_issues_section(issues)
    if issues_section is not None:
        sections.append(issues_section)

    # LLM calls (omit if none)
    llm_section = _render_llm_section(scrape_result)
    if llm_section is not None:
        sections.append(llm_section)

    return "\n\n".join(sections) + "\n"
