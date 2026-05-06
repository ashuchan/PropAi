"""DOM section LLM analyzer (DomAnalyzer).

Handles analyze_dom_with_llm() for DOM section analysis
that produces reusable CSS selectors.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from services.llm._shared import _normalize_units, _parse_llm_response

log = logging.getLogger(__name__)

_DOM_ANALYSIS_PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "prompts" / "dom_analysis.txt"


def _load_dom_analysis_prompt() -> str:
    try:
        return _DOM_ANALYSIS_PROMPT_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        log.warning("dom_analysis.txt not found at %s", _DOM_ANALYSIS_PROMPT_PATH)
        return ""


def _resolve_known_plans_block(property_context: dict[str, Any]) -> str:
    """Render the KNOWN FLOOR PLANS prompt block for this property."""
    pid = ""
    for key in ("property_id", "apartmentid", "canonical_id"):
        v = property_context.get(key)
        if v:
            pid = str(v)
            break
    if not pid:
        return ""
    try:
        from ma_poc.services.floorplan_catalog import get_default_catalog

        text, _meta = get_default_catalog().known_plans_block(pid)
        return text
    except Exception as exc:  # noqa: BLE001
        log.warning("known-plans block unavailable for %s: %s", pid, exc)
        return ""


async def analyze_dom_with_llm(
    dom_html: str,
    page_url: str,
    property_context: dict,
    property_id: str = "unknown",
) -> tuple[list[dict], dict | None, dict[str, Any] | None]:
    """Analyze a DOM section with LLM to extract units and learn CSS selectors.

    Args:
        dom_html: The HTML of the relevant DOM section (not full page).
        page_url: URL of the page containing this section.
        property_context: Dict with property_name, website.
        property_id: Canonical property ID for interaction logging.

    Returns:
        Tuple of (units, css_selectors_dict, interaction_record).
        css_selectors_dict contains learned selectors if units found.
    """
    template = _load_dom_analysis_prompt()
    if not template:
        return [], None, None

    # Cap DOM section to ~20KB
    if len(dom_html) > 20_000:
        dom_html = dom_html[:20_000] + "\n<!-- truncated -->"

    prompt = template.replace("{property_name}", property_context.get("property_name", "") or "Unknown")
    prompt = prompt.replace("{city}", property_context.get("city", "") or "")
    prompt = prompt.replace("{state}", property_context.get("state", "") or "")
    prompt = prompt.replace("{pmc}", property_context.get("pmc", "") or "")
    prompt = prompt.replace("{website}", property_context.get("website", "") or "")
    prompt = prompt.replace("{page_url}", page_url)
    prompt = prompt.replace("{dom_section_html}", dom_html)
    prompt = prompt.replace("{known_floor_plans}", _resolve_known_plans_block(property_context))

    system = (
        "You are a real estate data extraction agent analyzing website DOM. "
        "Return ONLY valid JSON. No markdown, no commentary."
    )

    try:
        from llm.factory import get_text_provider

        provider = get_text_provider()
    except Exception as exc:
        log.error("Failed to get LLM provider for DOM analysis: %s", exc)
        return [], None, None

    t0 = time.monotonic()
    timestamp = datetime.now(UTC).isoformat()
    raw_response = ""
    error_msg: str | None = None

    try:
        raw_response = await provider.complete(system, prompt, max_tokens=4096)
    except Exception as exc:
        log.error("DOM analysis LLM call failed: %s", exc)
        error_msg = str(exc)
        raw_response = f"llm_error: {exc}"

    latency_ms = int((time.monotonic() - t0) * 1000)

    usage: dict[str, Any] = getattr(provider, "_last_usage", {})
    tokens_in = int(usage.get("input_tokens", 0))
    tokens_out = int(usage.get("output_tokens", 0))
    model = str(usage.get("model", "unknown"))
    prov_name = str(usage.get("provider", "unknown"))

    try:
        from llm.interaction_logger import make_interaction

        interaction: dict[str, Any] | None = make_interaction(
            property_id=property_id,
            tier="DOM_ANALYSIS",
            call_type="text",
            provider=prov_name,
            model=model,
            system_prompt=system,
            user_prompt=prompt[:500] + "...(truncated)",
            raw_response=raw_response,
            tokens_input=tokens_in,
            tokens_output=tokens_out,
            latency_ms=latency_ms,
            timestamp=timestamp,
            success=error_msg is None,
            error=error_msg,
        )
    except Exception as exc:
        log.warning("Failed to build DOM analysis interaction record: %s", exc)
        interaction = None

    if error_msg:
        return [], None, interaction

    parsed = _parse_llm_response(raw_response)

    raw_units = parsed.get("units", [])
    if not isinstance(raw_units, list):
        raw_units = []
    units = _normalize_units(raw_units)

    css_selectors = parsed.get("css_selectors", {})
    selectors_dict: dict | None = None
    if isinstance(css_selectors, dict) and css_selectors.get("container"):
        selectors_dict = css_selectors

    log.info(
        "DOM analysis: %s → %d units, selectors=%s | tokens=%d+%d | latency=%dms",
        page_url[:80],
        len(units),
        "yes" if selectors_dict else "no",
        tokens_in,
        tokens_out,
        latency_ms,
    )
    return units, selectors_dict, interaction
