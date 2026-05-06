"""API response LLM analyzer (ApiAnalyzer).

Handles analyze_api_with_llm() for single API response analysis
that produces reusable field mappings.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from services.llm._shared import _normalize_units, _parse_llm_response

log = logging.getLogger(__name__)

_API_ANALYSIS_PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "prompts" / "api_analysis.txt"


def _load_api_analysis_prompt() -> str:
    try:
        return _API_ANALYSIS_PROMPT_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        log.warning("api_analysis.txt not found at %s", _API_ANALYSIS_PROMPT_PATH)
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


async def analyze_api_with_llm(
    api_response: dict,
    property_context: dict,
    property_id: str = "unknown",
) -> tuple[list[dict], dict | None, bool, dict[str, Any] | None]:
    """Analyze a SINGLE API response with LLM to extract units and learn field mappings.

    Args:
        api_response: Dict with "url" and "body" keys from network interception.
        property_context: Dict with property_name, website.
        property_id: Canonical property ID for interaction logging.

    Returns:
        Tuple of (units, llm_field_mapping_dict, is_noise, interaction_record).
        llm_field_mapping_dict contains json_paths + response_envelope if units found.
        is_noise is True if LLM determined this API has no unit data.
    """
    template = _load_api_analysis_prompt()
    if not template:
        return [], None, False, None

    api_url = api_response.get("url", "unknown")
    body = api_response.get("body", {})
    body_str = json.dumps(body, indent=2) if isinstance(body, (dict, list)) else str(body)
    # Cap API response to ~30KB to control token usage
    if len(body_str) > 30_000:
        body_str = body_str[:30_000] + "\n... (truncated)"

    # Phase 2 context enrichment — every targeted prompt now sees the CSV
    # metadata so the LLM can disambiguate (e.g. PMC-specific plan names)
    # and validate addresses against the known city/state.
    prompt = template.replace("{property_name}", property_context.get("property_name", "") or "Unknown")
    prompt = prompt.replace("{city}", property_context.get("city", "") or "")
    prompt = prompt.replace("{state}", property_context.get("state", "") or "")
    prompt = prompt.replace("{pmc}", property_context.get("pmc", "") or "")
    prompt = prompt.replace("{website}", property_context.get("website", "") or "")
    prompt = prompt.replace("{api_url}", api_url)
    prompt = prompt.replace("{api_response_json}", body_str)
    prompt = prompt.replace("{known_floor_plans}", _resolve_known_plans_block(property_context))

    system = (
        "You are a real estate data extraction agent analyzing API responses. "
        "Return ONLY valid JSON. No markdown, no commentary."
    )

    try:
        from llm.factory import get_text_provider

        provider = get_text_provider()
    except Exception as exc:
        log.error("Failed to get LLM provider for API analysis: %s", exc)
        return [], None, False, None

    t0 = time.monotonic()
    timestamp = datetime.now(UTC).isoformat()
    raw_response = ""
    error_msg: str | None = None

    try:
        raw_response = await provider.complete(system, prompt, max_tokens=4096)
    except Exception as exc:
        log.error("API analysis LLM call failed: %s", exc)
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
            tier="API_ANALYSIS",
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
        log.warning("Failed to build API analysis interaction record: %s", exc)
        interaction = None

    if error_msg:
        return [], None, False, interaction

    parsed = _parse_llm_response(raw_response)

    has_unit_data = parsed.get("has_unit_data", False)
    is_noise = not has_unit_data

    if is_noise:
        noise_reason = parsed.get("noise_reason", "unknown")
        log.info("API analysis: %s is noise (%s) | latency=%dms", api_url[:80], noise_reason, latency_ms)
        return [], None, True, interaction

    # Extract units
    raw_units = parsed.get("units", [])
    if not isinstance(raw_units, list):
        raw_units = []
    units = _normalize_units(raw_units)

    # Build field mapping for profile persistence
    json_paths = parsed.get("json_paths", {})
    response_envelope = parsed.get("response_envelope", "")
    mapping_dict: dict | None = None
    if isinstance(json_paths, dict) and json_paths:
        mapping_dict = {
            "api_url_pattern": api_url,
            "json_paths": json_paths,
            "response_envelope": response_envelope if isinstance(response_envelope, str) else "",
        }

    log.info(
        "API analysis: %s → %d units, mapping=%s | tokens=%d+%d | latency=%dms",
        api_url[:80],
        len(units),
        "yes" if mapping_dict else "no",
        tokens_in,
        tokens_out,
        latency_ms,
    )
    return units, mapping_dict, False, interaction
