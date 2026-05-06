"""Full-page LLM extraction (MonolithicExtractor).

Handles the extract_with_llm() function which combines full-page HTML
with captured API responses for a single comprehensive LLM call.
Also contains prompt loading and input preparation helpers.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from services.llm._shared import _normalize_units, _parse_llm_response, _rank_api_responses, _trim_html

log = logging.getLogger(__name__)

# Load prompt template
_PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "prompts" / "tier4_extraction.txt"

_FALLBACK_PROMPT = """You are a real estate data extraction specialist.
Extract unit-level apartment availability data from the provided website content.

PROPERTY CONTEXT:
- Name: {property_name}
- City: {city}, {state}
- Management Company: {pmc}
- Website: {website}

CONTENT TO ANALYZE:
{content_type}:

---
{trimmed_content}
---

OUTPUT FORMAT — respond with ONLY a JSON object, no markdown fences:
{{
  "units": [
    {{
      "unit_id": "string or null",
      "floor_plan_name": "string or null",
      "bedrooms": number_or_null,
      "bathrooms": number_or_null,
      "sqft": number_or_null,
      "market_rent_low": number_or_null,
      "market_rent_high": number_or_null,
      "available_date": "YYYY-MM-DD or null",
      "availability_status": "AVAILABLE|UNAVAILABLE|WAITLIST|UNKNOWN",
      "lease_term": number_or_null,
      "move_in_date": "YYYY-MM-DD or null",
      "confidence": 0.0-1.0
    }}
  ],
  "profile_hints": {{
    "api_urls_with_data": [],
    "json_paths": {{}},
    "css_selectors": {{}},
    "platform_guess": null,
    "navigation_hint": "",
    "field_mapping_notes": ""
  }}
}}

RULES:
- Extract ALL available units, not just a sample.
- If data is floor-plan-level (not unit-level), extract floor plans.
- For rent ranges like "$1,200 - $1,500", set market_rent_low=1200, market_rent_high=1500.
- If data is NOT on this page, put the subpage URL in navigation_hint.
- confidence: 1.0 = certain, 0.7 = likely correct, <0.5 = guessing.
"""


def _load_prompt_template() -> str:
    """Load the Tier 4 extraction prompt template."""
    try:
        return _PROMPT_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        log.warning("Prompt template not found at %s, using inline fallback", _PROMPT_PATH)
        return _FALLBACK_PROMPT


def _resolve_known_plans_block(property_context: dict[str, Any]) -> str:
    """Render the KNOWN FLOOR PLANS prompt block for this property.

    Returns the empty string when the catalog is bypassed (H6) or when the
    property has no plans on file. Failures are swallowed so prompt
    construction can never crash.
    """
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
    except Exception as exc:  # noqa: BLE001 — never break prompt rendering
        log.warning("known-plans block unavailable for %s: %s", pid, exc)
        return ""


def _build_prompt(llm_input: dict[str, Any]) -> str:
    """Build the full prompt from template and input.

    Uses str.replace() instead of str.format() because the template contains
    JSON examples with literal braces that would confuse format().
    """
    template = _load_prompt_template()
    ctx = llm_input.get("property_context", {})
    replacements = {
        "{property_name}": ctx.get("property_name", "Unknown") or "Unknown",
        "{city}": ctx.get("city", "") or "",
        "{state}": ctx.get("state", "") or "",
        "{pmc}": ctx.get("pmc", "") or "",
        "{total_units}": str(ctx.get("total_units") or "unknown"),
        "{website}": ctx.get("website", "") or "",
        "{content_type}": llm_input.get("content_type", "HTML"),
        "{trimmed_content}": llm_input.get("trimmed_content", ""),
        "{known_floor_plans}": _resolve_known_plans_block(ctx),
    }
    result = template
    for placeholder, value in replacements.items():
        result = result.replace(placeholder, value)
    return result


def prepare_llm_input(
    page_html: str,
    api_responses: list[dict],
    property_context: dict,
) -> dict[str, Any]:
    """Prepare the LLM input from page HTML and captured API responses.

    Returns a dict with keys: prompt, content_type, trimmed_content, property_context.
    """
    import json

    # Trim HTML
    trimmed_html = _trim_html(page_html) if page_html else ""
    # Cap at ~15K tokens (~60KB text)
    max_chars = 60_000
    if len(trimmed_html) > max_chars:
        trimmed_html = trimmed_html[:max_chars]

    # Rank and select best API responses
    top_apis = _rank_api_responses(api_responses)

    # Build content
    parts: list[str] = []
    content_type = "HTML"

    if top_apis:
        content_type = "HTML + API JSON"
        for i, api in enumerate(top_apis, 1):
            url = api.get("url", "unknown")
            body = api.get("body", {})
            body_str = json.dumps(body, indent=2) if isinstance(body, (dict, list)) else str(body)
            # Cap each API response
            if len(body_str) > 10_000:
                body_str = body_str[:10_000] + "\n... (truncated)"
            parts.append(f"=== API Response #{i}: {url} ===\n{body_str}")

    if trimmed_html:
        parts.append(f"=== Page HTML ===\n{trimmed_html}")

    trimmed_content = "\n\n".join(parts) if parts else "(no content available)"

    return {
        "trimmed_content": trimmed_content,
        "content_type": content_type,
        "property_context": property_context,
    }


async def extract_with_llm(
    llm_input: dict[str, Any],
    property_id: str = "unknown",
) -> tuple[list[dict], dict, str, dict[str, Any] | None]:
    """Run Tier 4 LLM extraction.

    Args:
        llm_input: Output from ``prepare_llm_input()``.
        property_id: Canonical property ID used for interaction logging.

    Returns:
        Tuple of (units, profile_hints, raw_response_text, interaction_record).
        ``interaction_record`` is ``None`` when no API call was made (provider
        error before the call) and a dict otherwise — pass it to the caller
        to include in the per-property LLM report.
    """
    prompt = _build_prompt(llm_input)

    # System prompt for structured extraction
    system = (
        "You are a real estate data extraction agent. Return ONLY valid JSON. No markdown, no commentary."
    )

    try:
        from llm.factory import get_text_provider

        provider = get_text_provider()
    except Exception as exc:
        log.error("Failed to get LLM provider: %s", exc)
        return [], {}, f"provider_error: {exc}", None

    # ── Time the API call and capture interaction data ─────────────────────
    t0 = time.monotonic()
    timestamp = datetime.now(UTC).isoformat()
    raw_response = ""
    error_msg: str | None = None

    try:
        raw_response = await provider.complete(system, prompt, max_tokens=4096)
    except Exception as exc:
        log.error("LLM call failed: %s", exc)
        error_msg = str(exc)
        raw_response = f"llm_error: {exc}"

    latency_ms = int((time.monotonic() - t0) * 1000)

    # Read token usage captured by the provider after the API call.
    usage: dict[str, Any] = getattr(provider, "_last_usage", {})
    tokens_in = int(usage.get("input_tokens", 0))
    tokens_out = int(usage.get("output_tokens", 0))
    model = str(usage.get("model", "unknown"))
    prov_name = str(usage.get("provider", "unknown"))

    # Build interaction record for cost accounting.
    try:
        from llm.interaction_logger import make_interaction

        interaction: dict[str, Any] | None = make_interaction(
            property_id=property_id,
            tier="TIER_6_LLM",
            call_type="text",
            provider=prov_name,
            model=model,
            system_prompt=system,
            user_prompt=prompt,
            raw_response=raw_response,
            tokens_input=tokens_in,
            tokens_output=tokens_out,
            latency_ms=latency_ms,
            timestamp=timestamp,
            success=error_msg is None,
            error=error_msg,
        )
    except Exception as exc:
        log.warning("Failed to build LLM interaction record: %s", exc)
        interaction = None

    if error_msg:
        return [], {}, raw_response, interaction

    parsed = _parse_llm_response(raw_response)
    raw_units = parsed.get("units", [])
    hints = parsed.get("profile_hints", {})

    if not isinstance(raw_units, list):
        raw_units = []
    if not isinstance(hints, dict):
        hints = {}

    units = _normalize_units(raw_units)
    log.info(
        "Tier 4 LLM extracted %d units (raw: %d) | tokens=%d+%d | cost=$%.5f | latency=%dms",
        len(units),
        len(raw_units),
        tokens_in,
        tokens_out,
        (interaction or {}).get("cost_usd", 0.0),
        latency_ms,
    )

    return units, hints, raw_response, interaction
