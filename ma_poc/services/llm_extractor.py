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
from typing import Any

from bs4 import BeautifulSoup

from ma_poc.services.llm_prompt import (
    UNIT_SIGNAL_KEYS as _UNIT_SIGNAL_KEYS,
    normalize_units as _normalize_units,
    parse_llm_response as _parse_llm_response,
    rank_api_responses as _rank_api_responses,
)

log = logging.getLogger(__name__)

# Load prompt template
_PROMPT_PATH = Path(__file__).resolve().parent.parent / "config" / "prompts" / "tier4_extraction.txt"


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


def clean_string_value_dict(d: Any) -> dict[str, str]:
    """Return a ``dict[str, str]`` with all non-string-valued entries dropped.

    The Pydantic models that consume LLM-emitted dicts (``ApiEndpoint.json_paths``,
    ``LlmFieldMapping.json_paths``) declare ``dict[str, str]`` strictly. The LLM
    routinely returns ``null`` for fields it could not map (``"lease_term": null``).
    Without this filter a single null value crashes the writer with Pydantic
    ``string_type`` validation, the runner's outer ``except Exception`` swallows
    it as a warning, and the property's profile is silently never updated —
    breaking the self-learning loop. See profile_updater.update_profile_after_extraction.
    """
    if not isinstance(d, dict):
        return {}
    return {k: v.strip() for k, v in d.items() if isinstance(v, str) and v.strip()}


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


def _resolve_prior_observations_block(property_context: dict[str, Any]) -> str:
    """Render the PRIOR OBSERVATIONS preamble for this property.

    profile_updater stamps the LLM's free-text field_mapping_notes onto
    ``profile.llm_artifacts.field_mapping_notes`` after every successful
    extraction. The adapter forwards the string into ``property_context``
    under ``prior_observations``; we render it here as a short block so
    the next LLM call sees what previous calls already learned about
    the site's organisation. Empty string when not present so cold
    profiles emit no header.
    """
    notes = property_context.get("prior_observations")
    if not isinstance(notes, str) or not notes.strip():
        return ""
    return (
        "PRIOR OBSERVATIONS (from previous successful extractions on this "
        "property — use these to converge faster, but verify they still "
        "apply to the current page):\n"
        f"  {notes.strip()}\n"
    )


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
        "{prior_observations}": _resolve_prior_observations_block(ctx),
    }
    result = template
    for placeholder, value in replacements.items():
        result = result.replace(placeholder, value)
    return result


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

    # The LLM emits ``null`` for fields it could not map. The downstream
    # ``ApiEndpoint`` / ``LlmFieldMapping`` writers declare ``json_paths:
    # dict[str, str]`` strictly. Clean here so cached on-disk artifacts
    # are also clean (replay matchers and drift comparators read this dict).
    if "json_paths" in hints:
        hints["json_paths"] = clean_string_value_dict(hints.get("json_paths"))

    # Capture the top-level ``property_amenities`` array the prompt asks
    # for. The previous return contract dropped this entirely — every
    # property paid for the LLM call, the LLM extracted property-level
    # amenities, and the data was discarded before the adapter saw it.
    # Folding it into the hints dict keeps the existing tuple arity
    # (callers read other keys via ``hints.get(...)``).
    prop_amenities = parsed.get("property_amenities")
    if isinstance(prop_amenities, list):
        hints["property_amenities"] = [
            str(a).strip() for a in prop_amenities if isinstance(a, str) and a.strip()
        ]

    # Stamp the prompt template hash so profile_updater can persist it
    # for drift detection across releases — when this hash changes we
    # know prior extractions ran against a different prompt and any
    # cached field-mapping artefacts may need to be re-validated.
    try:
        import hashlib as _hashlib
        hints.setdefault(
            "extraction_prompt_hash",
            _hashlib.sha256(prompt.encode("utf-8", errors="replace")).hexdigest()[:16],
        )
    except Exception:
        pass

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
) -> tuple[list[dict], dict | None, bool, dict[str, Any] | None]:
    """Analyze a SINGLE API response with LLM to extract units and learn field mappings.

    Args:
        api_response: Dict with "url" and "body" keys from network interception.
        property_context: Dict with property_name, website.
        property_id: Canonical property ID for interaction logging.

    Returns:
        Tuple of (units, hints_dict, is_noise, interaction_record).

        ``hints_dict`` carries everything the LLM volunteered about this
        API response — both the field-mapping triplet (when units were
        found) and ancillary signals that the prompt now asks for:

          - When units found:
              ``api_url_pattern``, ``json_paths``, ``response_envelope``
              (the persistable mapping the cascade replays next run)
              + optional ``navigation_hint``, ``platform_guess``,
              ``property_amenities``, ``api_schema_signature``.

          - When ``is_noise=True``:
              ``noise_reason`` (free text describing what the API
              actually contains) and any ``navigation_hint``,
              ``platform_guess``, ``property_amenities`` that
              the LLM still managed to produce while diagnosing the
              noise. Previously this dict was ``None`` and we lost
              every noise diagnostic except the boolean.

        ``None`` only when nothing learnable came back at all.
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
    prompt = prompt.replace("{prior_observations}", _resolve_prior_observations_block(property_context))

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

    # Helper: extract optional ancillary signals every API analysis
    # response is now allowed to volunteer (prompt update). Always safe
    # to call — returns whatever made it through validation.
    def _ancillary_signals(p: dict) -> dict[str, Any]:
        out: dict[str, Any] = {}
        nav_hint = p.get("navigation_hint")
        if isinstance(nav_hint, str) and nav_hint.strip():
            out["navigation_hint"] = nav_hint.strip()
        plat = p.get("platform_guess")
        if isinstance(plat, str) and plat.strip() and plat.strip().lower() not in ("null", "none"):
            out["platform_guess"] = plat.strip()
        prop_amen = p.get("property_amenities")
        if isinstance(prop_amen, list):
            cleaned = [a.strip() for a in prop_amen if isinstance(a, str) and a.strip()]
            if cleaned:
                out["property_amenities"] = cleaned
        return out

    has_unit_data = parsed.get("has_unit_data", False)
    is_noise = not has_unit_data

    if is_noise:
        noise_reason = parsed.get("noise_reason") or "no_unit_data"
        if not isinstance(noise_reason, str):
            noise_reason = "no_unit_data"
        log.info("API analysis: %s is noise (%s) | latency=%dms", api_url[:80], noise_reason, latency_ms)
        # Build a noise-side hints dict so the adapter can flow
        # noise_reason into BlockedEndpoint.reason and observe any
        # ancillary signals the LLM still produced (a navigation_hint
        # while diagnosing noise is real signal).
        noise_hints: dict[str, Any] = {"noise_reason": noise_reason}
        noise_hints.update(_ancillary_signals(parsed))
        return [], noise_hints if noise_hints else None, True, interaction

    # Extract units
    raw_units = parsed.get("units", [])
    if not isinstance(raw_units, list):
        raw_units = []
    units = _normalize_units(raw_units)

    # Build the unified hints dict. The mapping triplet (api_url_pattern
    # / json_paths / response_envelope) is what the cascade replays
    # deterministically on subsequent runs. The ancillary keys flow
    # through the adapter into ``_llm_hints`` so profile_updater can
    # persist them — these used to be silently discarded because the
    # second tuple element was a mapping-only dict.
    #
    # PR 1 (2026-05-10): always populate ``api_url_pattern`` when units
    # were extracted, regardless of whether the LLM articulated
    # ``json_paths``. Without this, the persistence layer never sees
    # ``api_url_pattern`` (the classifier requires it for kind="mapping")
    # and 38 of 41 daily LLM-API winners silently lose their mapping. The
    # downstream surfacing site (generic.py) and persistence layer
    # (profile_updater.save_llm_field_mapping) accept degraded mappings
    # behind ENABLE_DEGRADED_MAPPING_PERSIST so the cascade has a URL to
    # try even when the per-field paths weren't articulated.
    # The LLM emits ``null`` for fields it could not map. Drop those before the
    # mapping reaches the persistence layer — ``LlmFieldMapping.json_paths``
    # is ``dict[str, str]`` strict and a single null crashes the writer (and
    # thereby the whole self-learning loop for the property).
    json_paths = clean_string_value_dict(parsed.get("json_paths"))
    response_envelope = parsed.get("response_envelope", "")
    if not isinstance(response_envelope, str):
        response_envelope = ""

    hints: dict[str, Any] = {
        "api_url_pattern": api_url,
        "json_paths": json_paths,
        "response_envelope": response_envelope,
    }

    hints.update(_ancillary_signals(parsed))

    # API-side drift signature: SHA-256[:16] of the body envelope shape.
    # profile_updater stamps this onto ``profile.llm_artifacts.api_schema_signature``
    # so a future run that sees a different signature on the same URL
    # knows the API contract changed and any cached mapping needs
    # re-validation.
    try:
        from ma_poc.models.source import envelope_hash_of as _eh
        sig = _eh(api_response.get("body"))
        if sig:
            hints["api_schema_signature"] = sig[:16]
    except Exception:
        pass

    hints_out: dict | None = hints if hints else None

    log.info(
        "API analysis: %s → %d units, mapping=%s, nav_hint=%s | tokens=%d+%d | latency=%dms",
        api_url[:80],
        len(units),
        "yes" if hints.get("json_paths") else "no",
        "yes" if hints.get("navigation_hint") else "no",
        tokens_in,
        tokens_out,
        latency_ms,
    )
    return units, hints_out, False, interaction


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
        Tuple of (units, hints_dict, interaction_record).
        ``hints_dict`` is the broader DOM-LLM envelope when populated:
        ``{"css_selectors": {...}, "navigation_hint": str, "platform_guess":
        str, "api_urls_with_data": list[str], "property_amenities": list[str],
        "dom_structure_hash": str}``. ``None`` when the LLM produced no
        selectors AND no other learnable signals.
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
    prompt = prompt.replace("{prior_observations}", _resolve_prior_observations_block(property_context))

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
    if not isinstance(css_selectors, dict):
        css_selectors = {}

    # Build the broader hints envelope. Anything the prompt promised to
    # return becomes part of this dict so the adapter and profile_updater
    # can persist it. Previously only ``css_selectors`` survived; the new
    # signals (navigation_hint / platform_guess / api_urls_with_data /
    # property_amenities) used to be silently dropped because the return
    # tuple shape exposed nothing else.
    hints: dict[str, Any] = {}
    if css_selectors.get("container"):
        hints["css_selectors"] = css_selectors

    nav_hint = parsed.get("navigation_hint")
    if isinstance(nav_hint, str) and nav_hint.strip():
        hints["navigation_hint"] = nav_hint.strip()

    plat = parsed.get("platform_guess")
    if isinstance(plat, str) and plat.strip() and plat.strip().lower() not in ("null", "none"):
        hints["platform_guess"] = plat.strip()

    api_urls = parsed.get("api_urls_with_data")
    if isinstance(api_urls, list):
        clean_urls = [u.strip() for u in api_urls if isinstance(u, str) and u.strip()]
        if clean_urls:
            hints["api_urls_with_data"] = clean_urls

    prop_amenities = parsed.get("property_amenities")
    if isinstance(prop_amenities, list):
        cleaned_amen = [
            a.strip() for a in prop_amenities if isinstance(a, str) and a.strip()
        ]
        if cleaned_amen:
            hints["property_amenities"] = cleaned_amen

    # Drift-detection signal: stable hash of the DOM section we just
    # analysed. profile_updater stores it on the profile; on the next
    # run a different hash means the page shape changed and any
    # learned selectors should be re-validated rather than blindly trusted.
    try:
        import hashlib as _hashlib
        hints["dom_structure_hash"] = _hashlib.sha256(
            dom_html.encode("utf-8", errors="replace")
        ).hexdigest()[:16]
    except Exception:
        pass

    hints_out: dict | None = hints if hints else None

    log.info(
        "DOM analysis: %s → %d units, selectors=%s, nav_hint=%s, platform=%s | tokens=%d+%d | latency=%dms",
        page_url[:80],
        len(units),
        "yes" if hints.get("css_selectors") else "no",
        "yes" if hints.get("navigation_hint") else "no",
        hints.get("platform_guess") or "—",
        tokens_in,
        tokens_out,
        latency_ms,
    )
    return units, hints_out, interaction


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

        # Amenities + concessions — these used to be requested in the
        # prompt but never replayed, so the mapping was effectively
        # half-functional. Per-unit amenities feed the new
        # ``units.amenities`` JSON column; concession_* feeds the
        # existing ``units.concessions`` JSON column.
        amen_path = json_paths.get("amenities", "")
        if amen_path:
            amen_val = _get_nested(item, amen_path)
            if isinstance(amen_val, list):
                unit["amenities"] = [str(a) for a in amen_val if a]
            elif isinstance(amen_val, str) and amen_val.strip():
                # Some APIs return a comma-separated string instead of a list.
                unit["amenities"] = [s.strip() for s in amen_val.split(",") if s.strip()]

        ctext_path = json_paths.get("concession_text", "")
        if ctext_path:
            unit["concession_text"] = _get_nested(item, ctext_path)

        cval_path = json_paths.get("concession_value", "")
        if cval_path:
            unit["concession_value"] = _get_nested(item, cval_path)
            # Concession came from a dedicated API field — record source.
            unit["concession_source"] = "api_field"

        # Default confidence for mapping-based extraction
        unit["confidence"] = 0.85

        units.append(unit)

    normalized = _normalize_units(units)
    if normalized:
        log.info("apply_saved_mapping: produced %d units from %d items", len(normalized), len(data))
    return normalized
