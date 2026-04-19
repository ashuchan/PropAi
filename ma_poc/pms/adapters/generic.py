"""
Generic fallback adapter.

This adapter contains what remains of the current cascade from the main scraper
after PMS-specific branches (widget filter, map parser, API probe) are moved
into their respective adapters.

Research log
------------
Web sources consulted:
  - Internal: scripts main scraper parse_api_responses() (lines 503-664)
  - Internal: scripts main scraper extract_embedded_json() (lines 1229-1363)
  - Internal: scripts main scraper parse_jsonld() (lines 926-1008)
  - Internal: scripts main scraper parse_dom() (lines 1012-1176)
Real payloads inspected (from data/runs/*/raw_api/):
  - Multiple properties with various API shapes (Yardi /api/v1/, /api/v3/,
    Knock doorway-api, custom REST endpoints)
  - 12617 (Stoney Brook) — community_info endpoint (community-level only)
  - 254976 (San Artes) — gounion property status endpoint (property metadata)
Key findings:
  - Generic parser must handle 50+ key name variants for unit fields
  - Response envelopes vary: direct list[], {objects: [...]}, {data: {units: [...]}},
    {response: {floorplans: [...]}}, {results: [...]}
  - LLM/Vision tiers only run for pms=="unknown"; detected PMS failures skip LLM
  - Ported from parse_api_responses() with PMS-specific branches removed
"""
from __future__ import annotations

import re as _re
from typing import TYPE_CHECKING, Any

# Used by the Option C relaxed-LLM gate to sanity-check HTML has enough
# rent-ish content to be worth an LLM call even when the detected PMS
# adapter already returned empty.
_re_strip_script = _re.compile(r"<script.*?</script>|<style.*?</style>",
                                _re.IGNORECASE | _re.DOTALL)
_re_strip_tag = _re.compile(r"<[^>]+>")
_re_rent = _re.compile(r"\$\s?\d{3,4}(?:[,.]\d{3})?(?:/mo|\s*/\s*month)?",
                        _re.IGNORECASE)

from ma_poc.pms.adapters._daily_runner_parsers import (
    parse_api_responses as _dr_parse_api_responses,
    parse_sightmap_payload as _dr_parse_sightmap,
)
from ma_poc.pms.adapters._html_extract import (
    extract_embedded_blobs_from_html,
    extract_jsonld_from_html,
    extract_units_from_dom,
)
from ma_poc.pms.adapters._parsing import (
    bed_label_from,
    format_rent_range,
    get_field,
    make_unit_dict,
    money_to_int,
    rent_in_sanity_range,
)
from ma_poc.pms.adapters.base import AdapterContext, AdapterResult

if TYPE_CHECKING:
    from playwright.async_api import Page


def _find_unit_list(body: Any) -> list[dict[str, Any]]:
    """Attempt to find a list of unit/floorplan dicts in an API response body.

    Searches multiple envelope shapes: direct list, dict with known keys,
    one level of nesting (data.units, response.floorplans, etc.).
    """
    _LIST_KEYS = (
        "floorPlans", "floor_plans", "FloorPlans", "floorplans",
        "units", "apartments", "availabilities",
        "results", "items", "listings",
    )

    if isinstance(body, list) and body and isinstance(body[0], dict):
        return body

    if isinstance(body, dict):
        for k in _LIST_KEYS:
            v = body.get(k)
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v
        # One level deeper
        for outer in ("data", "response", "result", "body"):
            nested = body.get(outer)
            if isinstance(nested, dict):
                for k in _LIST_KEYS:
                    v = nested.get(k)
                    if isinstance(v, list) and v and isinstance(v[0], dict):
                        return v
            # response might be a list directly
            if isinstance(nested, list) and nested and isinstance(nested[0], dict):
                return nested

    return []


async def _get_page_html(page: Any, ctx: AdapterContext) -> str | None:
    """Extract raw HTML from either a live Playwright page or fetch_result.body.

    Jugnu adapters may receive either a real Page (legacy ``scrape()`` path)
    or ``page=None`` with ``ctx.fetch_result.body`` populated by L1. Both
    should be usable; prefer the live page (post-JS-render content) and
    fall back to the fetch body (raw server HTML).
    """
    # Prefer live page content — it reflects post-render DOM.
    if page is not None and hasattr(page, "content"):
        try:
            content = await page.content()
            if content:
                return content
        except Exception:
            pass

    # Fall back to the raw fetch body (bytes or str).
    fr = getattr(ctx, "fetch_result", None)
    if fr is None:
        return None
    body = getattr(fr, "body", None)
    if body is None:
        return None
    if isinstance(body, bytes):
        try:
            return body.decode("utf-8", errors="replace")
        except Exception:
            return None
    if isinstance(body, str):
        return body
    return None


def _has_unit_signals(items: list[dict[str, Any]]) -> bool:
    """Check if a list of dicts has enough unit/floorplan signals to be worth parsing."""
    if not items:
        return False
    _SIGNAL_KEYS = {
        "rent", "minRent", "maxRent", "min_rent", "max_rent",
        "price", "askingRent", "monthlyRent", "baseRent",
        "bedrooms", "beds", "bedRooms", "bed", "sqft", "squareFeet",
        "square_footage", "sq_ft", "minimumSquareFeet",
        "no_of_bedroom", "unitNumber", "unit_number", "unitId", "unit_id",
        "floorPlanName", "floor_plan_name", "floorplan_name", "floorplan-name",
        "availableDate", "available_date", "availableCount",
        "minimumRent", "maximumRent", "minimumMarketRent", "maximumMarketRent",
        "rentRange", "depositAmount", "numberOfUnitsDisplay",
    }
    sample_keys = set(items[0].keys())
    return len(sample_keys & _SIGNAL_KEYS) >= 2


def parse_generic_api(items: list[dict[str, Any]], url: str) -> list[dict[str, str]]:
    """Parse a generic list of unit/floorplan dicts using broad key name matching.

    Ported from the main scraper parse_api_responses() with PMS-specific branches
    removed (all PMS parsers moved to their own adapters).
    """
    units: list[dict[str, str]] = []
    seen: set[str] = set()

    for item in items:
        if not isinstance(item, dict):
            continue

        name = get_field(item, "floorPlanName", "floor_plan_name", "floorplan_name",
                         "floorplan-name", "name", "unitType", "planName")
        beds_str = get_field(item, "bedrooms", "beds", "bedroom_count", "bedRooms",
                             "numBedrooms", "no_of_bedroom", "bd", "bed")
        baths_str = get_field(item, "bathrooms", "baths", "bathroom_count", "bathRooms",
                              "numBathrooms", "no_of_bathroom", "ba", "bath")
        sqft_str = get_field(item, "sqft", "squareFeet", "square_feet", "minSqft",
                             "minimumSquareFeet", "size", "area", "square_footage",
                             "sq_ft", "maximumSquareFeet")
        unit_num = get_field(item, "unitNumber", "unit_number", "unitId", "unit_id",
                             "label", "display_unit_number", "id", "unit_name")
        rent_lo_str = get_field(item, "minRent", "rent_min", "min_rent", "startingFrom",
                                "askingRent", "price", "rent", "minimumRent",
                                "minimumMarketRent", "baseRent", "display_price",
                                "monthlyRent", "startingPrice")
        rent_hi_str = get_field(item, "maxRent", "rent_max", "max_rent", "maxAskingRent",
                                "endingAt", "maximumRent", "maximumMarketRent")
        avail_str = get_field(item, "availableCount", "available_count", "numAvailable",
                              "unitsAvailable", "units_available", "availableUnitsCount")
        avail_dt = get_field(item, "availableDate", "available_date", "moveInDate",
                             "moveInReady", "availableOn", "readyDate")
        floor_str = get_field(item, "floor", "floorNumber", "floor_id", "floorId")
        building_str = get_field(item, "building", "buildingName", "building_name")
        deposit_str = get_field(item, "deposit", "securityDeposit", "security_deposit",
                                "depositAmount")
        concession_str = get_field(item, "concession", "special", "promotion",
                                   "specials_description", "specialsDescription")
        plan_type = get_field(item, "floorPlanType", "type", "bedBath", "BedBath")
        status_str = get_field(item, "status", "availability_status", "leaseStatus", "unit_status")

        # Dedup gate: skip if missing ALL of [name, beds, sqft, rent_lo]
        if not any([name, beds_str, sqft_str, rent_lo_str]):
            continue

        # Dedup key
        dedup = unit_num or f"{name}|{beds_str}|{sqft_str}|{rent_lo_str}"
        if dedup in seen:
            continue
        seen.add(dedup)

        beds = int(float(beds_str)) if beds_str else None
        baths = int(float(baths_str)) if baths_str else None
        rent_lo = money_to_int(rent_lo_str)
        rent_hi = money_to_int(rent_hi_str)

        # Rent sanity check
        if not rent_in_sanity_range(rent_lo) or not rent_in_sanity_range(rent_hi):
            continue

        bl = bed_label_from(beds, name)
        if not bl and plan_type:
            bl = plan_type

        units.append(make_unit_dict(
            floor_plan_name=name,
            bed_label=bl,
            bedrooms=str(beds) if beds is not None else "",
            bathrooms=str(baths) if baths is not None else "",
            sqft=sqft_str,
            unit_number=unit_num,
            floor=floor_str,
            building=building_str,
            rent_range=format_rent_range(rent_lo, rent_hi),
            deposit=deposit_str,
            concession=concession_str,
            availability_status=status_str.upper() if status_str else "AVAILABLE",
            available_units=avail_str,
            availability_date=avail_dt,
            source_api_url=url,
            extraction_tier="TIER_1_API",
        ))

    return units


class GenericAdapter:
    """Generic fallback adapter.

    Contains the generic API parser and (when pms=="unknown") the full cascade
    including LLM/Vision tiers. When invoked for a detected PMS that failed,
    LLM/Vision tiers are skipped (controlled by ctx or skip_llm flag).
    """

    pms_name: str = "generic"
    _fingerprints: list[str] = []

    async def extract(self, page: Page, ctx: AdapterContext) -> AdapterResult:
        """Run generic extraction cascade on captured API responses.

        For detected PMS failures (pms != "unknown"), only deterministic tiers run.
        LLM/Vision are reserved for truly unknown sites.

        Emits ``extract.tier_attempted`` for every sub-tier run so the report
        shows exactly which sub-tiers fired, how long they took, and why empty
        ones stopped — the existing single-bucket ``tier_used`` hides all of
        that detail.
        """
        import time as _time
        try:
            from ma_poc.observability.events import EventKind, emit as _emit
        except Exception:
            _emit, EventKind = None, None  # type: ignore[assignment]

        attempts: list[dict[str, Any]] = []

        def _log_attempt(key: str, outcome: str, units: int = 0,
                          reason: str = "", duration_ms: int = 0) -> None:
            entry = {
                "tier_key": key, "outcome": outcome, "units_found": units,
                "reason": reason, "duration_ms": duration_ms,
            }
            attempts.append(entry)
            if _emit is not None and EventKind is not None:
                try:
                    _emit(EventKind.TIER_ATTEMPTED, ctx.property_id, **entry)
                except Exception:
                    pass

        result = AdapterResult(tier_used="TIER_1_API")
        result._tier_attempts = attempts  # type: ignore[attr-defined]
        all_units: list[dict[str, str]] = []
        # Option C gate: default is skip LLM when the detected PMS is not
        # "unknown" (GenericAdapter runs as a fallback for a failed PMS
        # adapter — spending LLM budget on those was originally gated OFF).
        # However the 10-property validation showed 2 FAILED_NO_DATA cases
        # (SightMap, RentCafe) where the detected adapter found nothing but
        # the HTML had visible text + rent signals. The relaxation below
        # re-enables LLM for those — evaluated after we have ``html`` and
        # can inspect its shape.
        skip_llm = ctx.detected.pms != "unknown"

        api_responses: list[dict[str, Any]] = getattr(ctx, "_api_responses", [])

        # Sub-tier 1: narrow generic API parser -----------------------------
        t0 = _time.monotonic()
        for resp in api_responses:
            body = resp.get("body")
            items = _find_unit_list(body)
            if items and _has_unit_signals(items):
                url = resp.get("url", "")
                units = parse_generic_api(items, url)
                if units:
                    all_units.extend(units)
                    result.api_responses.append(resp)
        _narrow_ms = int((_time.monotonic() - t0) * 1000)
        if api_responses:
            _log_attempt(
                "generic:api_narrow",
                "ran_units" if all_units else "ran_empty",
                units=len(all_units),
                reason="" if all_units else "no items matched unit-signal heuristic",
                duration_ms=_narrow_ms,
            )
        else:
            _log_attempt("generic:api_narrow", "skipped",
                         reason="no captured API responses", duration_ms=0)

        # Sub-tier 2: broad parser + host-specific (SightMap/RealPage) -----
        if not all_units and api_responses:
            t0 = _time.monotonic()
            for resp in api_responses:
                url = resp.get("url") or ""
                body = resp.get("body")
                host_units: list[dict[str, str]] = []
                if body is not None and "sightmap.com" in url.lower():
                    try:
                        host_units = _dr_parse_sightmap(body, url) or []
                    except Exception as exc:  # defensive — never break the run
                        result.errors.append(f"sightmap-parse-error: {exc}")
                if host_units:
                    all_units.extend(host_units)
                    result.api_responses.append(resp)
            if not all_units:
                try:
                    broad = _dr_parse_api_responses(list(api_responses)) or []
                except Exception as exc:
                    broad = []
                    result.errors.append(f"daily-runner-parser-error: {exc}")
                if broad:
                    all_units.extend(broad)
                    # parse_api_responses tags each unit with source_api_url;
                    # surface the first as winning_url if we don't have one.
                    if not result.api_responses:
                        first_url = next(
                            (u.get("source_api_url") for u in broad if u.get("source_api_url")),
                            None,
                        )
                        if first_url:
                            for resp in api_responses:
                                if resp.get("url") == first_url:
                                    result.api_responses.append(resp)
                                    break
            _log_attempt(
                "generic:api_broad",
                "ran_units" if all_units else "ran_empty",
                units=len(all_units),
                reason="" if all_units else "broad parser + host-specific found no units",
                duration_ms=int((_time.monotonic() - t0) * 1000),
            )

        if all_units:
            result.units = all_units
            result.winning_url = result.api_responses[0].get("url") if result.api_responses else None
            result.confidence = min(0.85, 0.6 + 0.05 * len(all_units))
            return result

        # ── HTML-based tiers ──────────────────────────────────────────────
        # If neither narrow nor broad API parsers produced units, fall through
        # to the HTML extractors. These run on the raw page HTML (either from
        # a live Playwright page or from fetch_result.body) and cover the SSR
        # / static-site cases where no XHR fires during load.
        html = await _get_page_html(page, ctx)
        if html:
            # Sub-tier 3: JSON-LD
            t0 = _time.monotonic()
            jsonld_units = extract_jsonld_from_html(html, ctx.base_url)
            _log_attempt(
                "generic:jsonld",
                "ran_units" if jsonld_units else "ran_empty",
                units=len(jsonld_units or []),
                reason="" if jsonld_units else "no Apartment/Offer schema in HTML",
                duration_ms=int((_time.monotonic() - t0) * 1000),
            )
            if jsonld_units:
                result.units = jsonld_units
                result.tier_used = "TIER_2_JSONLD"
                result.winning_url = ctx.base_url
                result.confidence = min(0.80, 0.55 + 0.05 * len(jsonld_units))
                return result

            # Sub-tier 4: Embedded JSON / SSR blobs -------------------------
            t0 = _time.monotonic()
            embedded = extract_embedded_blobs_from_html(html)
            if embedded:
                try:
                    embedded_units = _dr_parse_api_responses(embedded) or []
                except Exception as exc:
                    embedded_units = []
                    result.errors.append(f"embedded-parse-error: {exc}")
            else:
                embedded_units = []
            _log_attempt(
                "generic:embedded_json",
                "ran_units" if embedded_units else ("ran_empty" if embedded else "skipped"),
                units=len(embedded_units),
                reason="" if embedded_units else (
                    f"{len(embedded)} SSR blob(s) had no unit signals" if embedded
                    else "no __NEXT_DATA__/__NUXT__/window globals in HTML"
                ),
                duration_ms=int((_time.monotonic() - t0) * 1000),
            )
            if embedded_units:
                result.units = embedded_units
                result.tier_used = "TIER_1_5_EMBEDDED"
                result.winning_url = ctx.base_url
                result.confidence = min(0.80, 0.55 + 0.05 * len(embedded_units))
                return result

            # Sub-tier 5: DOM selector cascade ------------------------------
            # Scans container elements (.unit, .floor-plan, .pricing-card, …)
            # for visible rent + structural signals. Catches static HTML sites
            # where unit data lives in the markup, not in any JSON envelope.
            t0 = _time.monotonic()
            try:
                dom_units = extract_units_from_dom(html, ctx.base_url) or []
            except Exception as exc:
                dom_units = []
                result.errors.append(f"dom-scan-error: {exc}")
            _log_attempt(
                "generic:dom_scan",
                "ran_units" if dom_units else "ran_empty",
                units=len(dom_units),
                reason="" if dom_units else "no DOM container matched rent + structural signals",
                duration_ms=int((_time.monotonic() - t0) * 1000),
            )
            if dom_units:
                result.units = dom_units
                result.tier_used = "TIER_3_DOM"
                result.winning_url = ctx.base_url
                result.confidence = min(0.75, 0.5 + 0.04 * len(dom_units))
                return result
        else:
            _log_attempt("generic:jsonld", "skipped", reason="no HTML body available")
            _log_attempt("generic:embedded_json", "skipped", reason="no HTML body available")
            _log_attempt("generic:dom_scan", "skipped", reason="no HTML body available")

        # Sub-tier 6: LLM extraction --------------------------------------
        # Originally gated ON only for ``pms=unknown``. Option C relaxes
        # that gate: if the detected adapter returned empty BUT the page
        # has enough visible text and rent signals for the LLM to have a
        # shot, we let it run. Rationale: a detected-but-failing adapter
        # means the site shape drifted (or the data lives on a sub-page);
        # the LLM can sometimes recover from the home-page HTML that's
        # right there in front of us.
        if skip_llm and html:
            try:
                _text = _re_strip_script.sub("", html)
                _text = _re_strip_tag.sub(" ", _text)
                _rent_hits = len(_re_rent.findall(html))
                _text_bytes = len(_text.encode("utf-8", errors="ignore"))
                if _text_bytes >= 5000 and _rent_hits >= 1:
                    try:
                        from ma_poc.observability.events import EventKind, emit as _gate_emit
                        _gate_emit(
                            EventKind.LLM_GATE_RELAXED, ctx.property_id,
                            detected_pms=ctx.detected.pms,
                            text_bytes=_text_bytes, rent_signals=_rent_hits,
                            reason="detected_adapter_empty_html_has_signals",
                        )
                    except Exception:
                        pass
                    skip_llm = False
            except Exception:
                pass

        if skip_llm:
            _log_attempt("generic:llm", "skipped",
                         reason=f"detected PMS '{ctx.detected.pms}' — LLM gated off")
            result.errors.append(
                f"Generic fallback found no units for detected PMS '{ctx.detected.pms}'; "
                "LLM/Vision skipped for non-unknown PMS"
            )
            result.confidence = 0.0
            return result

        import os as _os
        if _os.getenv("ENABLE_TIER4_LLM", "true").lower() not in ("1", "true", "yes"):
            _log_attempt("generic:llm", "skipped",
                         reason="ENABLE_TIER4_LLM=false")
            result.errors.append("Generic parser found no units in captured API responses")
            result.confidence = 0.0
            return result

        if not html:
            _log_attempt("generic:llm", "skipped",
                         reason="no HTML body to send to LLM")
            result.errors.append("Generic parser found no units; no HTML for LLM")
            result.confidence = 0.0
            return result

        t0 = _time.monotonic()
        try:
            try:
                from ma_poc.services.llm_extractor import (
                    extract_with_llm, prepare_llm_input,
                )
            except ImportError:
                from services.llm_extractor import (  # type: ignore[no-redef]
                    extract_with_llm, prepare_llm_input,
                )
            property_context = {
                "property_name": "",
                "city": "", "state": "",
                "total_units": ctx.expected_total_units or "",
                "website": ctx.base_url,
            }
            llm_input = prepare_llm_input(html, api_responses, property_context)
            llm_units, hints, _raw, interaction = await extract_with_llm(
                llm_input, property_id=ctx.property_id or "unknown",
            )
            if interaction:
                # Stash the interaction dict so scraper.py surfaces it on
                # result["_llm_interactions"] for cost accounting + reports.
                existing = getattr(result, "_llm_interactions", []) or []
                existing.append(interaction)
                result._llm_interactions = existing  # type: ignore[attr-defined]
            _log_attempt(
                "generic:llm",
                "ran_units" if llm_units else "ran_empty",
                units=len(llm_units or []),
                reason="" if llm_units else "LLM returned no structured units",
                duration_ms=int((_time.monotonic() - t0) * 1000),
            )
            if llm_units:
                result.units = llm_units
                result.tier_used = "TIER_4_LLM"
                result.winning_url = ctx.base_url
                result.confidence = min(0.75, 0.5 + 0.04 * len(llm_units))
                # Surface the LLM-discovered hints so the profile updater can
                # record css_selectors / api_urls_with_data for future runs.
                if hints:
                    result._llm_hints = hints  # type: ignore[attr-defined]
                return result
        except Exception as exc:
            _log_attempt(
                "generic:llm", "errored",
                reason=str(exc)[:200],
                duration_ms=int((_time.monotonic() - t0) * 1000),
            )
            result.errors.append(f"llm-tier-error: {exc}")

        result.confidence = 0.0
        result.errors.append("Generic parser found no units in captured API responses")
        return result

    def static_fingerprints(self) -> list[str]:
        return list(self._fingerprints)
