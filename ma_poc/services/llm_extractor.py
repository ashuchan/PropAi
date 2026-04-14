"""
Tier 4 — LLM extraction service for the production pipeline.

Receives page content (HTML + captured API responses) and returns structured
unit data plus profile hints (CSS selectors, API paths, platform guess).

Phase: claude-scrapper-arch.md Step 2.1
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Optional

from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

# Load prompt template
_PROMPT_PATH = Path(__file__).resolve().parent.parent / "config" / "prompts" / "tier4_extraction.txt"

# Unit-like field names used to rank API responses by relevance
_UNIT_SIGNAL_KEYS = frozenset({
    "rent", "price", "sqft", "bed", "bath", "available", "unit",
    "floor", "plan", "bedroom", "bathroom", "floorplan", "floorPlan",
    "unitNumber", "unit_number", "asking_rent", "market_rent",
    "availability", "availableDate", "available_date",
})


def _load_prompt_template() -> str:
    """Load the Tier 4 extraction prompt template."""
    try:
        return _PROMPT_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        log.warning("Prompt template not found at %s, using inline fallback", _PROMPT_PATH)
        return _FALLBACK_PROMPT


_FALLBACK_PROMPT = """You are a real estate data extraction specialist.
Extract unit-level apartment availability data from the provided website content.

PROPERTY CONTEXT:
- Name: {property_name}
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
      "confidence": 0.0-1.0
    }}
  ],
  "profile_hints": {{
    "api_urls_with_data": [],
    "json_paths": {{}},
    "css_selectors": {{}},
    "platform_guess": null,
    "field_mapping_notes": ""
  }}
}}

RULES:
- Extract ALL available units, not just a sample.
- If data is floor-plan-level (not unit-level), extract floor plans.
- For rent ranges like "$1,200 - $1,500", set market_rent_low=1200, market_rent_high=1500.
- confidence: 1.0 = certain, 0.7 = likely correct, <0.5 = guessing.
"""


def _trim_html(html: str) -> str:
    """Remove non-content tags from HTML. Keep JSON-LD scripts."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup.find_all(["style", "svg", "noscript", "nav", "footer"]):
        tag.decompose()
    # Remove scripts EXCEPT JSON-LD
    for tag in soup.find_all("script"):
        if tag.get("type") != "application/ld+json":
            tag.decompose()
    # Remove cookie/consent banners
    for tag in soup.find_all(attrs={"class": re.compile(r"cookie|consent|gdpr", re.I)}):
        tag.decompose()
    # Try to keep just <main> or largest content div
    main = soup.find("main")
    if main:
        return str(main)
    return soup.get_text("\n", strip=True)


def _rank_api_responses(api_responses: list[dict]) -> list[dict]:
    """Rank API responses by overlap with unit-like field names. Return top 3."""
    if not api_responses:
        return []

    def _score(resp: dict) -> int:
        body = resp.get("body")
        if not body:
            return 0
        text = json.dumps(body) if isinstance(body, (dict, list)) else str(body)
        return sum(1 for key in _UNIT_SIGNAL_KEYS if key in text.lower())

    scored = [(resp, _score(resp)) for resp in api_responses]
    scored.sort(key=lambda x: x[1], reverse=True)
    return [resp for resp, s in scored[:3] if s > 0]


def prepare_llm_input(
    page_html: str,
    api_responses: list[dict],
    property_context: dict,
) -> dict[str, Any]:
    """Prepare the LLM input from page HTML and captured API responses.

    Returns a dict with keys: prompt, content_type, trimmed_content, property_context.
    """
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


def _build_prompt(llm_input: dict[str, Any]) -> str:
    """Build the full prompt from template and input.

    Uses str.replace() instead of str.format() because the template contains
    JSON examples with literal braces that would confuse format().
    """
    template = _load_prompt_template()
    ctx = llm_input.get("property_context", {})
    replacements = {
        "{property_name}": ctx.get("property_name", "Unknown"),
        "{city}": ctx.get("city", ""),
        "{state}": ctx.get("state", ""),
        "{total_units}": str(ctx.get("total_units", "unknown")),
        "{website}": ctx.get("website", ""),
        "{content_type}": llm_input.get("content_type", "HTML"),
        "{trimmed_content}": llm_input.get("trimmed_content", ""),
    }
    result = template
    for placeholder, value in replacements.items():
        result = result.replace(placeholder, value)
    return result


def _parse_llm_response(text: str) -> dict[str, Any]:
    """Parse LLM response, handling markdown fences and other formatting."""
    # Try direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try extracting from markdown code fences
    fence_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if fence_match:
        try:
            return json.loads(fence_match.group(1))
        except json.JSONDecodeError:
            pass

    # Try finding first { to last }
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            pass

    return {"units": [], "profile_hints": {}}


def _normalize_units(raw_units: list[dict]) -> list[dict]:
    """Normalize LLM-extracted units through parse functions."""
    normalized: list[dict] = []
    for u in raw_units:
        unit: dict[str, Any] = {}

        # Unit ID
        unit["unit_id"] = u.get("unit_id") or u.get("unit_number")

        # Floor plan
        unit["floor_plan_name"] = u.get("floor_plan_name") or u.get("floor_plan_type")

        # Bedrooms/bathrooms
        beds = u.get("bedrooms")
        if beds is not None:
            try:
                unit["bedrooms"] = int(float(beds))
            except (ValueError, TypeError):
                unit["bedrooms"] = None
        else:
            unit["bedrooms"] = None

        baths = u.get("bathrooms")
        if baths is not None:
            try:
                unit["bathrooms"] = float(baths)
            except (ValueError, TypeError):
                unit["bathrooms"] = None
        else:
            unit["bathrooms"] = None

        # Sqft
        sqft = u.get("sqft")
        if sqft is not None:
            try:
                unit["sqft"] = int(float(sqft))
            except (ValueError, TypeError):
                unit["sqft"] = None
        else:
            unit["sqft"] = None

        # Rent
        rent_low = u.get("market_rent_low") or u.get("asking_rent")
        rent_high = u.get("market_rent_high") or rent_low
        if rent_low is not None:
            try:
                unit["market_rent_low"] = float(rent_low)
            except (ValueError, TypeError):
                unit["market_rent_low"] = None
        else:
            unit["market_rent_low"] = None

        if rent_high is not None:
            try:
                unit["market_rent_high"] = float(rent_high)
            except (ValueError, TypeError):
                unit["market_rent_high"] = None
        else:
            unit["market_rent_high"] = None

        # Rent sanity bounds ($200 - $50,000)
        for key in ("market_rent_low", "market_rent_high"):
            val = unit.get(key)
            if val is not None and (val < 200 or val > 50_000):
                unit[key] = None

        # Availability
        unit["available_date"] = u.get("available_date")
        status = u.get("availability_status", "UNKNOWN")
        if isinstance(status, str):
            status = status.upper()
            if status not in ("AVAILABLE", "UNAVAILABLE", "WAITLIST", "UNKNOWN"):
                status = "UNKNOWN"
        unit["availability_status"] = status

        # Confidence
        conf = u.get("confidence", 0.7)
        try:
            unit["confidence"] = max(0.0, min(1.0, float(conf)))
        except (ValueError, TypeError):
            unit["confidence"] = 0.5

        # Only include if we have some meaningful data
        if unit.get("unit_id") or unit.get("floor_plan_name") or unit.get("market_rent_low"):
            normalized.append(unit)

    return normalized


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
        "You are a real estate data extraction agent. "
        "Return ONLY valid JSON. No markdown, no commentary."
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
    tokens_in  = int(usage.get("input_tokens",  0))
    tokens_out = int(usage.get("output_tokens", 0))
    model      = str(usage.get("model",    "unknown"))
    prov_name  = str(usage.get("provider", "unknown"))

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
        len(units), len(raw_units), tokens_in, tokens_out,
        (interaction or {}).get("cost_usd", 0.0), latency_ms,
    )

    return units, hints, raw_response, interaction


# ── Targeted LLM analysis functions ──────────────────────────────────────────
# These replace the "send entire page + all APIs" approach with surgical,
# single-response analysis that produces reusable field mappings.

_API_ANALYSIS_PROMPT_PATH = Path(__file__).resolve().parent.parent / "config" / "prompts" / "api_analysis.txt"
_DOM_ANALYSIS_PROMPT_PATH = Path(__file__).resolve().parent.parent / "config" / "prompts" / "dom_analysis.txt"


def _load_api_analysis_prompt() -> str:
    try:
        return _API_ANALYSIS_PROMPT_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        log.warning("api_analysis.txt not found at %s", _API_ANALYSIS_PROMPT_PATH)
        return ""


def _load_dom_analysis_prompt() -> str:
    try:
        return _DOM_ANALYSIS_PROMPT_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        log.warning("dom_analysis.txt not found at %s", _DOM_ANALYSIS_PROMPT_PATH)
        return ""


async def analyze_api_with_llm(
    api_response: dict,
    property_context: dict,
    property_id: str = "unknown",
) -> tuple[list[dict], Optional[dict], bool, dict[str, Any] | None]:
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

    prompt = template.replace("{property_name}", property_context.get("property_name", "Unknown"))
    prompt = prompt.replace("{website}", property_context.get("website", ""))
    prompt = prompt.replace("{api_url}", api_url)
    prompt = prompt.replace("{api_response_json}", body_str)

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
    mapping_dict: Optional[dict] = None
    if isinstance(json_paths, dict) and json_paths:
        mapping_dict = {
            "api_url_pattern": api_url,
            "json_paths": json_paths,
            "response_envelope": response_envelope if isinstance(response_envelope, str) else "",
        }

    log.info(
        "API analysis: %s → %d units, mapping=%s | tokens=%d+%d | latency=%dms",
        api_url[:80], len(units), "yes" if mapping_dict else "no",
        tokens_in, tokens_out, latency_ms,
    )
    return units, mapping_dict, False, interaction


async def analyze_dom_with_llm(
    dom_html: str,
    page_url: str,
    property_context: dict,
    property_id: str = "unknown",
) -> tuple[list[dict], Optional[dict], dict[str, Any] | None]:
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

    prompt = template.replace("{property_name}", property_context.get("property_name", "Unknown"))
    prompt = prompt.replace("{website}", property_context.get("website", ""))
    prompt = prompt.replace("{page_url}", page_url)
    prompt = prompt.replace("{dom_section_html}", dom_html)

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
    selectors_dict: Optional[dict] = None
    if isinstance(css_selectors, dict) and css_selectors.get("container"):
        selectors_dict = css_selectors

    log.info(
        "DOM analysis: %s → %d units, selectors=%s | tokens=%d+%d | latency=%dms",
        page_url[:80], len(units), "yes" if selectors_dict else "no",
        tokens_in, tokens_out, latency_ms,
    )
    return units, selectors_dict, interaction


def apply_saved_mapping(api_response_body: Any, mapping: dict) -> list[dict]:
    """Deterministic extraction using a previously-saved LLM field mapping.

    Navigates the JSON using response_envelope to find the unit list,
    then maps fields using json_paths. Returns [] if the mapping doesn't
    produce valid units (schema may have changed).

    Args:
        api_response_body: The raw JSON body from the API response.
        mapping: Dict with keys "response_envelope", "json_paths".

    Returns:
        List of normalized unit dicts, or [] on failure.
    """
    envelope = mapping.get("response_envelope", "")
    json_paths = mapping.get("json_paths", {})
    if not json_paths:
        return []

    # Navigate to the unit list using the envelope path
    data = api_response_body
    if envelope:
        for key in envelope.split("."):
            if isinstance(data, dict):
                data = data.get(key)
            elif isinstance(data, list) and key.isdigit():
                idx = int(key)
                data = data[idx] if idx < len(data) else None
            else:
                return []
            if data is None:
                return []

    # data should now be a list of unit dicts
    if not isinstance(data, list):
        # Maybe it's a single dict wrapping a list
        if isinstance(data, dict):
            # Try common wrapper keys
            for k in ("units", "floorPlans", "floor_plans", "results", "data", "items"):
                if isinstance(data.get(k), list):
                    data = data[k]
                    break
            else:
                return []
        else:
            return []

    if not data:
        return []

    # Extract fields using json_paths mapping
    units: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue

        unit: dict[str, Any] = {}

        # Helper to navigate dot-separated paths
        def _get_nested(obj: Any, path: str) -> Any:
            if not path:
                return None
            for part in path.split("."):
                if isinstance(obj, dict):
                    obj = obj.get(part)
                elif isinstance(obj, list) and part.isdigit():
                    idx = int(part)
                    obj = obj[idx] if idx < len(obj) else None
                else:
                    return None
            return obj

        # Map each field
        uid_path = json_paths.get("unit_id", "")
        if uid_path:
            unit["unit_id"] = _get_nested(item, uid_path)

        fp_path = json_paths.get("floor_plan_name", "")
        if fp_path:
            unit["floor_plan_name"] = _get_nested(item, fp_path)

        rent_low_path = json_paths.get("rent_low", "")
        if rent_low_path:
            unit["market_rent_low"] = _get_nested(item, rent_low_path)

        rent_high_path = json_paths.get("rent_high", "")
        if rent_high_path:
            unit["market_rent_high"] = _get_nested(item, rent_high_path)

        beds_path = json_paths.get("bedrooms", "")
        if beds_path:
            unit["bedrooms"] = _get_nested(item, beds_path)

        baths_path = json_paths.get("bathrooms", "")
        if baths_path:
            unit["bathrooms"] = _get_nested(item, baths_path)

        sqft_path = json_paths.get("sqft", "")
        if sqft_path:
            unit["sqft"] = _get_nested(item, sqft_path)

        date_path = json_paths.get("available_date", "")
        if date_path:
            unit["available_date"] = _get_nested(item, date_path)

        status_path = json_paths.get("availability_status", "")
        if status_path:
            unit["availability_status"] = _get_nested(item, status_path)

        # Default confidence for mapping-based extraction
        unit["confidence"] = 0.85

        units.append(unit)

    normalized = _normalize_units(units)
    if normalized:
        log.info("apply_saved_mapping: produced %d units from %d items", len(normalized), len(data))
    return normalized
