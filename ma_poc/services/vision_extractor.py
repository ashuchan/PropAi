"""
Tier 5 — Vision LLM extraction for the production pipeline.

Called when Tier 4 (text LLM) also fails — uses screenshot of the rendered page.

Phase: claude-scrapper-arch.md Step 2.2
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import UTC, datetime
from typing import Any

log = logging.getLogger(__name__)

_VISION_PROMPT = """You are a real estate data extraction specialist.
Extract unit-level apartment availability data from the provided screenshot(s)
of a property website.

PROPERTY CONTEXT:
- Name: {property_name}
- Website: {website}

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
      "confidence": 0.0-1.0
    }}
  ],
  "profile_hints": {{
    "api_urls_with_data": [],
    "json_paths": {{}},
    "css_selectors": {{
      "container": "CSS selector for the repeating unit/floor-plan card",
      "rent": "CSS selector for rent within the container",
      "sqft": "CSS selector for sqft",
      "bedrooms": "CSS selector for bedroom count",
      "availability_date": "CSS selector for date"
    }},
    "platform_guess": "entrata|rentcafe|appfolio|sightmap|knock|yardi|custom|null",
    "field_mapping_notes": "Free text: describe what visual layout elements contain the data"
  }}
}}

RULES:
- Extract ALL visible units, not just a sample.
- If data is floor-plan-level (not unit-level), extract floor plans.
- For rent ranges like "$1,200 - $1,500", set market_rent_low=1200, market_rent_high=1500.
- For CSS selectors: describe what visual elements you see that could be targeted.
- confidence: 1.0 = certain, 0.7 = likely correct, <0.5 = guessing.
- If no units are visible, return {{"units": [], "profile_hints": {{...}}}} with notes.
"""


def _parse_vision_response(text: str) -> dict[str, Any]:
    """Parse vision LLM response, handling various formats."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    fence_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if fence_match:
        try:
            return json.loads(fence_match.group(1))
        except json.JSONDecodeError:
            pass

    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            pass

    return {"units": [], "profile_hints": {}}


async def extract_with_vision(
    screenshot: bytes,
    property_context: dict,
    cropped_sections: list[bytes] | None = None,
    property_id: str = "unknown",
) -> tuple[list[dict], dict, str, dict[str, Any] | None]:
    """Run Tier 5 Vision extraction.

    Args:
        screenshot: Full-page screenshot PNG bytes.
        property_context: Dict with property_name, website, etc.
        cropped_sections: Optional list of cropped section screenshots.
        property_id: Canonical property ID used for interaction logging.

    Returns:
        Tuple of (units, profile_hints, raw_response_text, interaction_record).
        ``interaction_record`` is ``None`` when no API call was made.
    """
    prompt = _VISION_PROMPT.format(
        property_name=property_context.get("property_name", "Unknown"),
        website=property_context.get("website", ""),
    )

    images: list[bytes] = []
    if cropped_sections:
        images.extend(cropped_sections)
    else:
        images.append(screenshot)

    try:
        from llm.factory import get_vision_provider
        provider = get_vision_provider()
    except Exception as exc:
        log.error("Failed to get vision provider: %s", exc)
        return [], {}, f"provider_error: {exc}", None

    # ── Time the API call and capture interaction data ─────────────────────
    t0 = time.monotonic()
    timestamp = datetime.now(UTC).isoformat()
    result: dict[str, Any] = {}
    error_msg: str | None = None

    try:
        result = await provider.extract_from_images(images, prompt, max_tokens=4096)
    except Exception as exc:
        log.error("Vision LLM call failed: %s", exc)
        error_msg = str(exc)

    latency_ms = int((time.monotonic() - t0) * 1000)

    # Read token usage captured by the provider after the API call.
    usage: dict[str, Any] = getattr(provider, "_last_usage", {})
    tokens_in  = int(usage.get("input_tokens",  0))
    tokens_out = int(usage.get("output_tokens", 0))
    model      = str(usage.get("model",    "unknown"))
    prov_name  = str(usage.get("provider", "unknown"))

    raw_response_str = json.dumps(result) if isinstance(result, dict) else str(result)

    # Build interaction record for cost accounting.
    try:
        from llm.interaction_logger import make_interaction
        interaction: dict[str, Any] | None = make_interaction(
            property_id=property_id,
            tier="TIER_7_VISION",
            call_type="vision",
            provider=prov_name,
            model=model,
            system_prompt="(vision — no separate system prompt)",
            user_prompt=prompt,
            raw_response=raw_response_str,
            tokens_input=tokens_in,
            tokens_output=tokens_out,
            latency_ms=latency_ms,
            timestamp=timestamp,
            success=error_msg is None,
            error=error_msg,
        )
    except Exception as exc:
        log.warning("Failed to build vision interaction record: %s", exc)
        interaction = None

    if error_msg:
        return [], {}, f"vision_error: {error_msg}", interaction

    # result is already parsed as dict by the provider
    if isinstance(result, dict):
        raw_units = result.get("units", [])
        hints = result.get("profile_hints", {})
    else:
        raw_text = str(result)
        parsed = _parse_vision_response(raw_text)
        raw_units = parsed.get("units", [])
        hints = parsed.get("profile_hints", {})

    if not isinstance(raw_units, list):
        raw_units = []
    if not isinstance(hints, dict):
        hints = {}

    # Reuse normalization from llm_extractor
    from services.llm_extractor import _normalize_units

    units = _normalize_units(raw_units)
    log.info(
        "Tier 5 Vision extracted %d units (raw: %d) | tokens=%d+%d | cost=$%.5f | latency=%dms",
        len(units), len(raw_units), tokens_in, tokens_out,
        (interaction or {}).get("cost_usd", 0.0), latency_ms,
    )

    return units, hints, raw_response_str, interaction


# ── Navigation Discovery (Phase 4.5) ─────────────────────────────────────────

_NAV_PROMPT: str | None = None

def _load_nav_prompt() -> str:
    """Load navigation discovery prompt from config/prompts/."""
    global _NAV_PROMPT
    if _NAV_PROMPT is not None:
        return _NAV_PROMPT
    import pathlib
    prompt_path = pathlib.Path(__file__).resolve().parent.parent / "config" / "prompts" / "navigation_discovery.txt"
    if prompt_path.exists():
        _NAV_PROMPT = prompt_path.read_text(encoding="utf-8")
    else:
        _NAV_PROMPT = (
            "You are analyzing a screenshot of an apartment property website. "
            "Identify navigation actions (click buttons/tabs, or navigate to URLs) "
            "that would lead to apartment unit availability and pricing data. "
            "Return JSON: {{\"suggestions\": [{{\"action\": \"click\"|\"navigate\", "
            "\"selector\": \"CSS selector\", \"url\": \"URL\", \"text\": \"label\", "
            "\"reasoning\": \"why\"}}], \"page_analysis\": \"description\"}}"
        )
    return _NAV_PROMPT


async def suggest_navigation(
    screenshot: bytes,
    property_context: dict,
    property_id: str = "unknown",
) -> list[dict]:
    """Use vision LLM to suggest navigation actions for finding unit data.

    Returns a list of action dicts:
      [{"action": "click", "selector": "...", "text": "..."}, ...]
    or [{"action": "navigate", "url": "...", "text": "..."}, ...]
    """
    prompt_template = _load_nav_prompt()
    prompt = prompt_template.format(
        property_name=property_context.get("property_name", "Unknown"),
        website=property_context.get("website", ""),
    )

    try:
        from llm.factory import get_vision_provider
        provider = get_vision_provider()
    except Exception as exc:
        log.error("Failed to get vision provider for navigation: %s", exc)
        return []

    t0 = time.monotonic()
    result: dict[str, Any] = {}
    try:
        result = await provider.extract_from_images([screenshot], prompt, max_tokens=2048)
    except Exception as exc:
        log.error("Navigation LLM call failed: %s", exc)
        return []

    latency_ms = int((time.monotonic() - t0) * 1000)

    # Parse the response
    if isinstance(result, dict):
        suggestions = result.get("suggestions", [])
    else:
        parsed = _parse_vision_response(str(result))
        suggestions = parsed.get("suggestions", [])

    if not isinstance(suggestions, list):
        suggestions = []

    # Filter to valid actions only
    valid = []
    for s in suggestions[:5]:
        if not isinstance(s, dict):
            continue
        action = s.get("action", "")
        if action == "click" and s.get("selector"):
            valid.append(s)
        elif action == "navigate" and s.get("url"):
            valid.append(s)

    log.info(
        "Navigation discovery: %d suggestions | latency=%dms | property=%s",
        len(valid), latency_ms, property_id,
    )
    return valid
