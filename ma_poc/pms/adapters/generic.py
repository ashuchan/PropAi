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
    is_junk_floor_plan,
    is_junk_unit_number,
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


def _extract_rent_dom_section(html: str, max_bytes: int = 20_000) -> str | None:
    """Return the smallest HTML chunk that contains the site's rent signals.

    We pick the tightest ancestor around rent-looking text rather than
    sending the entire page to the DOM-analysis LLM. This keeps the prompt
    small enough to meet per-call token limits and biases the model toward
    the unit/floor-plan container instead of global layout chrome.

    Strategy:
      1. Prefer ``<main>`` when present — apartment sites almost always put
         availability content there.
      2. Otherwise find the smallest container with 2+ rent-pattern matches.
      3. Cap at ``max_bytes`` so oversized ``<main>`` tags don't blow the
         per-call token budget.
    """
    if not html:
        return None
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return html[:max_bytes]
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        try:
            soup = BeautifulSoup(html, "html.parser")
        except Exception:
            return html[:max_bytes]

    # Strip noise tags.
    for tag in soup.find_all(["script", "style", "svg", "noscript",
                              "nav", "footer", "header", "iframe"]):
        tag.decompose()

    main = soup.find("main")
    if main:
        block = str(main)
        if len(block) > max_bytes:
            block = block[:max_bytes] + "<!-- truncated -->"
        return block

    # Find smallest ancestor holding 2+ rent patterns.
    best: Any = None
    best_len = 10**9
    for el in soup.find_all(True):
        try:
            text = el.get_text(" ", strip=True)
        except Exception:
            continue
        if len(_re_rent.findall(text)) < 2:
            continue
        s = str(el)
        if 500 <= len(s) <= max_bytes and len(s) < best_len:
            best, best_len = el, len(s)

    if best is not None:
        return str(best)

    # Last resort: strip to body, then truncate.
    body = soup.find("body")
    fallback = str(body) if body else str(soup)
    return fallback[:max_bytes]


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

        # Phase 5: junk deny-list. Drops CMS-widget names like
        # "MODULE_CONCESSIONMANAGER" and "[Riedman] Lease Magnet - Pop-Up"
        # that the 2026-04-19 run surfaced as fake units. Also drops
        # unit_number stop-words ("Left", "s", etc).
        if is_junk_floor_plan(name):
            continue
        if is_junk_unit_number(unit_num):
            # Prefer to clear the unit number rather than drop the whole
            # record — the rent/sqft may still be valid floor-plan data.
            unit_num = ""

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
            rent_low=rent_lo,
            rent_high=rent_hi or rent_lo,
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

        # Phase 4: filter out API URLs the profile previously classified as
        # noise. Saves token spend on known-bad endpoints (chatbot configs,
        # analytics pixels, CMS widgets). New noise discoveries also flow
        # back into this list via _llm_analysis_results.
        profile = getattr(ctx, "profile", None)
        blocked_urls: set[str] = set()
        if profile is not None:
            try:
                for be in getattr(profile.api_hints, "blocked_endpoints", []) or []:
                    pat = getattr(be, "url_pattern", None)
                    if pat:
                        blocked_urls.add(str(pat))
            except Exception:
                blocked_urls = set()
        if blocked_urls:
            before = len(api_responses)
            api_responses = [r for r in api_responses
                             if r.get("url", "") not in blocked_urls]
            dropped = before - len(api_responses)
            if dropped:
                _log_attempt(
                    "generic:blocked_filter", "ran_units",
                    units=0,
                    reason=f"dropped {dropped} API(s) from profile.blocked_endpoints",
                )

        # Phase 4 sub-tier 0: deterministic replay of saved LLM mappings.
        # Runs before ANY parser: if a prior run's LLM told us exactly how
        # to extract units from a specific API shape, we can reuse it with
        # zero LLM cost. Falls through to the generic cascade on miss.
        if profile is not None and api_responses:
            t0 = _time.monotonic()
            replayed_units: list[dict[str, Any]] = []
            try:
                saved = list(getattr(profile.api_hints, "llm_field_mappings", []) or [])
            except Exception:
                saved = []
            if saved:
                try:
                    try:
                        from ma_poc.services.llm_extractor import apply_saved_mapping
                    except ImportError:
                        from services.llm_extractor import apply_saved_mapping  # type: ignore[no-redef]
                except ImportError:
                    apply_saved_mapping = None  # type: ignore[assignment]
                if apply_saved_mapping is not None:
                    for mapping in saved:
                        try:
                            pat = getattr(mapping, "api_url_pattern", None) or (
                                mapping.get("api_url_pattern")
                                if isinstance(mapping, dict) else None
                            )
                        except Exception:
                            pat = None
                        if not pat:
                            continue
                        for resp in api_responses:
                            if pat in resp.get("url", ""):
                                mdict = mapping if isinstance(mapping, dict) else {
                                    "api_url_pattern": pat,
                                    "json_paths": getattr(mapping, "json_paths", {}) or {},
                                    "response_envelope": getattr(mapping, "response_envelope", "") or "",
                                }
                                try:
                                    units = apply_saved_mapping(resp.get("body"), mdict) or []
                                except Exception:
                                    units = []
                                if units:
                                    replayed_units.extend(units)
                                    result.api_responses.append(resp)
                                    break
            if replayed_units:
                _log_attempt(
                    "generic:profile_replay", "ran_units",
                    units=len(replayed_units),
                    reason="replayed saved LlmFieldMapping",
                    duration_ms=int((_time.monotonic() - t0) * 1000),
                )
                result.units = replayed_units
                result.tier_used = "TIER_1_PROFILE_MAPPING"
                result.winning_url = (
                    result.api_responses[0].get("url") if result.api_responses
                    else ctx.base_url
                )
                result.confidence = min(0.90, 0.7 + 0.03 * len(replayed_units))
                return result
            _log_attempt(
                "generic:profile_replay",
                "skipped" if not saved else "ran_empty",
                reason=("no saved mappings" if not saved
                        else "saved mappings didn't match any captured API"),
                duration_ms=int((_time.monotonic() - t0) * 1000),
            )

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
            # Phase 5: reject JSON-LD success when the extraction is
            # plan-name-only with no Offer prices. That shape was gating
            # the LLM sub-tiers from running on 8 properties in the
            # 2026-04-19 run — marking them "SUCCESS" with a list of
            # floor-plan labels but no rent/sqft. Better to fall through.
            if jsonld_units:
                has_rent = any(
                    u.get("market_rent_low") or u.get("market_rent_high")
                    or u.get("rent_range") for u in jsonld_units
                )
                has_size = any(u.get("sqft") for u in jsonld_units)
                if not has_rent and not has_size:
                    _log_attempt(
                        "generic:jsonld",
                        "ran_empty",
                        units=len(jsonld_units),
                        reason="JSON-LD had floor-plan names only (no rent/sqft) — falling through",
                        duration_ms=int((_time.monotonic() - t0) * 1000),
                    )
                    jsonld_units = []
            if jsonld_units:
                _log_attempt(
                    "generic:jsonld",
                    "ran_units",
                    units=len(jsonld_units),
                    duration_ms=int((_time.monotonic() - t0) * 1000),
                )
            elif "generic:jsonld" not in {a["tier_key"] for a in attempts}:
                _log_attempt(
                    "generic:jsonld",
                    "ran_empty",
                    reason="no Apartment/Offer schema in HTML",
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

        # Phase 2: shared property context for every LLM call below.
        property_context = {
            "property_name": getattr(ctx, "property_name", "") or "",
            "city":          getattr(ctx, "city", "") or "",
            "state":         getattr(ctx, "state", "") or "",
            "pmc":           getattr(ctx, "pmc", "") or "",
            "total_units":   ctx.expected_total_units or "",
            "website":       ctx.base_url,
        }

        # Import the targeted LLM helpers; fall through cleanly if unavailable
        # so the adapter degrades gracefully (monolithic call still runs).
        try:
            try:
                from ma_poc.services.llm_extractor import (
                    analyze_api_with_llm,
                    analyze_dom_with_llm,
                    extract_with_llm,
                    prepare_llm_input,
                )
            except ImportError:
                from services.llm_extractor import (  # type: ignore[no-redef]
                    analyze_api_with_llm,
                    analyze_dom_with_llm,
                    extract_with_llm,
                    prepare_llm_input,
                )
        except ImportError as exc:
            _log_attempt("generic:llm", "errored", reason=f"llm_extractor import: {exc}")
            result.errors.append(f"llm-import-error: {exc}")
            result.confidence = 0.0
            return result

        # Budget: capped per property so a broken site can't burn unlimited
        # tokens. Mirrors the legacy entrata.py budget (3 API + 1 DOM).
        api_llm_budget = 3
        dom_llm_budget = 1
        llm_interactions: list[dict[str, Any]] = (
            getattr(result, "_llm_interactions", []) or []
        )
        # Self-learning payload surfaced to scraper.py. Shape matches what
        # services.profile_updater.update_profile_after_extraction expects:
        #   - dict (with api_url_pattern) => save as LlmFieldMapping
        #   - "noise:<reason>" string     => add to blocked_endpoints
        llm_analysis_results: dict[str, Any] = {}
        llm_field_mappings: list[dict[str, Any]] = []
        llm_css_selectors: dict[str, Any] | None = None
        llm_navigation_hints: list[str] = []

        # Sub-tier 6a: targeted API analysis ------------------------------
        # For each captured API response with unit-like signals that the
        # deterministic parsers couldn't unwrap, ask the LLM to both
        # extract units AND return json_paths + response_envelope that we
        # can replay deterministically on the next run (zero LLM cost).
        targeted_units: list[dict[str, Any]] = []
        if api_responses and api_llm_budget > 0:
            t0 = _time.monotonic()
            api_calls_made = 0
            for resp in api_responses:
                if api_calls_made >= api_llm_budget:
                    break
                body = resp.get("body")
                items = _find_unit_list(body)
                # Only spend budget on responses where something looks like a
                # unit list — avoids feeding analytics payloads to the LLM.
                if not items or not _has_unit_signals(items):
                    continue
                url = resp.get("url", "")
                try:
                    units, mapping, is_noise, interaction = await analyze_api_with_llm(
                        resp, property_context, ctx.property_id or "unknown",
                    )
                except Exception as exc:
                    result.errors.append(f"api-analysis-error: {exc}")
                    continue
                api_calls_made += 1
                if interaction:
                    llm_interactions.append(interaction)
                if is_noise:
                    # Feed profile_updater the colon-prefixed format it
                    # already recognises so this URL ends up in
                    # profile.api_hints.blocked_endpoints on next run.
                    llm_analysis_results[url] = "noise:no_unit_data"
                    continue
                if units:
                    targeted_units.extend(units)
                    if mapping:
                        # Emit the mapping dict at its URL key — matches
                        # the shape profile_updater reads via
                        # save_llm_field_mapping().
                        llm_field_mappings.append(mapping)
                        llm_analysis_results[url] = mapping
                        if not result.api_responses:
                            result.api_responses.append(resp)

            _log_attempt(
                "generic:llm_api_targeted",
                "ran_units" if targeted_units else ("ran_empty" if api_calls_made else "skipped"),
                units=len(targeted_units),
                reason="" if targeted_units else (
                    f"{api_calls_made} API(s) analysed, no units" if api_calls_made
                    else "no API responses with unit signals"
                ),
                duration_ms=int((_time.monotonic() - t0) * 1000),
            )

        if targeted_units:
            result.units = targeted_units
            result.tier_used = "TIER_4_LLM_API"
            result.winning_url = (
                result.api_responses[0].get("url") if result.api_responses
                else ctx.base_url
            )
            result.confidence = min(0.85, 0.6 + 0.04 * len(targeted_units))
            result._llm_interactions = llm_interactions  # type: ignore[attr-defined]
            result._llm_field_mappings = llm_field_mappings  # type: ignore[attr-defined]
            result._llm_analysis_results = llm_analysis_results  # type: ignore[attr-defined]
            return result

        # Sub-tier 6b: targeted DOM analysis ------------------------------
        # Extract the rent-bearing DOM section (not the full page) and ask
        # the LLM to return units AND CSS selectors we can replay next run.
        dom_units: list[dict[str, Any]] = []
        dom_section_html = _extract_rent_dom_section(html) if html else None
        if dom_section_html and dom_llm_budget > 0:
            t0 = _time.monotonic()
            try:
                dom_units, selectors, interaction = await analyze_dom_with_llm(
                    dom_section_html, ctx.base_url, property_context,
                    ctx.property_id or "unknown",
                )
            except Exception as exc:
                result.errors.append(f"dom-analysis-error: {exc}")
                dom_units, selectors, interaction = [], None, None
            if interaction:
                llm_interactions.append(interaction)
            if selectors:
                llm_css_selectors = selectors
            _log_attempt(
                "generic:llm_dom_targeted",
                "ran_units" if dom_units else "ran_empty",
                units=len(dom_units or []),
                reason="" if dom_units else "targeted DOM LLM returned no units",
                duration_ms=int((_time.monotonic() - t0) * 1000),
            )

        if dom_units:
            result.units = dom_units
            result.tier_used = "TIER_4_LLM_DOM"
            result.winning_url = ctx.base_url
            result.confidence = min(0.80, 0.55 + 0.04 * len(dom_units))
            result._llm_interactions = llm_interactions  # type: ignore[attr-defined]
            result._llm_field_mappings = llm_field_mappings  # type: ignore[attr-defined]
            result._llm_analysis_results = llm_analysis_results  # type: ignore[attr-defined]
            if llm_css_selectors:
                result._llm_hints = {"css_selectors": llm_css_selectors}  # type: ignore[attr-defined]
            return result

        # Sub-tier 6c: monolithic fallback --------------------------------
        # Only fires when 6a + 6b both returned empty. This is the legacy
        # "send full HTML + top-3 APIs" prompt — broadest coverage, highest
        # token cost, so it runs last.
        t0 = _time.monotonic()
        try:
            llm_input = prepare_llm_input(html, api_responses, property_context)
            llm_units, hints, _raw, interaction = await extract_with_llm(
                llm_input, property_id=ctx.property_id or "unknown",
            )
            if interaction:
                llm_interactions.append(interaction)
            # Capture navigation_hint (Phase 3 + Phase 5) so scrape_jugnu's
            # link-hop can prioritise the URL the LLM just told us about.
            if isinstance(hints, dict):
                nav = hints.get("navigation_hint") or ""
                if nav:
                    llm_navigation_hints.append(str(nav))
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
                result._llm_interactions = llm_interactions  # type: ignore[attr-defined]
                result._llm_field_mappings = llm_field_mappings  # type: ignore[attr-defined]
                result._llm_analysis_results = llm_analysis_results  # type: ignore[attr-defined]
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

        # All LLM sub-tiers empty — surface everything we learned so the
        # profile updater (Phase 4) can still record blocked endpoints and
        # link-hop (Phase 5) can follow navigation_hint on a second pass.
        result._llm_interactions = llm_interactions  # type: ignore[attr-defined]
        result._llm_field_mappings = llm_field_mappings  # type: ignore[attr-defined]
        result._llm_analysis_results = llm_analysis_results  # type: ignore[attr-defined]
        if llm_navigation_hints:
            result._llm_navigation_hints = llm_navigation_hints  # type: ignore[attr-defined]
        result.confidence = 0.0
        result.errors.append("Generic parser found no units in captured API responses")
        return result

    def static_fingerprints(self) -> list[str]:
        return list(self._fingerprints)
