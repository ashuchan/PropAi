"""
Per-Property Scrape Report Generator
=====================================

Generates a detailed markdown report for each property after scraping,
covering every phase of the pipeline: pages loaded, links explored/ignored,
APIs captured/filtered, LLM prompts/responses, and extraction results.

Output: ``data/runs/{date}/property_reports/{canonical_id}.md``

Usage (called by daily_runner.py / retry_runner.py):
    from scripts.scrape_report import generate_property_report
    generate_property_report(scrape_result, property_record, unit_diff,
                             per_prop_issues, run_dir, canonical_id, run_date)
"""
from __future__ import annotations

import json
import textwrap
from datetime import datetime
from pathlib import Path
from typing import Any


def generate_property_report(
    scrape_result: dict[str, Any],
    property_record: dict[str, Any] | None,
    unit_diff: dict[str, list],
    per_prop_issues: list,
    run_dir: Path,
    canonical_id: str,
    run_date: str,
) -> Path | None:
    """Generate a detailed markdown scrape report for one property.

    Args:
        scrape_result: Raw dict returned by ``entrata.scrape()``.
        property_record: The 46-key property record (may be None on failure).
        unit_diff: ``{new, updated, unchanged, disappeared}`` from state diff.
        per_prop_issues: Validation issues for this property.
        run_dir: Daily run directory (``data/runs/{date}/``).
        canonical_id: Stable property identifier.
        run_date: ISO date string (``YYYY-MM-DD``).

    Returns:
        Path to the written markdown file, or None on error.
    """
    try:
        report_dir = run_dir / "property_reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        safe_cid = _safe_filename(canonical_id)
        out_path = report_dir / f"{safe_cid}.md"

        md = _build_report(
            scrape_result, property_record, unit_diff,
            per_prop_issues, canonical_id, run_date,
        )

        with open(out_path, "w", encoding="utf-8") as f:
            f.write(md)

        return out_path
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Report builder
# ---------------------------------------------------------------------------

def _build_report(
    sr: dict[str, Any],
    rec: dict[str, Any] | None,
    unit_diff: dict[str, list],
    issues: list,
    cid: str,
    run_date: str,
) -> str:
    """Assemble the full markdown report string."""
    sections: list[str] = []

    # Header
    prop_name = sr.get("property_name", cid)
    base_url = sr.get("base_url", "")
    tier_used = sr.get("extraction_tier_used", "UNKNOWN")
    unit_count = len(sr.get("units") or [])

    sections.append(f"# Scrape Report: {prop_name}")
    sections.append(f"**Canonical ID:** `{cid}`  ")
    sections.append(f"**URL:** {base_url}  ")
    sections.append(f"**Run Date:** {run_date}  ")
    sections.append(f"**Scraped At:** {sr.get('scraped_at', 'N/A')}  ")
    sections.append(f"**Extraction Tier:** `{tier_used}`  ")
    sections.append(f"**Units Extracted:** {unit_count}  ")
    winning = sr.get("_winning_page_url")
    if winning:
        sections.append(f"**Winning Source:** {winning}  ")
    sections.append("")

    # Quick summary box
    sections.append(_summary_box(sr, unit_diff))

    # Property metadata
    sections.append(_metadata_section(sr, rec))

    # Profile routing
    sections.append(_profile_section(sr))

    # Phase 1: Homepage + network capture
    sections.append(_phase1_section(sr))

    # Phase 2: Noise filtering
    sections.append(_phase2_section(sr))

    # Phase 3: Known pattern extraction
    sections.append(_phase3_section(sr, tier_used))

    # Phase 4: Link exploration
    sections.append(_phase4_section(sr, tier_used))

    # Phase 5: LLM API analysis
    sections.append(_phase5_section(sr, tier_used))

    # Phase 6: DOM / Legacy LLM / Vision fallback
    sections.append(_phase6_section(sr, tier_used))

    # Phase 7: Finalization
    sections.append(_phase7_section(sr, unit_diff))

    # LLM interactions detail
    sections.append(_llm_interactions_section(sr))

    # Extracted units
    sections.append(_units_section(sr))

    # Validation issues
    sections.append(_issues_section(issues))

    # Errors
    sections.append(_errors_section(sr))

    # Raw API inventory
    sections.append(_api_inventory_section(sr))

    return "\n".join(s for s in sections if s)


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------

def _summary_box(sr: dict, unit_diff: dict) -> str:
    """Quick overview at the top."""
    lines = ["## Summary", ""]
    tier = sr.get("extraction_tier_used", "UNKNOWN")
    units = sr.get("units") or []
    apis = sr.get("api_calls_intercepted") or []
    links_found = sr.get("links_found") or []
    links_crawled = sr.get("property_links_crawled") or []
    errors = sr.get("errors") or []
    llm_calls = sr.get("_llm_interactions") or []

    total_cost = sum(i.get("cost_usd", 0) for i in llm_calls)

    lines.append("| Metric | Value |")
    lines.append("|---|---|")
    lines.append(f"| Extraction Tier | `{tier}` |")
    lines.append(f"| Units Extracted | {len(units)} |")
    lines.append(f"| APIs Intercepted | {len(apis)} |")
    lines.append(f"| Links Discovered | {len(links_found)} |")
    lines.append(f"| Links Explored | {len(links_crawled)} |")
    lines.append(f"| LLM Calls Made | {len(llm_calls)} |")
    lines.append(f"| LLM Total Cost | ${total_cost:.5f} |")
    lines.append(f"| Errors | {len(errors)} |")

    new = unit_diff.get("new", [])
    updated = unit_diff.get("updated", [])
    unchanged = unit_diff.get("unchanged", [])
    disappeared = unit_diff.get("disappeared", [])
    lines.append(f"| State Diff | new={len(new)}, updated={len(updated)}, "
                 f"unchanged={len(unchanged)}, disappeared={len(disappeared)} |")
    lines.append("")
    return "\n".join(lines)


def _metadata_section(sr: dict, rec: dict | None) -> str:
    """Property metadata from scrape and record."""
    meta = sr.get("property_metadata") or {}
    if not meta and not rec:
        return ""

    lines = ["## Property Metadata", ""]
    lines.append("| Field | Value |")
    lines.append("|---|---|")

    # From scrape metadata
    for key in ("name", "title", "address", "city", "state", "zip",
                "lat", "lng", "phone", "total_units"):
        val = meta.get(key)
        if val:
            lines.append(f"| {key} | {val} |")

    # From property record (CSV-enriched)
    if rec:
        for key in ("Property Name", "City", "State", "ZIP Code",
                     "Management Company", "Type", "Property Address"):
            val = rec.get(key)
            if val:
                lines.append(f"| {key} (record) | {val} |")

    lines.append("")
    return "\n".join(lines)


def _profile_section(sr: dict) -> str:
    """Profile routing decision."""
    skip_to = sr.get("_profile_skip_to_tier")
    cascade = sr.get("_profile_cascade")
    if skip_to is None and cascade is None:
        return ""

    lines = ["## Profile Routing", ""]
    lines.append(f"- **Skip to tier:** {skip_to if skip_to else 'None (full cascade)'}")
    lines.append(f"- **Run full cascade:** {cascade}")
    lines.append("")
    return "\n".join(lines)


def _phase1_section(sr: dict) -> str:
    """Phase 1: Homepage load + network capture."""
    links = sr.get("links_found") or []
    apis = sr.get("_raw_api_responses") or []

    lines = ["## Phase 1: Homepage Load + Network Capture", ""]
    lines.append(f"**Internal links discovered:** {len(links)}  ")
    lines.append(f"**API responses captured:** {len(apis)}  ")
    lines.append("")

    if links:
        lines.append("### Links Found")
        lines.append("")
        for i, link in enumerate(links, 1):
            lines.append(f"{i}. {link}")
        lines.append("")

    if apis:
        lines.append("### APIs Captured (from homepage load)")
        lines.append("")
        for i, resp in enumerate(apis, 1):
            url = resp.get("url", "unknown")
            body = resp.get("body")
            body_size = len(json.dumps(body, default=str)) if body else 0
            body_type = _classify_api_body(body)
            lines.append(f"{i}. `{url}`")
            lines.append(f"   - Size: {_human_size(body_size)}")
            lines.append(f"   - Type: {body_type}")
        lines.append("")

    return "\n".join(lines)


def _phase2_section(sr: dict) -> str:
    """Phase 2: Noise filtering."""
    all_apis = sr.get("_raw_api_responses") or []
    intercepted = sr.get("api_calls_intercepted") or []
    # The difference between raw responses and final intercepted gives us
    # a rough sense of filtering, but we don't have the exact blocked list.
    # We can show which APIs made it through.
    if not all_apis:
        return ""

    lines = ["## Phase 2: Noise Filtering", ""]
    lines.append(f"**APIs before filtering:** {len(all_apis)}  ")
    lines.append(f"**APIs after filtering:** {len(intercepted)}  ")
    blocked_count = len(all_apis) - len(intercepted)
    if blocked_count > 0:
        lines.append(f"**Blocked by noise filter:** {blocked_count}  ")

        # Show which URLs were filtered out
        intercepted_set = set(intercepted)
        blocked_urls = [r.get("url", "") for r in all_apis
                        if r.get("url", "") not in intercepted_set]
        if blocked_urls:
            lines.append("")
            lines.append("### Blocked APIs (noise)")
            lines.append("")
            for url in blocked_urls:
                lines.append(f"- ~~{url}~~")
    lines.append("")
    return "\n".join(lines)


def _phase3_section(sr: dict, tier_used: str) -> str:
    """Phase 3: Known pattern extraction."""
    phase3_tiers = {
        "TIER_1_API", "TIER_1_PROFILE_MAPPING", "TIER_1_5_EMBEDDED",
        "TIER_2_JSONLD", "TIER_3_DOM",
    }
    hit_phase3 = tier_used in phase3_tiers

    lines = ["## Phase 3: Known Pattern Extraction", ""]
    if hit_phase3:
        units = sr.get("units") or []
        lines.append(f"**Result:** SUCCESS — `{tier_used}` extracted {len(units)} units  ")
        winning = sr.get("_winning_page_url")
        if winning:
            lines.append(f"**Source:** {winning}  ")
        lines.append("")
        lines.append("Pipeline stopped here — Phases 4-6 were skipped.")
    else:
        lines.append("**Result:** No units found from homepage data.  ")
        lines.append("Tried: profile mappings, known endpoints, global API parser, "
                     "embedded JSON, JSON-LD, DOM parsing.")
        lines.append("")
        lines.append("Proceeding to Phase 4 (link exploration).")
    lines.append("")
    return "\n".join(lines)


def _phase4_section(sr: dict, tier_used: str) -> str:
    """Phase 4: Link-by-link exploration."""
    crawled = sr.get("property_links_crawled") or []
    explored = sr.get("_explored_links") or {}
    links_found = sr.get("links_found") or []

    # If Phase 3 succeeded, Phase 4 was skipped
    phase3_tiers = {
        "TIER_1_API", "TIER_1_PROFILE_MAPPING", "TIER_1_5_EMBEDDED",
        "TIER_2_JSONLD", "TIER_3_DOM",
    }
    if tier_used in phase3_tiers:
        return ""

    lines = ["## Phase 4: Link-by-Link Exploration", ""]
    lines.append(f"**Links explored:** {len(crawled)} / {len(links_found)} discovered  ")
    lines.append("")

    if crawled or explored:
        lines.append("### Explored Links")
        lines.append("")
        lines.append("| # | URL | Had Data |")
        lines.append("|---|---|---|")

        # Show explored links from the status dict first
        shown = set()
        idx = 1
        for link, had_data in explored.items():
            status = "YES" if had_data else "no"
            lines.append(f"| {idx} | {link} | {status} |")
            shown.add(link)
            idx += 1

        # Add any crawled links not in explored dict
        for link in crawled:
            if link not in shown:
                lines.append(f"| {idx} | {link} | ? |")
                idx += 1

        lines.append("")

    # Links NOT explored (ignored)
    ignored_links = [l for l in links_found if l not in set(crawled)]
    if ignored_links:
        lines.append(f"### Links NOT Explored ({len(ignored_links)} skipped)")
        lines.append("")
        # Show first 30, then truncate
        for link in ignored_links[:30]:
            lines.append(f"- {link}")
        if len(ignored_links) > 30:
            lines.append(f"- ... and {len(ignored_links) - 30} more")
        lines.append("")

    phase4_tiers = {"TIER_5_5_EXPLORATORY", "TIER_4_ENTRATA_API", "TIER_5_PORTAL"}
    if tier_used in phase4_tiers:
        units = sr.get("units") or []
        lines.append(f"**Result:** SUCCESS — `{tier_used}` extracted {len(units)} units  ")
        winning = sr.get("_winning_page_url")
        if winning:
            lines.append(f"**Winning page:** {winning}  ")
    else:
        llm_candidates = sr.get("_llm_analysis_results") or {}
        lines.append(f"**Result:** No units from deterministic extraction.  ")
        if llm_candidates:
            lines.append(f"Collected {len(llm_candidates)} LLM candidate APIs for Phase 5.")

    lines.append("")
    return "\n".join(lines)


def _phase5_section(sr: dict, tier_used: str) -> str:
    """Phase 5: LLM-assisted API analysis."""
    analysis = sr.get("_llm_analysis_results") or {}
    if not analysis:
        return ""

    lines = ["## Phase 5: LLM-Assisted API Analysis", ""]
    lines.append(f"**APIs analyzed:** {len(analysis)}  ")
    lines.append("")

    lines.append("### Analysis Results")
    lines.append("")
    lines.append("| # | API URL | Result |")
    lines.append("|---|---|---|")

    for idx, (url, result) in enumerate(analysis.items(), 1):
        if isinstance(result, str) and result.startswith("noise"):
            lines.append(f"| {idx} | `{_trunc(url, 80)}` | NOISE — {result} |")
        elif isinstance(result, dict):
            jp = result.get("json_paths", {})
            env = result.get("response_envelope", "")
            lines.append(f"| {idx} | `{_trunc(url, 80)}` | UNITS FOUND — "
                         f"envelope=`{env}`, {len(jp)} field mappings |")
        else:
            lines.append(f"| {idx} | `{_trunc(url, 80)}` | {result} |")
    lines.append("")

    # Show the field mapping detail if any
    for url, result in analysis.items():
        if isinstance(result, dict) and result.get("json_paths"):
            lines.append(f"**Field mapping for** `{_trunc(url, 100)}`:")
            lines.append("")
            lines.append("| Target Field | JSON Key |")
            lines.append("|---|---|")
            for field, key in result["json_paths"].items():
                lines.append(f"| {field} | `{key}` |")
            if result.get("response_envelope"):
                lines.append(f"\n**Response envelope:** `{result['response_envelope']}`")
            lines.append("")

    return "\n".join(lines)


def _phase6_section(sr: dict, tier_used: str) -> str:
    """Phase 6: DOM / Legacy LLM / Vision fallback."""
    phase6_tiers = {"TIER_3_DOM_LLM", "TIER_4_LLM", "TIER_5_VISION"}
    # Only show if we got to Phase 6 or all failed
    earlier_tiers = {
        "TIER_1_API", "TIER_1_PROFILE_MAPPING", "TIER_1_5_EMBEDDED",
        "TIER_2_JSONLD", "TIER_3_DOM", "TIER_5_5_EXPLORATORY",
        "TIER_4_ENTRATA_API", "TIER_5_PORTAL",
    }
    if tier_used in earlier_tiers:
        return ""

    lines = ["## Phase 6: DOM / LLM / Vision Fallback", ""]

    if tier_used in phase6_tiers:
        units = sr.get("units") or []
        lines.append(f"**Result:** SUCCESS — `{tier_used}` extracted {len(units)} units  ")
    elif tier_used == "FAILED":
        lines.append("**Result:** ALL PHASES FAILED — no units extracted.  ")
    else:
        lines.append(f"**Result:** `{tier_used}`  ")

    # Show LLM hints if any CSS selectors or json_paths were discovered
    hints = sr.get("_llm_hints") or {}
    if hints:
        lines.append("")
        lines.append("### LLM-Discovered Hints")
        lines.append("")
        if hints.get("css_selectors"):
            lines.append("**CSS Selectors:**")
            lines.append("")
            for field, sel in hints["css_selectors"].items():
                lines.append(f"- `{field}`: `{sel}`")
        if hints.get("json_paths"):
            lines.append("**JSON Paths:**")
            lines.append("")
            for field, path in hints["json_paths"].items():
                lines.append(f"- `{field}`: `{path}`")

    lines.append("")
    return "\n".join(lines)


def _phase7_section(sr: dict, unit_diff: dict) -> str:
    """Phase 7: Finalization + state diff."""
    units = sr.get("units") or []
    if not units:
        return ""

    lines = ["## Phase 7: Finalization", ""]
    lines.append(f"**Final unit count:** {len(units)}  ")
    lines.append("Availability defaults applied (AVAILABLE + today) where missing.  ")
    lines.append("")

    new = unit_diff.get("new", [])
    updated = unit_diff.get("updated", [])
    unchanged = unit_diff.get("unchanged", [])
    disappeared = unit_diff.get("disappeared", [])

    lines.append("### State Diff")
    lines.append("")
    lines.append(f"- **New units:** {len(new)}")
    lines.append(f"- **Updated units:** {len(updated)}")
    lines.append(f"- **Unchanged units:** {len(unchanged)}")
    lines.append(f"- **Disappeared units:** {len(disappeared)}")

    if new:
        lines.append("")
        lines.append("**New unit IDs:** " + ", ".join(f"`{u}`" for u in new[:20]))
        if len(new) > 20:
            lines.append(f"... and {len(new) - 20} more")
    if disappeared:
        lines.append("")
        lines.append("**Disappeared unit IDs:** " + ", ".join(f"`{u}`" for u in disappeared[:20]))
        if len(disappeared) > 20:
            lines.append(f"... and {len(disappeared) - 20} more")

    lines.append("")
    return "\n".join(lines)


def _llm_interactions_section(sr: dict) -> str:
    """Full detail of every LLM call: prompt, response, cost."""
    interactions = sr.get("_llm_interactions") or []
    if not interactions:
        return ""

    lines = ["## LLM Interactions (Full Detail)", ""]
    total_cost = sum(i.get("cost_usd", 0) for i in interactions)
    total_input = sum(i.get("tokens_input", 0) for i in interactions)
    total_output = sum(i.get("tokens_output", 0) for i in interactions)

    lines.append(f"**Total calls:** {len(interactions)}  ")
    lines.append(f"**Total tokens:** {total_input:,} input + {total_output:,} output "
                 f"= {total_input + total_output:,} total  ")
    lines.append(f"**Total cost:** ${total_cost:.5f}  ")
    lines.append("")

    for idx, interaction in enumerate(interactions, 1):
        tier = interaction.get("tier", "unknown")
        provider = interaction.get("provider", "unknown")
        model = interaction.get("model", "unknown")
        success = interaction.get("success", False)
        cost = interaction.get("cost_usd", 0)
        latency = interaction.get("latency_ms", 0)
        tokens_in = interaction.get("tokens_input", 0)
        tokens_out = interaction.get("tokens_output", 0)
        error = interaction.get("error")
        timestamp = interaction.get("timestamp", "")

        status_icon = "OK" if success else "FAILED"

        lines.append(f"### LLM Call {idx}: {tier} ({status_icon})")
        lines.append("")
        lines.append("| Field | Value |")
        lines.append("|---|---|")
        lines.append(f"| Provider | {provider} |")
        lines.append(f"| Model | {model} |")
        lines.append(f"| Tier | {tier} |")
        lines.append(f"| Call Type | {interaction.get('call_type', 'text')} |")
        lines.append(f"| Timestamp | {timestamp} |")
        lines.append(f"| Latency | {latency:,}ms |")
        lines.append(f"| Tokens (in/out) | {tokens_in:,} / {tokens_out:,} |")
        lines.append(f"| Cost | ${cost:.5f} |")
        lines.append(f"| Success | {success} |")
        if error:
            lines.append(f"| Error | {error} |")
        lines.append("")

        # System prompt
        sys_prompt = interaction.get("system_prompt", "")
        if sys_prompt:
            lines.append("<details>")
            lines.append(f"<summary>System Prompt ({len(sys_prompt):,} chars)</summary>")
            lines.append("")
            lines.append("```")
            lines.append(_trunc(sys_prompt, 3000))
            lines.append("```")
            lines.append("")
            lines.append("</details>")
            lines.append("")

        # User prompt
        user_prompt = interaction.get("user_prompt", "")
        if user_prompt:
            lines.append("<details>")
            lines.append(f"<summary>User Prompt ({len(user_prompt):,} chars)</summary>")
            lines.append("")
            lines.append("```")
            lines.append(_trunc(user_prompt, 5000))
            lines.append("```")
            lines.append("")
            lines.append("</details>")
            lines.append("")

        # Raw response
        raw_resp = interaction.get("raw_response", "")
        if raw_resp:
            lines.append("<details>")
            lines.append(f"<summary>LLM Response ({len(raw_resp):,} chars)</summary>")
            lines.append("")
            lines.append("```json")
            lines.append(_trunc(raw_resp, 5000))
            lines.append("```")
            lines.append("")
            lines.append("</details>")
            lines.append("")

        lines.append("---")
        lines.append("")

    return "\n".join(lines)


def _units_section(sr: dict) -> str:
    """Extracted units table."""
    units = sr.get("units") or []
    if not units:
        lines = ["## Extracted Units", ""]
        lines.append("No units extracted.")
        lines.append("")
        return "\n".join(lines)

    lines = ["## Extracted Units", ""]
    lines.append(f"**Total:** {len(units)}  ")
    lines.append("")

    # Determine which columns are present
    all_keys = set()
    for u in units:
        all_keys.update(u.keys())

    # Priority columns for the table
    priority_cols = [
        "unit_number", "unit_id", "floor_plan_name",
        "bedrooms", "bathrooms", "sqft",
        "rent_low", "rent_high", "asking_rent",
        "market_rent_low", "market_rent_high",
        "availability_status", "availability_date", "available_date",
    ]
    # Use only columns that exist in the data
    cols = [c for c in priority_cols if c in all_keys]
    # Add any remaining non-internal columns
    for k in sorted(all_keys):
        if k not in cols and not k.startswith("_"):
            cols.append(k)

    # Cap columns for readability
    display_cols = cols[:10]

    lines.append("| " + " | ".join(display_cols) + " |")
    lines.append("| " + " | ".join("---" for _ in display_cols) + " |")

    for u in units[:50]:  # Cap at 50 rows for report size
        row_vals = []
        for c in display_cols:
            val = u.get(c, "")
            if val is None:
                val = ""
            row_vals.append(str(val).replace("|", "\\|")[:40])
        lines.append("| " + " | ".join(row_vals) + " |")

    if len(units) > 50:
        lines.append(f"\n*... {len(units) - 50} more units not shown.*")

    lines.append("")
    return "\n".join(lines)


def _issues_section(issues: list) -> str:
    """Validation issues."""
    if not issues:
        return ""

    lines = ["## Validation Issues", ""]
    lines.append(f"**Total:** {len(issues)}  ")
    lines.append("")

    lines.append("| Severity | Code | Message |")
    lines.append("|---|---|---|")
    for issue in issues:
        sev = getattr(issue, "severity", "?")
        code = getattr(issue, "code", "?")
        msg = getattr(issue, "message", str(issue))
        lines.append(f"| {sev} | `{code}` | {_trunc(msg, 120)} |")

    lines.append("")
    return "\n".join(lines)


def _errors_section(sr: dict) -> str:
    """Scraping errors."""
    errors = sr.get("errors") or []
    if not errors:
        return ""

    lines = ["## Errors", ""]
    for i, err in enumerate(errors, 1):
        lines.append(f"{i}. {err}")
    lines.append("")
    return "\n".join(lines)


def _api_inventory_section(sr: dict) -> str:
    """Complete inventory of all API responses captured."""
    raw = sr.get("_raw_api_responses") or []
    if not raw:
        return ""

    lines = ["## Raw API Inventory", ""]
    lines.append(f"**Total API responses:** {len(raw)}  ")
    lines.append("")

    lines.append("| # | URL | Size | Preview |")
    lines.append("|---|---|---|---|")

    for idx, resp in enumerate(raw, 1):
        url = resp.get("url", "unknown")
        body = resp.get("body")
        body_str = json.dumps(body, default=str) if body else ""
        size = _human_size(len(body_str))
        preview = _body_preview(body)
        lines.append(f"| {idx} | `{_trunc(url, 70)}` | {size} | {_trunc(preview, 60)} |")

    lines.append("")

    # Detailed body samples for each API (collapsible)
    lines.append("### API Response Bodies")
    lines.append("")

    for idx, resp in enumerate(raw, 1):
        url = resp.get("url", "unknown")
        body = resp.get("body")
        if body is None:
            continue

        body_str = json.dumps(body, indent=2, default=str)

        lines.append("<details>")
        lines.append(f"<summary>API {idx}: {_trunc(url, 100)} "
                     f"({_human_size(len(body_str))})</summary>")
        lines.append("")
        lines.append("```json")
        lines.append(_trunc(body_str, 3000))
        lines.append("```")
        lines.append("")
        lines.append("</details>")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_filename(s: str, max_len: int = 80) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in s)[:max_len]


def _trunc(s: str, max_len: int) -> str:
    if len(s) <= max_len:
        return s
    return s[:max_len - 3] + "..."


def _human_size(byte_count: int) -> str:
    if byte_count < 1024:
        return f"{byte_count}B"
    elif byte_count < 1024 * 1024:
        return f"{byte_count / 1024:.1f}KB"
    else:
        return f"{byte_count / (1024 * 1024):.1f}MB"


def _classify_api_body(body: Any) -> str:
    """Classify an API response body by its structure."""
    if body is None:
        return "null"
    if isinstance(body, list):
        if body and isinstance(body[0], dict):
            keys = list(body[0].keys())[:5]
            return f"list[dict] ({len(body)} items, keys: {keys})"
        return f"list ({len(body)} items)"
    if isinstance(body, dict):
        keys = list(body.keys())[:8]
        return f"dict (keys: {keys})"
    return type(body).__name__


def _body_preview(body: Any) -> str:
    """Short textual preview of an API body."""
    if body is None:
        return "null"
    if isinstance(body, list):
        if body and isinstance(body[0], dict):
            return f"[{{{', '.join(list(body[0].keys())[:4])}...}}] ({len(body)} items)"
        return f"[...] ({len(body)} items)"
    if isinstance(body, dict):
        return f"{{{', '.join(list(body.keys())[:5])}...}}"
    s = str(body)
    return s[:60]
