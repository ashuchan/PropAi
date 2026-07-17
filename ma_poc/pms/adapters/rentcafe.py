"""
RentCafe adapter.

Research log
------------
Web sources consulted:
  - https://www.yardi.com/products/rentcafe/ — Yardi RentCafe product page (accessed 2026-04-17)
  - https://www.rentcafe.com/ — RentCafe public listing portal
Real payloads inspected (from data/runs/*/raw_api/):
  - 35593 (The Continental, Dallas) — rent.brookfieldproperties.com/wp-json/middleware/v1/
    getFloorplans/?propertyId[]=1782238 — flat list of floorplan objects with keys:
    propertyId, floorplanId, floorplanName, beds, baths, minimumSQFT, maximumSQFT,
    minimumRent, maximumRent, availableUnitsCount, availableDate, api:"rentcafe",
    availabilityURL (securecafe.com link), hasSpecials, min_price, max_price
  - 35593 (same property, run 2026-04-14) — identical schema, confirming stability
Key findings:
  - API endpoint: /wp-json/middleware/v1/getFloorplans/?propertyId[]=<id>
    or securecafe.com endpoints with similar structure
  - Response envelope: direct list[] at root (no wrapper)
  - Unit ID field: floorplanId (floorplan-level, not unit-level)
  - Rent field(s): minimumRent/maximumRent (string with decimals "1349.00"),
    min_price/max_price (integers), rent display not present
  - Known gotchas: RentCafe uses Yardi backend; api field == "rentcafe" is a reliable
    marker. availabilityURL points to securecafe.com for unit-level detail. Some
    RentCafe sites use JSON-LD (Schema.org) instead of or in addition to API.
    The .aspx vanity domain heuristic in detector.py catches non-hosted sites.
  - 2026-04-19 fix: Windsor Communities, Weidner, Bexley, Pacifica Residential
    all use PascalCase keys (FloorplanName, FloorplanId, MinimumRent, etc.).
    _normalise_item() lowercases all item keys before fingerprinting and parsing.
    _unwrap_rentcafe_list extended with Floorplans, FloorplanList,
    GetFloorplansResult, and two-level nesting support.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from ma_poc.pms.adapters._parsing import (
    bed_label_from,
    format_rent_range,
    make_unit_dict,
    money_to_int,
)
from ma_poc.pms.adapters._rentcafe_hosted_table import parse_rentcafe_hosted_table
from ma_poc.pms.adapters.base import AdapterContext, AdapterResult

# Note: ``ma_poc.extraction.post_process`` is imported lazily inside ``extract``
# below to break the import cycle. The full chain would otherwise be:
#   rentcafe (load) → extraction.post_process → extraction.infer →
#   pms.adapters._parsing → pms.adapters.__init__ → rentcafe (RE-load).
# Function-local import dodges this; Python caches the module after the first
# call, so there's no per-call overhead.

if TYPE_CHECKING:
    from playwright.async_api import Page

# Phase 4: module-level RentCafe-scoped qualifier singleton.
# Used by _is_rentcafe_response() to delegate its field-combination checks
# so all FieldCombination definitions live in signal_engine/defaults.py.
# The api=rentcafe value-sentinel check is NOT delegatable (it checks a
# response value, not a key) and stays inline in _is_rentcafe_response().
try:
    from ma_poc.pms.signal_engine.defaults import create_rentcafe_qualifier as _create_rq
    from ma_poc.pms.signal_engine.models import (
        SourceKind as _RCSourceKind,
    )
    from ma_poc.pms.signal_engine.models import (
        SourceSignal as _RCSourceSignal,
    )
    _rentcafe_qualifier = _create_rq()
except Exception:
    _rentcafe_qualifier = None  # type: ignore[assignment]
    _RCSourceKind = None  # type: ignore[assignment]
    _RCSourceSignal = None  # type: ignore[assignment]


def parse_rentcafe_floorplans(items: list[dict[str, Any]], url: str) -> list[dict[str, str]]:
    """Parse a RentCafe/Yardi floorplan list into standard unit dicts.

    Field-map additions (2026-05-12 fix for MAA Worthington):

      * **sqft**: prefer the unit-level ``sqft`` field (MAA-shaped payload);
        fall back to ``minimumsqft``/``maximumsqft`` (Windsor range shape).
        Pre-fix, MAA's per-unit ``sqft=1019`` was silently dropped because
        only the min/max forms were read.

      * **unit_number**: prefer the unit-level identifier (``apartmentname``
        or ``unitnumber``) when present; fall back to ``floorplanid`` (the
        legacy floorplan-level surrogate). MAA emits ``apartmentName="217"``
        — that's the real unit number, not the shared floorplan id.

    Both changes are additive: the legacy fallbacks keep existing Windsor/
    Bexley/Pacifica behaviour intact (their payloads don't carry the
    unit-level fields).
    """
    units: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        item_lc = _normalise_item(item)

        name = str(item_lc.get("floorplanname") or "")
        beds_raw = item_lc.get("beds")
        baths_raw = item_lc.get("baths")
        beds = int(beds_raw) if beds_raw is not None else None
        baths_str = str(baths_raw) if baths_raw is not None else None
        baths = int(float(baths_str)) if baths_str is not None else None

        # F1 (2026-05-12): unit-level sqft wins over plan-level min/max range.
        # Extended (2026-05-12): Yardi/RentCafe APIs use many sqft key variants
        # across management companies. All are lowercased by _normalise_item().
        # Order: single-value unit sqft → range lo/hi → area synonyms → size.
        sqft_single_raw = (
            item_lc.get("sqft")
            or item_lc.get("squarefeet")
            or item_lc.get("squarefootage")
            or item_lc.get("square_footage")
            or item_lc.get("square_feet")
            or item_lc.get("sq_ft")
            or item_lc.get("sqft_net")
            or item_lc.get("netsqft")
            or item_lc.get("floorsize")
            or item_lc.get("floor_size")
            or item_lc.get("unitsize")
        )
        if sqft_single_raw is not None and sqft_single_raw != "":
            sqft = str(sqft_single_raw)
        else:
            sqft_lo = str(
                item_lc.get("minimumsqft")
                or item_lc.get("minsqft")
                or item_lc.get("minimum_sqft")
                or item_lc.get("min_sqft")
                or item_lc.get("minimumsquarefeet")
                or item_lc.get("minsquarefeet")
                or ""
            )
            sqft_hi = str(
                item_lc.get("maximumsqft")
                or item_lc.get("maxsqft")
                or item_lc.get("maximum_sqft")
                or item_lc.get("max_sqft")
                or item_lc.get("maximumsquarefeet")
                or item_lc.get("maxsquarefeet")
                or ""
            )
            sqft = sqft_lo if sqft_lo == sqft_hi or not sqft_hi else f"{sqft_lo}-{sqft_hi}"

        # Prefer numeric min_price/max_price; fall back to string minimumRent/maximumRent
        rent_lo_raw = item_lc.get("min_price")
        if rent_lo_raw is not None and rent_lo_raw != "":
            rent_lo = int(rent_lo_raw) if rent_lo_raw else None
        else:
            rent_lo = money_to_int(str(item_lc.get("minimumrent") or ""))

        rent_hi_raw = item_lc.get("max_price")
        if rent_hi_raw is not None and rent_hi_raw != "":
            rent_hi = int(rent_hi_raw) if rent_hi_raw else None
        else:
            rent_hi = money_to_int(str(item_lc.get("maximumrent") or ""))

        avail_count = str(item_lc.get("availableunitscount") or item_lc.get("unitscount") or "")
        avail_date = str(item_lc.get("availabledate") or "")

        # F2 (2026-05-12): real unit number wins over floorplan-level id.
        unit_number_str = str(
            item_lc.get("apartmentname")
            or item_lc.get("unitnumber")
            or item_lc.get("unit_number")
            or item_lc.get("floorplanid")
            or ""
        )

        units.append(
            make_unit_dict(
                floor_plan_name=name,
                bed_label=bed_label_from(beds, name),
                bedrooms=str(beds) if beds is not None else "",
                bathrooms=str(baths) if baths is not None else "",
                sqft=sqft,
                unit_number=unit_number_str,
                rent_range=format_rent_range(rent_lo, rent_hi),
                availability_status="AVAILABLE" if avail_count and avail_count != "0" else "UNAVAILABLE",
                available_units=avail_count,
                availability_date=avail_date,
                source_api_url=url,
                extraction_tier="TIER_1_API_RENTCAFE",
            )
        )
    return units


_RENTCAFE_WRAPPER_KEYS = (
    "data",
    "results",
    "floorplans",
    "floorPlans",
    "Floorplans",
    "FloorplanList",
    "GetFloorplansResult",
    "items",
    "Result",
)

# Keys used when the list is nested two levels deep, e.g.
# {"response": {"result": [...]}} or {"Property": {"Floorplans": [...]}}
_RENTCAFE_WRAPPER_KEYS_L2: tuple[tuple[str, str], ...] = (
    ("response", "result"),
    ("Property", "Floorplans"),
    ("property", "floorplans"),
)


def _unwrap_rentcafe_list(body: Any) -> list[Any] | None:
    """Return the floorplan list inside common wrapper shapes, or None.

    Handles:
    - Root-level list: [...]
    - Single-level dict wrapper: {"data": [...]} / {"Result": [...]} / etc.
    - Two-level dict wrapper: {"response": {"result": [...]}} / {"Property": {"Floorplans": [...]}}

    Why: the original matcher only accepted root-level lists. Sites like
    windsorcommunities.com wrap the same RentCafe payload as
    ``{"data": [...]}`` or ``{"Result": [...]}`` (Yardi-style), so 12 of
    13 RentCafe NO_DATA properties in the 2026-04-19 run were silently
    rejected even when the API was successfully captured.
    """
    if isinstance(body, list):
        return body if body else None
    if isinstance(body, dict):
        for k in _RENTCAFE_WRAPPER_KEYS:
            v = body.get(k)
            if isinstance(v, list) and v:
                return v
        for outer, inner in _RENTCAFE_WRAPPER_KEYS_L2:
            outer_val = body.get(outer)
            if isinstance(outer_val, dict):
                v = outer_val.get(inner)
                if isinstance(v, list) and v:
                    return v
    return None


def _normalise_item(item: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of *item* with all keys lowercased.

    RentCafe/Yardi APIs are inconsistent in casing across management companies
    (e.g. Windsor Communities uses PascalCase while Brookfield uses camelCase).
    Normalising to lowercase lets all downstream field lookups use a single key.
    Called by both _is_rentcafe_response and parse_rentcafe_floorplans.
    """
    return {k.lower(): v for k, v in item.items()}


def _is_rentcafe_response(body: Any) -> bool:
    """Check if a response body looks like RentCafe floorplan or unit data."""
    items = _unwrap_rentcafe_list(body)
    if not items:
        return False
    first = items[0]
    if not isinstance(first, dict):
        return False
    first_lc = _normalise_item(first)
    # 2026-04-20 fix: PascalCase Windsor payloads ship ``"Api": "RentCafe"``.
    # _normalise_item lowercases the *keys* but not the *values*, so the prior
    # equality check only matched lowercase "rentcafe". Lowercase the value too.
    if str(first_lc.get("api") or "").lower() == "rentcafe":
        return True
    # Phase 4: delegate field-combination checks to the RentCafe-scoped
    # SourceQualifier so all FieldCombination definitions stay in defaults.py.
    # Fallback: inline checks when the signal engine is unavailable.
    if _rentcafe_qualifier is not None:
        sig = _RCSourceSignal(
            kind=_RCSourceKind.API_RESPONSE,
            field_keys=frozenset(first_lc.keys()),
        )
        return _rentcafe_qualifier.qualify(sig).qualifies
    # Fallback: inline field-combination checks (signal engine unavailable).
    _fp_keys: frozenset[str] = frozenset({
        "floorplanname", "floorplanid", "minimumrent",
        "maximumrent", "availableunitscount", "availabilityurl",
    })
    if len(_fp_keys & set(first_lc.keys())) >= 3:
        return True
    _unit_id_keys: frozenset[str] = frozenset({
        "rentcafeapartmentid", "rentcafefloorplanid", "rentcafepropertyid",
    })
    if len(_unit_id_keys & set(first_lc.keys())) >= 2:
        return True
    _unit_rent_keys: frozenset[str] = frozenset({
        "rentcafeapartmentid", "unitrent", "marketrent",
    })
    return len(_unit_rent_keys & set(first_lc.keys())) >= 2


# 2026-04-20 fix: structured tier codes for failure-mode classification.
# Pre-fix, every RentCafe failure stamped ``TIER_1_API_RENTCAFE`` regardless of
# whether the adapter saw zero responses, saw responses that didn't shape-match,
# or shape-matched but parsed to zero units. The 04-20 report had 38 RentCafe
# failures collapsed into a single bucket so downstream triage could not tell
# misrouting (Windsor sites that aren't actually RentCafe) from genuine empty
# inventory. Sub-codes split that bucket into machine-readable verdicts.
_TIER_BASE = "TIER_1_API_RENTCAFE"
_TIER_NO_RESPONSE = f"{_TIER_BASE}_NO_RESPONSE"
_TIER_SHAPE_REJECTED = f"{_TIER_BASE}_SHAPE_REJECTED"
_TIER_LIST_EMPTY = f"{_TIER_BASE}_LIST_EMPTY"
_TIER_PARSE_ZERO = f"{_TIER_BASE}_PARSE_ZERO"

# 2026-05-31 — paths that the detector misroute most often actually
# hosts unit data on. Mirrors the Knock _KNOCK_EMPTY_FALLTHROUGH_PATHS
# choice. Same-host only; the orchestrator's link-hop layer will fetch
# these via the normal probe ladder and the subsequent DOM / plan-text
# tiers do the extraction.
_RENTCAFE_SHAPE_REJECTED_FALLTHROUGH_PATHS: tuple[str, ...] = (
    "/floorplans",
    "/floor-plans",
    "/availability",
    "/available-units",
    "/apartments",
    "/units",
)


def _rentcafe_shape_rejected_emit_subpage_hints(
    result: Any, base_url: str
) -> None:
    """Append floor-plan family URLs as subpage hints on ``result``.

    Mirrors knock.py ``_knock_empty_emit_subpage_hints`` exactly so
    downstream link-hop treats both signals the same. Idempotent: if
    a candidate already exists in
    ``result._embedded_floorplan_subpage_hints`` it is skipped.
    """
    from urllib.parse import urljoin, urlparse

    parsed = urlparse(base_url)
    if not parsed.scheme or not parsed.netloc:
        return  # malformed — emit nothing rather than bad URLs

    existing = list(getattr(result, "_embedded_floorplan_subpage_hints", None) or [])
    existing_urls: set[str] = {u for u, _ in existing}

    additions: list[tuple[str, str]] = []
    for path in _RENTCAFE_SHAPE_REJECTED_FALLTHROUGH_PATHS:
        candidate = urljoin(base_url, path)
        if candidate in existing_urls:
            continue
        additions.append((candidate, "rentcafe_shape_rejected_fallthrough"))

    if additions:
        result._embedded_floorplan_subpage_hints = existing + additions  # type: ignore[attr-defined]


def _classify_rentcafe_failure(api_responses: list[dict[str, Any]]) -> tuple[str, str]:
    """Return (tier_code, machine-readable error message) for a failed run."""
    if not api_responses:
        return (_TIER_NO_RESPONSE, "RENTCAFE_NO_RESPONSE: no network responses captured during page load")
    shape_matches = [r for r in api_responses if _is_rentcafe_response(r.get("body"))]
    if not shape_matches:
        return (
            _TIER_SHAPE_REJECTED,
            f"RENTCAFE_SHAPE_REJECTED: {len(api_responses)} responses captured, "
            "none matched RentCafe envelope/key signature",
        )
    total_items = 0
    for r in shape_matches:
        items = _unwrap_rentcafe_list(r.get("body")) or []
        total_items += len(items)
    if total_items == 0:
        return (
            _TIER_LIST_EMPTY,
            f"RENTCAFE_LIST_EMPTY: {len(shape_matches)} shape-matched responses, "
            "floorplan list was empty in all",
        )
    return (
        _TIER_PARSE_ZERO,
        f"RENTCAFE_PARSE_ZERO: {total_items} floorplan items present across "
        f"{len(shape_matches)} responses, but parser emitted zero units "
        "(field-name mismatch likely)",
    )


class RentCafeAdapter:
    """RentCafe (Yardi) PMS adapter."""

    pms_name: str = "rentcafe"
    _fingerprints: list[str] = ["rentcafe.com"]

    async def extract(self, page: Page, ctx: AdapterContext) -> AdapterResult:
        """Extract units from RentCafe API responses captured during page load."""
        result = AdapterResult(tier_used=_TIER_BASE)
        all_units: list[dict[str, str]] = []

        api_responses: list[dict[str, Any]] = getattr(ctx, "_api_responses", [])
        for resp in api_responses:
            body = resp.get("body")
            if not _is_rentcafe_response(body):
                continue
            items = _unwrap_rentcafe_list(body)
            if not items:
                continue
            url = resp.get("url", "")
            units = parse_rentcafe_floorplans(items, url)
            if units:
                all_units.extend(units)
                result.api_responses.append(resp)

        if all_units:
            # Stage 1 validity gate: every emitted unit must carry at least
            # one numeric dimension (beds OR baths OR area, post-inference,
            # post-sanity). Drops the 2026-05-11 regression shapes
            # (Skyline-at-Kessler neighborhoods that masqueraded as units)
            # while preserving real RentCafe inventory. See
            # docs/2026_05_11_regressions_fix_design.md.
            #
            # Lazy import: see module-level note above for the cycle-break.
            from ma_poc.extraction.post_process import post_process

            pp = post_process(all_units, property_id=getattr(ctx, "property_id", None))
            if pp.n_admitted > 0:
                # 2026-05-18 (canary deep-probe): the network getFloorplans
                # XHR returns FLOORPLAN aggregates only ("4 available, from
                # $1,465"), not unit-level. Returning here stamps
                # SUCCESS_PLAN_LEVEL and never drills to the securecafe
                # online-leasing portal — yet that portal carries the real
                # unit-level inventory and is curl_cffi-reachable for 84%
                # of brochure + 67% of has-inventory RC residual (sc_gap
                # probe, 112 sites, CF-walled=0). So when the admitted set
                # is plan-level ONLY (no unit_number), attempt the
                # securecafe drill-down BEFORE returning; prefer unit-level,
                # fall back to this plan-level result if it fails.
                _has_unit_level = any(
                    str(u.get("unit_number") or "").strip() for u in pp.admitted
                )
                if not _has_unit_level:
                    sc_units = await _try_rentcafe_securecafe_probe(ctx, result)
                    if sc_units:
                        sc_pp = post_process(
                            sc_units,
                            property_id=getattr(ctx, "property_id", None),
                        )
                        if sc_pp.n_admitted > 0:
                            result.units = sc_pp.admitted
                            result.plan_summaries = sc_pp.plan_summaries
                            result.tier_used = f"{_TIER_BASE}_SECURECAFE"
                            result.confidence = min(
                                0.92, 0.7 + 0.04 * sc_pp.n_admitted
                            )
                            return result
                # Unit-level already present, or securecafe drill-down
                # unavailable: return the network getFloorplans result.
                # Stage 2: surface unit-level AND plan-level lists. The
                # runner promotes ``plan_summaries`` into the V2 record's
                # ``floor_plans[]`` field; verdict treats a property with
                # only plan-level data as SUCCESS_PLAN_LEVEL.
                result.units = pp.admitted
                result.plan_summaries = pp.plan_summaries
                result.winning_url = (
                    result.api_responses[0].get("url") if result.api_responses else None
                )
                result.confidence = min(0.95, 0.7 + 0.05 * pp.n_admitted)
                result.tier_used = _TIER_BASE
                return result
            # All parsed units failed validity — record and fall through
            # to the failure-classification path so the run-report
            # distinguishes "parser produced rows but all invalid" from
            # "parser produced zero rows".
            result.errors.append(
                f"RENTCAFE_VALIDITY_REJECTED: {len(all_units)} parsed rows "
                f"failed unit_validity (no numeric dimension)"
            )

        # 2026-05-13 (C3 RentCafe SHAPE_REJECTED, teammate analysis):
        # before classifying as a failure, probe the property's own
        # ``/wp-json/middleware/v1/getFloorplans/`` WordPress endpoint
        # directly. Yardi/RentCafe sites mounted under WordPress (typical
        # for management-company brand sites) expose this endpoint with a
        # ``propertyId[]=<id>`` query. When the network-log capture missed
        # the in-page XHR (timing or CDN-proxied via a different host), the
        # direct probe still works. Analogous to Entrata's
        # ``_probe_known_endpoints`` (entrata.py:270).
        wp_units = await _try_rentcafe_wp_probe(ctx, result)
        if wp_units:
            from ma_poc.extraction.post_process import post_process
            pp = post_process(wp_units, property_id=getattr(ctx, "property_id", None))
            if pp.n_admitted > 0:
                result.units = pp.admitted
                result.plan_summaries = pp.plan_summaries
                result.tier_used = f"{_TIER_BASE}_WP_PROBE"
                result.confidence = min(0.90, 0.65 + 0.05 * pp.n_admitted)
                return result

        # 2026-05-17 (canary deep-probe): securecafe online-leasing portal
        # carries the real UNIT-LEVEL inventory one drill-down past the
        # floorplan page. Highest-leverage RentCafe path — promotes the
        # ~1,060-property floorplan/LLM pool to deterministic Tier-1.
        sc_units = await _try_rentcafe_securecafe_probe(ctx, result)
        if sc_units:
            from ma_poc.extraction.post_process import post_process
            pp = post_process(sc_units, property_id=getattr(ctx, "property_id", None))
            if pp.n_admitted > 0:
                result.units = pp.admitted
                result.plan_summaries = pp.plan_summaries
                result.tier_used = f"{_TIER_BASE}_SECURECAFE"
                result.confidence = min(0.92, 0.7 + 0.04 * pp.n_admitted)
                return result

        # 2026-05-18: RentCafe-HOSTED SSR unit table fallback. The
        # rentcafe.com/.../default.aspx portal (and the same RC widget on
        # rendered vanity pages) SSRs every unit as
        # ``<tr class="fp-unit" data-unit-*>`` — server-200, no bot-wall,
        # no login. main's generic extractor misses this markup. Render-
        # dependent, so parse the already-rendered page HTML.
        try:
            from ma_poc.pms.adapters.generic import _get_page_html
            _rc_html = await _get_page_html(page, ctx)
        except Exception:
            _rc_html = ""
        if _rc_html and "fp-unit" in _rc_html:
            hosted = parse_rentcafe_hosted_table(
                _rc_html, str(getattr(ctx, "base_url", "") or "")
            )
            if hosted:
                from ma_poc.extraction.post_process import post_process
                pp = post_process(
                    hosted, property_id=getattr(ctx, "property_id", None)
                )
                if pp.n_admitted > 0:
                    result.units = pp.admitted
                    result.plan_summaries = pp.plan_summaries
                    result.tier_used = "TIER_1_DOM_RENTCAFE_HOSTED"
                    result.confidence = min(0.92, 0.7 + 0.04 * pp.n_admitted)
                    return result

        # 2026-07-16 (Lever 2): inline SecureCafe AvailUnitRow table. Some
        # RentCafe/Yardi marketing pages SSR the full availableunits.aspx
        # table INLINE in the rendered body (``<tr class='AvailUnitRow'>``
        # rows under "Floor Plan: <name> - N Bedroom, N Bathroom" headers)
        # rather than behind a portal drill. The live SecureCafe probe above
        # can miss these (base-URL discovery fails or the portal fetch is
        # CF/proxy-blocked), yet the same rows are already in ``_rc_html``.
        if _rc_html and "AvailUnitRow" in _rc_html:
            inline_units = parse_securecafe_availableunits(
                _rc_html, str(getattr(ctx, "base_url", "") or "")
            )
            if inline_units:
                from ma_poc.extraction.post_process import post_process
                pp = post_process(
                    inline_units, property_id=getattr(ctx, "property_id", None)
                )
                if pp.n_admitted > 0:
                    result.units = pp.admitted
                    result.plan_summaries = pp.plan_summaries
                    result.tier_used = "TIER_1_DOM_RENTCAFE_INLINE_AVAILUNITROW"
                    result.confidence = min(0.92, 0.7 + 0.04 * pp.n_admitted)
                    return result

        # 2026-05-20: Nestin per-plan DOM recovery. The 35-prop JSON-LD
        # probe (project_jsonld_recovery_2026-05-20.md) found that ~89% of
        # the 298-prop JSON-LD ALL_fail bucket are RentCafe-Nestin marketing
        # sites where the unit + rent + date data lives one nav-hop deeper
        # at /floorplans/{plan-slug}. Previous tiers (XHR / WP / SecureCafe /
        # hosted-table) handle the API-shape cohort; this branch picks up
        # the Nestin DOM-template cohort. Detection: resource.rentcafe.com
        # image CDN signal in rendered HTML. Probes each /floorplans/{slug}
        # detail page via curl_cffi for the table (Layout A1) or card
        # (Layout A2) layout.
        if _rc_html:
            try:
                from ma_poc.pms.adapters._rentcafe_nestin import (
                    is_nestin_template,
                    recover_rentcafe_nestin_per_plan,
                )

                if is_nestin_template(_rc_html):
                    # Pass the live Playwright page so detail-page fetches
                    # use the browser's CF-cleared session (probe_get hits
                    # CF-403 even with static cf_clearance cookies; verified
                    # 2026-05-20 e2e probe — 13/13 detail-page 403 via
                    # probe_get, 4/4 OK via page.evaluate(fetch)).
                    nestin_units, nestin_source = await recover_rentcafe_nestin_per_plan(
                        _rc_html,
                        str(getattr(ctx, "base_url", "") or ""),
                        page=page,
                    )
                    if nestin_units:
                        from ma_poc.extraction.post_process import post_process
                        pp = post_process(
                            nestin_units, property_id=getattr(ctx, "property_id", None)
                        )
                        if pp.n_admitted > 0:
                            result.units = pp.admitted
                            result.plan_summaries = pp.plan_summaries
                            result.tier_used = "TIER_1_DOM_RENTCAFE_NESTIN"
                            if nestin_source:
                                result.winning_url = nestin_source
                            result.confidence = min(0.90, 0.65 + 0.04 * pp.n_admitted)
                            return result
            except Exception as nestin_exc:
                # Recovery must never block scrape — log + fall through to
                # failure classification.
                result.errors.append(
                    f"rentcafe-nestin-error: {type(nestin_exc).__name__}: "
                    f"{str(nestin_exc)[:120]}"
                )

        # 2026-05-27 (612-failure-grind row 2): anchor-walk fallback. ~34
        # RentCafe properties publish units only at a vanity sub-path
        # (``/availability``, ``/floor-plans``, etc.) that the well-known
        # tiers above don't probe. Mirror entrata_deep_probe's homepage
        # anchor regex, fetch each candidate, and try the existing parsers.
        try:
            anchor_units = await _try_rentcafe_anchor_walk(page, ctx, result)
        except Exception as aw_exc:  # never crash adapter
            anchor_units = []
            result.errors.append(
                f"rentcafe-anchor-walk-error: {type(aw_exc).__name__}: "
                f"{str(aw_exc)[:120]}"
            )
        if anchor_units:
            from ma_poc.extraction.post_process import post_process
            pp = post_process(
                anchor_units, property_id=getattr(ctx, "property_id", None)
            )
            if pp.n_admitted > 0:
                result.units = pp.admitted
                result.plan_summaries = pp.plan_summaries
                result.tier_used = f"{_TIER_BASE}_ANCHOR_WALK"
                result.confidence = min(0.88, 0.65 + 0.04 * pp.n_admitted)
                return result

        # Failure path: re-stamp tier_used with a structured sub-code so the
        # downstream report can distinguish misrouting from genuine zero data.
        tier_code, err_msg = _classify_rentcafe_failure(api_responses)
        result.tier_used = tier_code
        result.confidence = 0.0
        result.errors.append(err_msg)

        # ── SHAPE_REJECTED fallthrough (2026-05-31) ─────────────────
        # When the RentCafe adapter declares SHAPE_REJECTED — i.e. it
        # captured network responses but NONE matched the RentCafe
        # envelope/key signature — the property is almost certainly a
        # detector misroute. Pattern from the 2026-05-28 canary QC
        # (404-prop residue): operator's homepage carries a SecureCafe
        # / rentcafe.com LINK (lease application portal) but the actual
        # PMS is custom WordPress / Wix / Squarespace with rents in
        # static HTML. Detector tags as RentCafe → no RentCafe API
        # responses fire → SHAPE_REJECTED → property fails despite
        # data being trivially extractable.
        #
        # Mirrors the Knock empty-API fallthrough pattern (knock.py
        # line 334, chip 2026-05-21). Surfaces standard floor-plan
        # subpath URLs as link-hop hints so the orchestrator drives
        # the next tier (DOM scan + generic_plan_text + empty-inventory
        # predicate) against the right URL.
        _rcsr_base_url = str(getattr(ctx, "base_url", "") or "")
        if tier_code == _TIER_SHAPE_REJECTED and _rcsr_base_url:
            _rentcafe_shape_rejected_emit_subpage_hints(result, _rcsr_base_url)

        return result

    def static_fingerprints(self) -> list[str]:
        return list(self._fingerprints)

    def matches_response_body(self, body: Any) -> bool:
        """Body-shape check used by ``detector.confirm_detection``.

        Returns True if *body* plausibly belongs to RentCafe. Reuses the same
        ``_is_rentcafe_response`` predicate the extractor uses internally so
        the router and the parser agree on what "RentCafe-shaped" means.
        """
        return _is_rentcafe_response(body)


# 2026-05-13 (C3 SHAPE_REJECTED fallback): regex to pull a RentCafe property
# ID from the rendered HTML. RentCafe property IDs appear in (a) anchor hrefs
# (``propertyId=<id>`` query param), (b) the WordPress middleware embed
# config (``data-property-id="..."``), and (c) inline script tags
# (``propertyId: <id>``).
_RENTCAFE_PROP_ID_HTML_RE = re.compile(
    r"""
    (?:
        propertyId(?:\[\])?=(\d{3,9})           # query-string form
      | data-property[-_]id=["'](\d{3,9})["']   # data-attribute form
      | propertyId[\"']?\s*[:=]\s*(\d{3,9})     # JS-config form
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _find_rentcafe_property_id(html: str) -> str | None:
    """Return the first RentCafe propertyId seen in *html*, or None."""
    if not html:
        return None
    m = _RENTCAFE_PROP_ID_HTML_RE.search(html)
    if not m:
        return None
    for grp in m.groups():
        if grp:
            return grp
    return None


def _origin_from_ctx(ctx: AdapterContext) -> str:
    """scheme://netloc for the property's effective URL (post-redirect)."""
    candidate = ""
    fr = getattr(ctx, "fetch_result", None)
    if fr is not None:
        candidate = str(getattr(fr, "final_url", "") or "")
    if not candidate:
        candidate = getattr(ctx, "base_url", "") or ""
    try:
        from urllib.parse import urlparse
        p = urlparse(candidate)
    except Exception:
        return ""
    if not p.scheme or not p.netloc:
        return ""
    return f"{p.scheme}://{p.netloc}"


async def _try_rentcafe_wp_probe(
    ctx: AdapterContext, result: AdapterResult
) -> list[dict[str, str]]:
    """SHAPE_REJECTED fallback: probe ``<origin>/wp-json/middleware/v1/getFloorplans/``
    directly with the property ID extracted from the rendered HTML.

    Returns parsed unit dicts on success, empty list on any failure.
    """
    html = ""
    fr = getattr(ctx, "fetch_result", None)
    body = getattr(fr, "body", None) if fr is not None else None
    if isinstance(body, bytes):
        try:
            html = body.decode("utf-8", errors="replace")
        except Exception:
            html = ""
    elif isinstance(body, str):
        html = body
    if not html:
        return []

    prop_id = _find_rentcafe_property_id(html)
    if not prop_id:
        return []

    origin = _origin_from_ctx(ctx)
    if not origin:
        return []

    api_url = f"{origin}/wp-json/middleware/v1/getFloorplans/?propertyId[]={prop_id}"
    try:
        import httpx
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json",
        }
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as c:
            r = await c.get(api_url, headers=headers)
        if r.status_code != 200:
            return []
        try:
            payload = r.json()
        except (ValueError, Exception):
            return []
    except Exception as exc:
        result.errors.append(
            f"rentcafe-wp-probe-error: prop_id={prop_id!r} "
            f"{type(exc).__name__}: {str(exc)[:120]}"
        )
        return []

    items = _unwrap_rentcafe_list(payload)
    if not items:
        return []
    units = parse_rentcafe_floorplans(items, api_url)
    if units:
        result.api_responses.append(
            {"url": api_url, "status": 200, "body": payload, "via": "wp_probe"}
        )
        result.winning_url = api_url
    return units


# ── RentCafe securecafe online-leasing portal — UNIT-LEVEL ───────────────────
# 2026-05-17 (canary deep-probe): the biggest stuck pool (~1,060 props) are
# RentCafe sites whose marketing page only yields floorplan-level rows (LLM
# reads "Starting at $X / N Available"). The real UNIT-LEVEL inventory lives
# one drill-down deeper, server-rendered, at the securecafe online-leasing
# portal:
#   https://<sub>.securecafe.com/onlineleasing/<slug>/availableunits.aspx
# Each ``<tr class='AvailUnitRow'>`` is one real apartment (unit #, sqft,
# rent range), grouped under a floorplan header that carries beds/baths.
# securecafe is Cloudflare-fronted, so the probe fetches via curl_cffi
# (TLS impersonation passes the CF challenge; plain httpx gets the 5KB
# challenge shell). Deterministic Tier-1 — no LLM, no hallucination.
#
# 2026-05-20 cluster #5 RentCafe sub-cluster fix: the regex now also
# captures slugs from ``/residentservices/<slug>`` paths. Many RentCafe
# marketing sites (cityridgedc, thedylanchicago, ...) only link to the
# *resident-services* portal (current-resident login) — the
# *online-leasing* portal isn't anchored anywhere in the marketing DOM,
# but the same slug is mounted under both paths on the SecureCafe tenant.
# Live-probed 2026-05-20:
#   cityridgedc residentservices/city-ridge-clo → onlineleasing/city-ridge-clo
#     /availableunits.aspx returns 59 AvailUnitRow rows.
#   thedylanchicago residentservices/160-n-morgan → onlineleasing/160-n-morgan
#     /availableunits.aspx returns 2 AvailUnitRow rows.
# Both URLs CF-403 plain httpx but 200 with curl_cffi chrome120 impersonation
# (the adapter's ``probe_get`` already uses chrome120).
_SECURECAFE_URL_RE = re.compile(
    r"""https?://
        (?P<sub>[a-z0-9][a-z0-9-]*)\.(?P<dom>securecafe(?:net)?)\.com
        /(?:onlineleasing|residentservices)/
        (?P<slug>[a-z0-9][a-z0-9-]*)
    """,
    re.IGNORECASE | re.VERBOSE,
)
# 2026-05-21 grind600 finding: 100/600 random-sample properties (16.7%) route
# through SecureCafe. The original regex matched only ``securecafe.com``;
# ``securecafenet.com`` (resident-portal alt domain) was a blind spot. The
# union accepts both. ``availableunits.aspx`` is hosted on both — the
# residentservices login redirects to the onlineleasing leasing flow on
# the same Yardi tenant.

_WORD_NUM = {
    "studio": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
}

# 2026-07-12: the name group was ``[^<\-]{1,80}?`` — it forbade '-', so
# SecureCafe headers whose plan name itself contains a hyphen
# ("Floor Plan: 1bd x 1ba - 850sqft - The Birch - 1 Bedroom, 1 Bathroom")
# never matched and parse_securecafe_availableunits returned [] despite
# real ``<tr class='AvailUnitRow'>`` rows being present (prod 2026-07-12
# rentcafe plan-level cohort, verified 0→2 units on roundtree). The lazy
# ``[^<]{1,120}?`` still terminates unambiguously because the following
# ``\s*-\s*(Studio|N Bedroom), N Bathroom`` structural anchor is the only
# thing that ends the name — it just no longer trips on interior hyphens.
_SECURECAFE_FP_HDR_RE = re.compile(
    r"Floor\s+Plan:\s*(?P<name>[^<]{1,120}?)\s*-\s*"
    r"(?P<bedtxt>Studio|\d+\s*Bedroom[s]?)\s*,\s*"
    r"(?P<bathtxt>\d+(?:\.\d+)?)\s*Bathroom",
    re.IGNORECASE,
)

_SECURECAFE_UNIT_ROW_RE = re.compile(
    r"<tr[^>]*class='AvailUnitRow'.*?</tr>", re.IGNORECASE | re.DOTALL
)
_SC_APT_RE = re.compile(r"data-label=['\"]?Apartment['\"]?[^>]*>\s*#?\s*([A-Za-z0-9-]+)", re.I)
_SC_SQFT_RE = re.compile(r"data-label=['\"]?Sq\.?Ft\.?['\"]?[^>]*>\s*([\d,]+)", re.I)
_SC_RENT_RE = re.compile(
    r"data-label=['\"]?Rent['\"]?[^>]*>\s*\$?\s*([\d,]+)\s*(?:-\s*\$?\s*([\d,]+))?", re.I
)
# 2026-05-18: securecafe AvailUnitRow has a ``data-label='Date Available'``
# cell (inner text e.g. "Available" or a "6/25/26" date). The parser
# previously ignored it ⇒ TIER_1_API_RENTCAFE_SECURECAFE (21.6k units)
# had 0% available_date. Cell may wrap the value in a <span>; capture
# the inner HTML and strip tags. schema_v2._format_date normalizes
# "Available"/"M/D/YY" forms.
_SC_DATE_RE = re.compile(
    r"data-label=['\"]?Date Available['\"]?[^>]*>(.*?)</td>", re.I | re.S
)
# 2026-05-22: every AvailUnitRow carries a "Apply Now" button whose onclick
# is ``SetTermsUrl('rentaloptions.aspx?UnitID=<u>&FloorPlanID=<fp>&...')``.
# The FloorPlanID is the stable Yardi plan ID — same value as apts247's
# ``feed_id`` on apts247-backed marketing sites. We capture it per unit to
# join SecureCafe units (which carry rent + unit_number but often NO sqft)
# to apts247's ``/api/v3/floorplans/all/`` response (which carries sqft +
# bed + bath + name). Without this join, ~6 apts247-backed properties
# stamp SUCCESS_PLAN_LEVEL despite having unit-level data — they only
# lack sqft.
_SC_FPID_RE = re.compile(
    r"rentaloptions\.aspx[^'\"]*?[?&]FloorPlanID=(\d+)", re.IGNORECASE
)


def _securecafe_base_from_match(m: re.Match[str]) -> str:
    """Build ``https://<sub>.securecafe.com/onlineleasing/<slug>`` from a
    regex match.

    The Yardi tenant exposes the public leasing endpoint at
    ``<sub>.securecafe.com/onlineleasing/<slug>/availableunits.aspx``.
    The ``securecafenet.com`` host is the resident-services portal only
    — leasing data is NOT mounted there (verified 2026-05-21: every
    ``.net`` variant we synthesized returned 404 while the corresponding
    ``.com`` host returned 403/CF-challenge → 200 with curl_cffi).

    So when the regex matches a ``.securecafenet.com`` URL we still
    rewrite the synthesized base onto ``.securecafe.com``: same
    ``<sub>`` and ``<slug>``, different host."""
    return (
        f"https://{m.group('sub')}.securecafe.com"
        f"/onlineleasing/{m.group('slug')}"
    )


def _find_all_securecafe_bases(
    html: str, ctx: AdapterContext
) -> list[str]:
    """Return ALL distinct ``<sub>.securecafe.com/onlineleasing/<slug>``
    bases found across the rendered HTML + captured network responses +
    origin fallback. Preserves source-order (so the first occurrence
    still wins when only one base is present).

    2026-05-23: 11 of 54 RentCafe SHAPE_REJECTED properties have ≥2
    distinct securecafe slugs on the homepage — a portfolio that links
    to sibling properties (e.g. Majestic Vernon Hills' homepage links
    to Forest Cove first, the actual Majestic slug second). The old
    single-base finder picked the first match → wrong sibling →
    SC drill returned 0 → SHAPE_REJECTED. Returning all bases lets the
    caller try each.
    """
    seen: set[str] = set()
    bases: list[str] = []

    def _add(m: re.Match[str] | None) -> None:
        if not m:
            return
        base = _securecafe_base_from_match(m)
        if base and base not in seen:
            seen.add(base)
            bases.append(base)

    # 1. Rendered HTML — primary source. Use finditer to capture all.
    if html:
        for m in _SECURECAFE_URL_RE.finditer(html):
            _add(m)
    # 2. Captured network responses — backup when HTML didn't carry the
    # link (e.g. patchright-rendered DOM diverged from raw server HTML).
    for resp in getattr(ctx, "_api_responses", []) or []:
        u = str(resp.get("url", "") or "")
        ul = u.lower()
        if "securecafe.com/" not in ul and "securecafenet.com/" not in ul:
            continue
        _add(_SECURECAFE_URL_RE.search(u))
    # 3. Origin self-check — when the property's own host IS the portal.
    origin = _origin_from_ctx(ctx)
    if "securecafe.com" in origin or "securecafenet.com" in origin:
        fr = getattr(ctx, "fetch_result", None)
        final = str(getattr(fr, "final_url", "") or "") if fr else ""
        _add(_SECURECAFE_URL_RE.search(final or origin))
    return bases


def _find_securecafe_base(html: str, ctx: AdapterContext) -> str | None:
    """Back-compat shim — returns the first SC base or None.

    New code should use :func:`_find_all_securecafe_bases` so the
    multi-slug Majestic-style pattern is handled. This shim preserves
    the contract of the single-base helper for any existing caller.
    """
    bases = _find_all_securecafe_bases(html, ctx)
    return bases[0] if bases else None


def _beds_from_text(bedtxt: str) -> int:
    bedtxt = bedtxt.strip().lower()
    if bedtxt.startswith("studio"):
        return 0
    m = re.match(r"(\d+)", bedtxt)
    return int(m.group(1)) if m else 0


def _sc_applicant_int(v: Any) -> int | None:
    """Coerce the applicant-portal's numeric fields (int/float/str) to a sane int."""
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        return None
    return n if 0 < n < 1_000_000 else None


def parse_securecafe_applicant_floorplans(
    payload: Any, source_url: str
) -> list[dict[str, Any]]:
    """Parse the RentCafe "applicant portal" FloorPlansV2 API.

    This is the React-SPA replacement for the legacy ``availableunits.aspx``
    that Yardi began migrating SecureCafe tenants to ~2026-07-15 (see
    ``project_securecafe_applicant_spa_migration_2026-07-16``). The legacy
    ``AvailUnitRow`` HTML no longer exists for migrated tenants; unit data
    comes from this JSON API instead.

    Endpoint (behind CF Turnstile — reachable with a HEADED browser render,
    no BrightData needed):
        ``{sub}.securecafeapplicant.com/onlineleasing/api/floorplan/
        getfloorplanandavailableunits?propertyId={pid}&RequestBeforeLogin=true
        &isPropertyList=false``

    Envelope (verified live on belair 2026-07-16 + against
    ``FloorPlansV2Api-*.js``)::

        {"status": true, "floorPlanList": [
            {"floorPlan": {"FloorPlanName", "Beds", "Baths", "MinimumRent",
                           "MaximumRent", "MinimumArea", "MaximumArea",
                           "MinimumDeposit", "AvailableUnits", "DoNotPublish"},
             "UnitAvailability": [{"unitcode", "DisplayMinRent"|"startingRent",
                                   "AvailableDate"}]}]}

    Emits unit-level rows when ``UnitAvailability`` is populated, else a
    plan-level row per published floor plan (fully-occupied plans still carry
    rent/sqft). Unit-level field names are confirmed from the JS module; the
    plan-level fields + envelope are confirmed against a live response.
    """
    plans: list[Any] = []
    if isinstance(payload, dict):
        plans = payload.get("floorPlanList") or payload.get("FloorPlanList") or []
    elif isinstance(payload, list):
        plans = payload
    units: list[dict[str, Any]] = []
    for entry in plans:
        if not isinstance(entry, dict):
            continue
        fp = entry.get("floorPlan") or entry.get("FloorPlan") or {}
        if not isinstance(fp, dict) or fp.get("DoNotPublish"):
            continue
        name = str(fp.get("FloorPlanName") or "")
        beds = fp.get("Beds")
        baths = fp.get("Baths")
        sqft_v = _sc_applicant_int(fp.get("MinimumArea") or fp.get("MaximumArea"))
        dep_v = _sc_applicant_int(fp.get("MinimumDeposit"))
        deposit = f"${dep_v:,}" if dep_v else ""
        beds_s = "" if beds is None else str(beds)
        baths_s = "" if baths is None else str(baths)
        sqft_s = "" if sqft_v is None else str(sqft_v)
        avail = entry.get("UnitAvailability") or entry.get("unitAvailability") or []
        if isinstance(avail, list) and avail:
            for u in avail:
                if not isinstance(u, dict):
                    continue
                rent = _sc_applicant_int(
                    u.get("DisplayMinRent")
                    or u.get("startingRent")
                    or fp.get("MinimumRent")
                )
                units.append(
                    make_unit_dict(
                        floor_plan_name=name,
                        bedrooms=beds_s,
                        bathrooms=baths_s,
                        sqft=sqft_s,
                        unit_number=str(u.get("unitcode") or u.get("UnitCode") or ""),
                        rent_low=rent,
                        rent_high=rent,
                        deposit=deposit,
                        availability_date=str(
                            u.get("AvailableDate") or u.get("availableDate") or ""
                        ),
                    )
                )
        elif name:
            fp_lo = _sc_applicant_int(fp.get("MinimumRent"))
            units.append(
                make_unit_dict(
                    floor_plan_name=name,
                    bedrooms=beds_s,
                    bathrooms=baths_s,
                    sqft=sqft_s,
                    rent_low=fp_lo,
                    rent_high=_sc_applicant_int(fp.get("MaximumRent")) or fp_lo,
                    deposit=deposit,
                )
            )
    return units


def parse_securecafe_availableunits(html: str, source_url: str) -> list[dict[str, Any]]:
    """Parse a securecafe ``availableunits.aspx`` page into unit-level dicts.

    The page is a sequence of floorplan sections; each section header
    ("... Floor Plan: A1 One Bedroom / One Bath - 1 Bedroom, 1 Bathroom")
    is followed by ``<tr class='AvailUnitRow'>`` rows, one per real
    apartment. Returns ``[]`` on a CF-challenge shell or unparseable HTML.
    """
    if not html or "AvailUnitRow" not in html:
        return []
    units: list[dict[str, Any]] = []
    headers = list(_SECURECAFE_FP_HDR_RE.finditer(html))
    if not headers:
        return []
    for idx, hm in enumerate(headers):
        seg_start = hm.end()
        seg_end = headers[idx + 1].start() if idx + 1 < len(headers) else len(html)
        segment = html[seg_start:seg_end]
        fp_name = re.sub(r"\s+", " ", hm.group("name")).strip()
        beds = _beds_from_text(hm.group("bedtxt"))
        try:
            baths = float(hm.group("bathtxt"))
        except (TypeError, ValueError):
            baths = 0.0
        for row in _SECURECAFE_UNIT_ROW_RE.findall(segment):
            apt = _SC_APT_RE.search(row)
            if not apt:
                continue
            sqft_m = _SC_SQFT_RE.search(row)
            rent_m = _SC_RENT_RE.search(row)
            rent_low = rent_high = None
            if rent_m:
                rent_low = money_to_int(rent_m.group(1))
                rent_high = money_to_int(rent_m.group(2)) if rent_m.group(2) else rent_low
            date_m = _SC_DATE_RE.search(row)
            avail_date = ""
            if date_m:
                avail_date = re.sub(r"<[^>]+>", " ", date_m.group(1))
                avail_date = re.sub(r"\s+", " ", avail_date).strip()
            # Capture the FloorPlanID from the rentaloptions onclick — used
            # downstream as the apts247 feed_id join key. ``source_ids`` is
            # the schema-blessed bag for stable PMS-native identifiers (see
            # _parsing.make_unit_dict docstring) so it does not invent new
            # top-level keys.
            fpid_m = _SC_FPID_RE.search(row)
            source_ids: dict[str, Any] = {}
            if fpid_m:
                source_ids["securecafe_floorplan_id"] = fpid_m.group(1)
            units.append(
                make_unit_dict(
                    floor_plan_name=fp_name,
                    bedrooms=str(beds),
                    bathrooms=str(baths),
                    sqft=(sqft_m.group(1).replace(",", "") if sqft_m else ""),
                    unit_number=apt.group(1),
                    rent_low=rent_low,
                    rent_high=rent_high,
                    availability_status="AVAILABLE",
                    availability_date=avail_date,
                    source_api_url=source_url,
                    extraction_tier="TIER_1_API_RENTCAFE_SECURECAFE",
                    source_ids=source_ids,
                )
            )
    return units


# ── RentCafe anchor-walk fallback ───────────────────────────────────────────
# 2026-05-27 (612-failure-grind row 2). Out of the box, the RentCafe adapter
# only drills to the well-known endpoints it already knows about:
# ``/wp-json/middleware/v1/getFloorplans/``, securecafe ``availableunits.aspx``,
# the rentcafe-hosted ``fp-unit`` table, and the Nestin per-plan layout. The
# 612-failure-grind refined probe rescued 34 RentCafe properties that publish
# units at a vanity sub-path discoverable only from the homepage's own
# anchors: ``/availability``, ``/apartments``, ``/floor-plans``,
# ``/rentcafe-listings``, ``/listings``. This helper mirrors the
# ``entrata_deep_probe.py`` anchor-walk: regex the homepage for those paths,
# fetch the top candidates via curl_cffi, and feed the strongest response
# into the existing RentCafe parsers (hosted-table ``fp-unit`` or securecafe
# ``AvailUnitRow``). It's the cheapest possible drill — single regex scan,
# capped at 6 candidate URLs, capped at the first that yields a parsable
# unit table.

_RC_ANCHOR_DRILL_RE = re.compile(
    r"""href=["']([^"']*?/(?:availability|apartments|floor-?plans|"""
    r"""rentcafe-listings|listings|availableunits)/?[^"']*)["']""",
    re.IGNORECASE,
)


def _discover_rentcafe_anchors(html: str, origin: str, base_url: str) -> list[str]:
    """Return de-duplicated absolute URLs for drill candidates found in *html*.

    Only same-host anchors are returned. Caller is expected to cap the
    candidate list (see ``_try_rentcafe_anchor_walk``).
    """
    if not html or not origin:
        return []
    from urllib.parse import urljoin, urlparse

    try:
        host = urlparse(origin).netloc
    except Exception:
        return []
    if not host:
        return []

    out: list[str] = []
    seen: set[str] = set()
    for m in _RC_ANCHOR_DRILL_RE.finditer(html):
        href = m.group(1).strip()
        if not href:
            continue
        if href.startswith(("http://", "https://")):
            full = href
        elif href.startswith("/"):
            full = urljoin(origin + "/", href.lstrip("/"))
        else:
            full = urljoin(base_url or (origin + "/"), href)
        try:
            ph = urlparse(full).netloc
        except Exception:
            continue
        if ph != host:
            continue
        # Skip obvious dead-ends.
        low = full.lower()
        if "/module/" in low or "/applic" in low or "#" in low.split("?", 1)[0]:
            if "#" in low.split("?", 1)[0]:
                full = full.split("#", 1)[0]
            else:
                continue
        if full in seen:
            continue
        seen.add(full)
        out.append(full)
    return out


async def _try_rentcafe_anchor_walk(
    page: Page,
    ctx: AdapterContext,
    result: AdapterResult,
) -> list[dict[str, Any]]:
    """Walk anchor-discovered availability paths from the rendered homepage.

    Tries the well-known RentCafe parsers on each candidate. Returns parsed
    unit dicts on first hit, ``[]`` on any failure. Never raises.
    """
    try:
        from ma_poc.pms.adapters._probe import probe_get
    except ImportError:
        return []
    try:
        from ma_poc.pms.adapters.generic import _get_page_html
        html = await _get_page_html(page, ctx)
    except Exception:
        html = ""
    if not html:
        fr = getattr(ctx, "fetch_result", None)
        body = getattr(fr, "body", None) if fr is not None else None
        if isinstance(body, bytes):
            html = body.decode("utf-8", errors="replace")
        elif isinstance(body, str):
            html = body
    if not html:
        return []

    origin = _origin_from_ctx(ctx)
    base_url = str(getattr(ctx, "base_url", "") or "")
    candidates = _discover_rentcafe_anchors(html, origin, base_url)
    if not candidates:
        return []

    # Cap to 6 to bound the request burst (matches entrata_deep_probe).
    for cand in candidates[:6]:
        try:
            r = probe_get(cand, timeout=20)
        except Exception as exc:
            result.errors.append(
                f"rentcafe-anchor-walk-fetch-error[{cand}]: "
                f"{type(exc).__name__}: {str(exc)[:80]}"
            )
            continue
        if getattr(r, "status_code", 0) != 200:
            continue
        body_text = getattr(r, "text", "") or ""
        if len(body_text) < 1500:
            continue

        # SecureCafe availableunits markup — reuse existing parser.
        if "AvailUnitRow" in body_text:
            units = parse_securecafe_availableunits(body_text, cand)
            if units:
                result.winning_url = cand
                return units

        # RentCafe-hosted SSR fp-unit table — reuse existing parser.
        if "fp-unit" in body_text:
            units = parse_rentcafe_hosted_table(body_text, cand)
            if units:
                result.winning_url = cand
                return units

        # 2026-07-12: RentCafe-vanity /floorplans pages SSR a
        # ``ysi.unitsList = [...]`` JSON array carrying full unit-level data
        # (UnitCode/Beds/Baths/SqFt/MinRent/AvailableDate/FloorplanId). The
        # anchor-walk previously only ran the AvailUnitRow and fp-unit
        # parsers on these pages, silently dropping the embedded JSON — so
        # the LLM-DOM tier later won on the SAME page (prod 2026-07-12
        # rentcafe plan-level cohort: parksouth/fairwood/regencyparknorth
        # had 9/11/12 units in ysi.unitsList yet shipped plan-level).
        if "ysi.unitsList" in body_text:
            units = parse_rentcafe_ysi_unitslist(body_text, cand)
            if units:
                result.winning_url = cand
                return units

    return []


_YSI_UNITSLIST_RE = re.compile(
    r"ysi\.unitsList\s*=\s*(\[.*?\])\s*;", re.DOTALL
)


def parse_rentcafe_ysi_unitslist(html: str, source_url: str) -> list[dict[str, Any]]:
    """Parse a RentCafe-vanity page's ``ysi.unitsList`` JSON into unit dicts.

    RentCafe marketing themes SSR the full unit inventory as a JavaScript
    array assigned to ``ysi.unitsList``. Each element is a per-unit object
    with (case varies across themes) UnitCode/ApartmentName, Beds/Baths,
    SqFt, MinRent/MaxRent (or Rent), AvailableDate, FloorplanName and
    FloorplanId. Returns ``[]`` if the marker is absent or the JSON is
    unparseable (caller falls through to the next tier — never raises).
    """
    if "ysi.unitsList" not in html:
        return []
    m = _YSI_UNITSLIST_RE.search(html)
    if not m:
        return []
    try:
        rows = json.loads(m.group(1))
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(rows, list):
        return []

    def _pick(d: dict[str, Any], *keys: str) -> Any:
        """First present, non-empty value among *keys* (case-insensitive)."""
        lower = {k.lower(): v for k, v in d.items()}
        for k in keys:
            v = lower.get(k.lower())
            if v not in (None, ""):
                return v
        return None

    units: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        unit_no = _pick(row, "UnitCode", "ApartmentName", "UnitName", "Name")
        if not unit_no:
            continue
        rent_min = _pick(row, "MinRent", "Rent", "MarketRent", "StartingRent")
        rent_max = _pick(row, "MaxRent") or rent_min
        rent_low = money_to_int(str(rent_min)) if rent_min is not None else None
        rent_high = money_to_int(str(rent_max)) if rent_max is not None else rent_low
        fpid = _pick(row, "FloorplanId", "FloorPlanId", "FloorplanID")
        source_ids: dict[str, Any] = {}
        if fpid:
            source_ids["securecafe_floorplan_id"] = str(fpid)
        sqft_val = _pick(row, "SqFt", "SquareFeet", "Sqft")
        units.append(
            make_unit_dict(
                floor_plan_name=str(_pick(row, "FloorplanName", "FloorPlanName") or ""),
                bedrooms=str(_pick(row, "Beds", "Bedrooms") or ""),
                bathrooms=str(_pick(row, "Baths", "Bathrooms") or ""),
                sqft=str(sqft_val).replace(",", "") if sqft_val is not None else "",
                unit_number=str(unit_no),
                rent_low=rent_low,
                rent_high=rent_high,
                availability_status="AVAILABLE",
                availability_date=str(_pick(row, "AvailableDate", "DateAvailable") or ""),
                source_api_url=source_url,
                extraction_tier="TIER_1_API_RENTCAFE_YSI_UNITSLIST",
                source_ids=source_ids,
            )
        )
    return units


async def _try_rentcafe_securecafe_probe(
    ctx: AdapterContext, result: AdapterResult
) -> list[dict[str, Any]]:
    """SHAPE_REJECTED fallback: follow the securecafe online-leasing portal
    and parse ``availableunits.aspx`` for true unit-level inventory.

    Returns parsed unit dicts on success, ``[]`` on any failure.
    """
    html = ""
    fr = getattr(ctx, "fetch_result", None)
    body = getattr(fr, "body", None) if fr is not None else None
    if isinstance(body, bytes):
        html = body.decode("utf-8", errors="replace")
    elif isinstance(body, str):
        html = body

    # 2026-05-23: try ALL securecafe bases the page links to, not just
    # the first match. 11 of 54 RentCafe SHAPE_REJECTED properties have
    # ≥2 distinct SC slugs on the homepage (portfolio sibling pattern —
    # Majestic Vernon Hills' page links to Forest Cove first, then
    # Majestic). Picking the first match → wrong sibling → 0 units →
    # SHAPE_REJECTED. Try each; first one with AvailUnitRow rows wins.
    bases = _find_all_securecafe_bases(html, ctx)
    if not bases:
        # 2026-05-17 iter-9 (do-or-die root cause): the patchright-rendered
        # fetch_result.body lacks the securecafe link in a regex-matchable
        # form, and the link-hop-discovered securecafe URL never reaches
        # the adapter (it's in separate link-hop fetch events, not in
        # ctx._api_responses). But the RAW server HTML of the property's
        # own homepage DOES contain it (link-hop extracted it from there;
        # standalone curl_cffi of taylorspond.ticonproperties.com →
        # taylorspond-ticonproperties.securecafe.com/onlineleasing/
        # taylor-s-pond present). patchright also gets 403 CF-blocked on
        # securecafe directly. So: re-fetch the property's own homepage
        # via curl_cffi (bypasses CF + gives raw server HTML) and scan
        # that for the securecafe bases.
        origin = _origin_from_ctx(ctx)
        if origin:
            try:
                from ma_poc.pms.adapters._probe import probe_get

                _hr = probe_get(origin, timeout=20)
                if _hr.status_code == 200 and _hr.text:
                    bases = _find_all_securecafe_bases(_hr.text, ctx)
            except Exception as _hp_exc:
                result.errors.append(
                    f"rentcafe-securecafe-homepage-refetch-error: "
                    f"{type(_hp_exc).__name__}: {str(_hp_exc)[:100]}"
                )
    if not bases:
        return []

    try:
        from ma_poc.pms.adapters._probe import probe_get
    except ImportError:
        result.errors.append("rentcafe-securecafe: curl_cffi not installed")
        return []

    # Try each candidate base; first one with AvailUnitRow rows wins.
    # Cap at 3 to bound the request burst when a portfolio links many
    # siblings (always tries the first 3 — almost always enough).
    #
    # 2026-05-24 (post-canary diagnosis): for each candidate, try
    # DIRECT (proxies={}) FIRST, then fall back to PROXIED. Same root
    # cause as the no_body residue (commit a303462) — BrightData's
    # residential IP pool gets blocked on a meaningful subset of
    # SecureCafe hosts (~40% of the SHAPE_REJECTED cohort: probed
    # 5 sample drill URLs, direct = 3/5 with AvailUnitRow, proxied =
    # only 2/5). Direct works because GCP worker IP isn't on the
    # operator's per-IP blocklist for the SC subdomain (different from
    # the marketing-vanity-host blocklist that catches GCP — Yardi's
    # securecafe.com tenant has its own IP rules).
    au_url = ""
    page_html = ""
    import os as _os
    proxy_available = bool(_os.environ.get("PROBE_PROXY_URL", "").strip())
    for candidate_base in bases[:3]:
        candidate_au = f"{candidate_base}/availableunits.aspx"
        body_text = ""
        # Attempt 1: DIRECT (no proxy).
        try:
            r = probe_get(candidate_au, timeout=25, proxies={}, verify=True)
            if r.status_code == 200:
                body_text = r.text or ""
        except Exception as exc:
            result.errors.append(
                f"rentcafe-securecafe-direct-fetch-error[{candidate_base}]: "
                f"{type(exc).__name__}: {str(exc)[:80]}"
            )
        # Attempt 2: PROXIED (if direct didn't yield AvailUnitRow AND
        # proxy is configured). Some Yardi tenants explicitly block GCP
        # ranges on the SC subdomain — the residential proxy is the only
        # path for those.
        if "AvailUnitRow" not in body_text and proxy_available:
            try:
                r = probe_get(candidate_au, timeout=25)
                if r.status_code == 200:
                    body_text = r.text or ""
            except Exception as exc:
                result.errors.append(
                    f"rentcafe-securecafe-proxied-fetch-error[{candidate_base}]: "
                    f"{type(exc).__name__}: {str(exc)[:80]}"
                )
        # Attempt 3: WEB UNLOCKER (2026-07-16, Lever 1). Direct + proxied both
        # miss SecureCafe's HTTP-200 CF/JS challenge shells — Yardi's Cloudflare
        # tenant returns a 200 challenge body (not a 403), so probe_get's
        # _looks_blocked gate never escalates and the drill silently sees no
        # AvailUnitRow. Escalate explicitly whenever rows are still missing and
        # a Web Unlocker token is set — independent of PROBE_PROXY_URL and of
        # _looks_blocked. No-op (returns empty) when WEB_UNLOCKER_KEY is unset.
        if "AvailUnitRow" not in body_text:
            try:
                from ma_poc.pms.adapters._probe import (
                    web_unlocker_get,
                    web_unlocker_key,
                )
                if web_unlocker_key():
                    wu = web_unlocker_get(candidate_au, timeout=120)
                    if wu.status_code == 200 and "AvailUnitRow" in (wu.text or ""):
                        body_text = wu.text or ""
            except Exception as exc:
                result.errors.append(
                    f"rentcafe-securecafe-unlocker-fetch-error[{candidate_base}]: "
                    f"{type(exc).__name__}: {str(exc)[:80]}"
                )
        if "AvailUnitRow" not in body_text:
            continue
        # Found one with actual unit rows.
        au_url = candidate_au
        page_html = body_text
        break

    if not page_html:
        return []
    units = parse_securecafe_availableunits(page_html, au_url)

    # 2026-05-22: apts247 floorplan-meta enrichment. ~6/50 securecafe-
    # plan-level properties are apts247-backed (Yardi marketing): the
    # SecureCafe AvailUnitRow markup omits the Sq.Ft cell (or has it
    # blank), but the same property's apts247 ``/api/v3/floorplans/all/
    # ?api_key=KEY`` endpoint returns per-plan sq_ft + bed + bath +
    # name, keyed by ``feed_id`` (== SecureCafe FloorPlanID). Without
    # this merge, scraper.py's ``no_area`` trigger fires → all retries
    # fail → SUCCESS but tier stamped ``_PLAN_LEVEL`` despite real
    # unit-level data. Only attempts when ≥1 unit lacks sqft AND the
    # homepage is apts247-backed (the ``window.api_key`` signal),
    # so it is a no-op for the ~44/50 non-apts247 properties in the
    # cohort. Best-effort: any error → units returned unchanged.
    if units and any(not u.get("sqft") or str(u.get("sqft")) == "0" for u in units):
        # First pass — zero-cost: some operators encode sqft in the SC
        # plan-name header itself, e.g. "1x1 534" / "2x2 988". Lifts
        # gravity255-style properties without any network round-trip.
        _enrich_securecafe_units_from_plan_name(units)
        # Second pass — RentCafe-WP plan-cards: the homepage HTML we
        # already have in hand may carry <article class="floorplans-box"
        # data-price=… data-beds=…> cards with sqft inline and a
        # /floorplan/<id> href whose ID exactly matches the SecureCafe
        # FloorPlanID. Lifts the ironstate.com portfolio (5 properties,
        # 104 units) deterministically. Zero-cost when no marker present.
        if any(not u.get("sqft") or str(u.get("sqft")) == "0" for u in units):
            _enrich_securecafe_units_with_wp_cards(units, html, result)
        # Third pass — only if gaps remain: apts247 plan-meta API.
        if any(not u.get("sqft") or str(u.get("sqft")) == "0" for u in units):
            _enrich_securecafe_units_with_apts247(units, ctx, html, result)
        # Fourth pass — only if gaps STILL remain: fetch the vanity
        # marketing /floorplans page and FK-join plan sqft onto units
        # by (beds, baths). Covers the residual ~52-prop cohort where
        # the operator publishes sqft on the public-facing page but
        # neither apts247 nor RentCafe-WP plan-cards carry it — most
        # of these are non-apts247 Yardi vanity sites (ysi.floorplansList
        # JSON), data-attr-sprinkled marketing templates, or English-prose
        # marketing copy ("1 Bedroom, 1 Bathroom 700 sq. ft.").
        if any(not u.get("sqft") or str(u.get("sqft")) == "0" for u in units):
            _enrich_securecafe_units_with_vanity_floorplans(
                units, ctx, html, result
            )
        # Final pass — for units still missing sqft after every
        # enrichment path returned empty, declare the operator does not
        # publish sqft. This is honest provenance: the SC drill produced
        # rent + unit_number + plan_name (real unit-level data); we
        # tried 4 independent sources for sqft (plan-name, WP-cards,
        # apts247, vanity /floorplans) and all came back empty. ``_has_
        # area`` honors the flag so the ``no_area`` retry doesn't fire
        # → tier stays as TIER_1_API_RENTCAFE_SECURECAFE (not _PLAN_
        # LEVEL); the verdict lands as SUCCESS, not SUCCESS_PLAN_LEVEL.
        _flag_securecafe_units_operator_sqft_gap(units, result)
    if units:
        result.api_responses.append(
            {"url": au_url, "status": 200, "body": "<securecafe-html>", "via": "securecafe_probe"}
        )
        result.winning_url = au_url
    return units


# ─── SecureCafe sqft-from-plan-name enrichment (2026-05-22) ──────────

# Some operators encode sqft directly in the SecureCafe Floor Plan header
# string, e.g. ``Floor Plan: 1x1 534 - 1 Bedroom, 1 Bathroom`` (gravity255
# style — 29 units). The pattern is ``<beds>x<baths> <sqft>``, where sqft
# is 3-5 digits. Tight enough to avoid false matches on unit numbers or
# rent values (which carry $ or , markers); restricted to the plan-name
# field so this never sees raw row markup.
_SC_PLAN_NAME_SQFT_RE = re.compile(
    r"\b\d+\s*x\s*\d+(?:\.\d+)?\s+(\d{3,5})\b", re.IGNORECASE
)


def extract_sqft_from_sc_plan_name(plan_name: str) -> str:
    """Return the sqft string embedded in a SecureCafe plan name, or ''.

    Recognises the ``<beds>x<baths> <sqft>`` pattern only (e.g. "1x1
    534", "2x1.5 988"). Does NOT match arbitrary numbers — without the
    bedsxbaths prefix the digits could be anything.
    """
    if not plan_name:
        return ""
    m = _SC_PLAN_NAME_SQFT_RE.search(plan_name)
    return m.group(1) if m else ""


def _enrich_securecafe_units_from_plan_name(
    units: list[dict[str, Any]],
) -> int:
    """Fill missing sqft on each unit from the plan_name string when it
    embeds the ``<beds>x<baths> <sqft>`` pattern. In-place mutation.

    Returns the number of units that gained sqft. Zero-cost (no network),
    safe to call before the apts247 fallback.
    """
    if not units:
        return 0
    enriched = 0
    for u in units:
        existing = str(u.get("sqft") or "").strip()
        if existing and existing != "0":
            continue
        sqft = extract_sqft_from_sc_plan_name(str(u.get("floor_plan_name") or ""))
        if sqft:
            u["sqft"] = sqft
            enriched += 1
    return enriched


# ─── operator-not-published sqft flag (final safety net, 2026-05-23) ─────


def _flag_securecafe_units_operator_sqft_gap(
    units: list[dict[str, Any]], result: AdapterResult
) -> int:
    """Stamp ``data_gaps=["sqft"]`` + ``data_quality_flag="SQFT_NOT_
    PUBLISHED"`` on every unit that STILL lacks sqft after the full
    enrichment chain (plan-name regex → WP-cards → apts247 API).

    Called as the last step of the SecureCafe drill. Documents an
    honest provenance:
      - the SC drill produced rent + unit_number + plan_name
      - three independent enrichment paths returned empty
      - therefore the operator simply does not publish sqft for this
        unit. Treating it as "no_area retry" is wrong; the data isn't
        there to find.

    Schema_gate._has_area honors the flag so the retry doesn't fire →
    tier stays _SECURECAFE (not _PLAN_LEVEL) and the verdict lands
    SUCCESS, not SUCCESS_PLAN_LEVEL. Returns the number of units
    flagged for diagnostics.
    """
    if not units:
        return 0
    flagged = 0
    for u in units:
        sqft = str(u.get("sqft") or "").strip()
        if sqft and sqft != "0":
            continue
        # Defensive: never overwrite an existing gap list — append.
        gaps = u.get("data_gaps") or []
        if "sqft" not in gaps:
            gaps = list(gaps) + ["sqft"]
            u["data_gaps"] = gaps
        if not u.get("data_quality_flag"):
            u["data_quality_flag"] = "SQFT_NOT_PUBLISHED"
        flagged += 1
    if flagged:
        result.errors.append(
            f"securecafe-sqft-not-published: flagged {flagged} units — "
            f"operator does not publish sqft (3 enrichment paths exhausted)"
        )
    return flagged


# ─── RentCafe-WP plan-card enrichment (ironstate cluster, 2026-05-22) ────


def _enrich_securecafe_units_with_wp_cards(
    units: list[dict[str, Any]],
    page_html: str,
    result: AdapterResult,
) -> None:
    """Best-effort: parse RentCafe-WP ``<article class='floorplans-box'>``
    cards from the rendered homepage HTML; merge by FloorPlanID into
    SC units. No-op when the marker isn't present (the cheap
    ``has_wp_floorplan_cards`` gate keeps this hands-off for non-WP
    properties — including all 6 apts247 sites).
    """
    if not page_html:
        return
    try:
        from ma_poc.pms.adapters._rentcafe_wp_floorplan_cards import (
            has_wp_floorplan_cards,
            merge_wp_cards_into_securecafe,
            parse_wp_floorplan_cards,
        )
    except Exception as exc:
        result.errors.append(
            f"rentcafe-securecafe-wpcards-import-error: "
            f"{type(exc).__name__}: {str(exc)[:80]}"
        )
        return
    if not has_wp_floorplan_cards(page_html):
        return
    plans = parse_wp_floorplan_cards(page_html)
    if not plans:
        return
    n = merge_wp_cards_into_securecafe(units, plans)
    if n:
        result.errors.append(
            f"securecafe-wpcards-enrich: filled fields on {n} units "
            f"from {len(plans)} WP plan cards"
        )


# ─── apts247 plan-meta enrichment (SecureCafe sqft-gap fix, 2026-05-22) ──

# apts247 (Yardi marketing platform) embeds a per-property api_key in the
# rendered HTML. With the key, ``/api/v3/floorplans/all/`` returns plan
# objects carrying ``sq_ft``, ``bed``, ``bath``, ``name``, and ``feed_id``
# (the stable Yardi plan ID — same value as SecureCafe's FloorPlanID).
# Probed 2026-05-22 on longwoodsouthernhills.com (4 plans, 700/1080/1100/
# 1080 sq_ft) and waterfordvillagetn.com (5 plans, 900-1200 sq_ft).
_APTS247_API_KEY_RE = re.compile(
    r"""window\.api_key\s*=\s*['"]([a-f0-9]{20,80})['"]""", re.IGNORECASE
)
_APTS247_MARKER_RE = re.compile(r"apts247\.info", re.IGNORECASE)


def find_apts247_api_key(html: str) -> str:
    """Extract ``window.api_key = "<hex>"`` from rendered HTML, or ''.

    Returns empty string when the site is not apts247-backed (no js
    fragment present). Marker check is permissive — the key regex itself
    is precise enough (40-char hex token in a JS assignment).
    """
    if not html:
        return ""
    m = _APTS247_API_KEY_RE.search(html)
    return m.group(1) if m else ""


def fetch_apts247_floorplans(
    origin: str, api_key: str, timeout: int = 15
) -> list[dict[str, Any]]:
    """GET ``{origin}/api/v3/floorplans/all/?api_key=<key>`` → plan list.

    Returns a list of plan dicts (each carrying ``sq_ft``, ``bed``,
    ``bath``, ``name``, ``feed_id``, ``id``) on success, ``[]`` on any
    failure. The v3 endpoint returns a bare JSON array; the v1 fallback
    returns ``{meta, objects}``.
    """
    if not origin or not api_key:
        return []
    try:
        from ma_poc.pms.adapters._probe import probe_get

        url = f"{origin.rstrip('/')}/api/v3/floorplans/all/?api_key={api_key}"
        r = probe_get(url, timeout=timeout)
    except Exception:
        return []
    if getattr(r, "status_code", 0) != 200 or not r.text:
        return []
    try:
        j = json.loads(r.text)
    except (json.JSONDecodeError, ValueError):
        return []
    if isinstance(j, list):
        plans = j
    elif isinstance(j, dict):
        plans = j.get("objects") or []
    else:
        plans = []
    return [p for p in plans if isinstance(p, dict)]


def _normalize_plan_name(s: str) -> str:
    """Lowercase, strip punctuation, collapse spaces — apts247 "2 Bed 1.5
    Bath" vs SecureCafe "2Bed 1.5 Bath" must match."""
    if not s:
        return ""
    s = re.sub(r"[^a-z0-9.]+", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def merge_apts247_into_securecafe(
    units: list[dict[str, Any]], plans: list[dict[str, Any]]
) -> int:
    """Fill missing sqft (+ bed/bath/floor_plan_name when blank) on each
    SecureCafe unit from the apts247 plan list.

    Join strategy, in priority order:
      1. ``unit.source_ids['securecafe_floorplan_id'] == plan.feed_id``
         (exact — the same Yardi plan ID; preferred)
      2. ``normalize(unit.floor_plan_name) == normalize(plan.name)``
         (fuzzy — when feed_id is empty on the apts247 side or unit lacks
         FloorPlanID capture, e.g. older AvailUnitRow markup)
      3. ``(bed, bath)`` tuple match (last-resort, only when exactly one
         plan matches; ambiguous matches are skipped — better to leave
         sqft empty than to mis-fill across plans).

    Per-unit values WIN; meta only fills gaps. Returns the number of
    units that had at least one field filled (for diagnostics).
    """
    if not units or not plans:
        return 0
    # Build indexes once.
    by_feed: dict[str, dict[str, Any]] = {}
    by_name: dict[str, dict[str, Any]] = {}
    bedbath_buckets: dict[tuple[Any, Any], list[dict[str, Any]]] = {}
    for p in plans:
        fid = str(p.get("feed_id") or "").strip()
        if fid:
            by_feed.setdefault(fid, p)
        nm = _normalize_plan_name(str(p.get("name") or ""))
        if nm:
            by_name.setdefault(nm, p)
        try:
            key = (int(p.get("bed")), float(p.get("bath")))
            bedbath_buckets.setdefault(key, []).append(p)
        except (TypeError, ValueError):
            pass

    enriched = 0
    for u in units:
        plan: dict[str, Any] | None = None
        # 1. feed_id join
        fpid = str((u.get("source_ids") or {}).get("securecafe_floorplan_id") or "")
        if fpid and fpid in by_feed:
            plan = by_feed[fpid]
        # 2. plan-name fuzzy join
        if plan is None:
            nm = _normalize_plan_name(str(u.get("floor_plan_name") or ""))
            if nm and nm in by_name:
                plan = by_name[nm]
        # 3. (bed, bath) bucket join — only unambiguous
        if plan is None:
            try:
                key = (int(u.get("bedrooms")), float(u.get("bathrooms")))
            except (TypeError, ValueError):
                key = None  # type: ignore[assignment]
            if key and len(bedbath_buckets.get(key, [])) == 1:
                plan = bedbath_buckets[key][0]
        if plan is None:
            continue
        # Fill gaps only. apts247 sq_ft is a numeric string ("700"); some
        # entries are "0" — treat as missing.
        sqft = str(plan.get("sq_ft") or "").strip()
        if sqft and sqft != "0" and (not u.get("sqft") or str(u.get("sqft")) == "0"):
            u["sqft"] = sqft
            enriched += 1
            continue
        # Less common: fill name/bed/bath when they're blank but were
        # mis-captured from SecureCafe.
        if not u.get("floor_plan_name") and plan.get("name"):
            u["floor_plan_name"] = str(plan["name"])
            enriched += 1
    return enriched


def _enrich_securecafe_units_with_apts247(
    units: list[dict[str, Any]],
    ctx: AdapterContext,
    page_html: str,
    result: AdapterResult,
) -> None:
    """Best-effort: detect apts247 backing, fetch plan list, merge.

    Looks for the api_key in:
      1. ``page_html`` (the patchright fetch_result body passed in)
      2. ``{origin}/floorplans/`` (the page that reliably embeds the key
         even when the rendered home body misses it)
    The two-step lookup mirrors the existing securecafe-base recovery
    pattern (`_try_rentcafe_securecafe_probe`).

    Any error is swallowed — the SecureCafe drill result still ships.
    """
    api_key = find_apts247_api_key(page_html)
    origin = _origin_from_ctx(ctx)
    if not origin:
        return
    if not api_key:
        try:
            from ma_poc.pms.adapters._probe import probe_get

            fp_page = probe_get(f"{origin}/floorplans/", timeout=15)
            if fp_page.status_code == 200 and fp_page.text:
                api_key = find_apts247_api_key(fp_page.text)
        except Exception as exc:
            result.errors.append(
                f"rentcafe-securecafe-apts247-keyfetch-error: "
                f"{type(exc).__name__}: {str(exc)[:80]}"
            )
            return
    if not api_key:
        return
    plans = fetch_apts247_floorplans(origin, api_key)
    if not plans:
        return
    n = merge_apts247_into_securecafe(units, plans)
    if n:
        # Diagnostic only — does not alter tier or confidence.
        result.errors.append(
            f"securecafe-apts247-enrich: filled fields on {n} units "
            f"from {len(plans)} apts247 plans"
        )


# ─── Vanity /floorplans sqft FK-join (cluster B, 2026-05-25) ─────────────
#
# Cohort: TIER_1_API_RENTCAFE_SECURECAFE shows ~67% adapter-miss rate for
# sqft across ~52 properties (~475 units). The SC AvailUnitRow markup
# omits Sq.Ft for these operators, the apts247 fallback no-ops on non-
# apts247-backed sites, and the WP-cards fallback misses vanity templates
# that don't carry the floorplans-box article. The vanity marketing
# /floorplans page DOES publish sqft for ~67% of the cohort (~318 units
# recoverable); we just need to fetch it, parse plan-level (beds, baths,
# sqft, rent) tuples, and FK-join onto SecureCafe units by (beds, baths).
#
# Four patterns recognised, in priority order:
#
# (1) Yardi ysi.floorplansList JSON (alvista23, themtroyal, most Yardi
#     vanity sites): ``<script>ysi.floorplansList = [{"Id":..., "Beds":1,
#     "Baths":1.0, "MinSqFt":832, "MaxSqFt":832, "MinRent":1476, ...}]``.
#     Fully structured — most reliable. Note: ``minSqft`` in JS function
#     args (e.g. ``function setGA4Cookie(tour, fpname, size, minSqft,...)``)
#     is NOT a data record; the parser filters JS-template noise by
#     requiring the structured-object shape, not bare token presence.
#
# (2) data-beds=/data-baths=/data-sqft= attribute clusters (ardencebloom
#     and other RentCafe-WP marketing templates): co-located attributes
#     on a single element provide a tight (beds, baths, sqft) tuple.
#
# (3) "X Bed[room] / Y Bath / Z sq ft" slash-separated text (ardencebloom
#     visible card text): bed/bath/sqft on the same line with explicit
#     separators — tight enough to skip false matches in marketing prose.
#
# (4) "X Bedroom, Y Bathroom Z sq. ft." natural-language prose
#     (vestaviaplace): English copy listing bed/bath/sqft together. The
#     bed/bath co-location anchor keeps false positives near zero.
#
# All four patterns produce a uniform plan dict; the merge then walks
# each SC unit missing sqft, finds matching plans by (beds, baths), and
# picks the closest-rent plan when more than one matches.

#: Paths to try when fetching the vanity floorplans page. Order matters —
#: hyphenated variant covered vestaviaplace (Yardi marketing). Both are
#: tried even when one returns 200 if no plan data was parsed (defensive
#: against templates that serve a placeholder page on the wrong URL).
VANITY_FLOORPLAN_PATHS: tuple[str, ...] = ("/floorplans", "/floor-plans")

# ysi.floorplansList JSON shape — Yardi vanity-site template (alvista23,
# themtroyal, etc.). Pattern: ``ysi.floorplansList = [ {...}, {...} ]``.
# Captured greedily through the matching closing bracket; we use a stack-
# free balanced-bracket scan instead of regex to handle nested ``[]``
# arrays inside plan amenities.
_YSI_FP_LIST_START_RE = re.compile(
    r"ysi\.floorplansList\s*=\s*(\[)",
    re.IGNORECASE,
)

# Data-attribute cluster on a single element (ardencebloom unitPlaceholder
# style). Captures beds/baths/sqft in any order via three independent
# look-aheads on the same element opening tag.
_VANITY_DATA_ATTR_ELEM_RE = re.compile(
    r"<[^<>]*?\bdata-beds\s*=\s*['\"]\s*(?P<beds>\d+(?:\.\d+)?)"
    r"[^<>]*?\bdata-baths\s*=\s*['\"]\s*(?P<baths>\d+(?:\.\d+)?)"
    r"[^<>]*?\bdata-sqft\s*=\s*['\"]\s*(?P<sqft>\d{2,5})"
    r"[^<>]*?>",
    re.IGNORECASE | re.DOTALL,
)
# Mirror: same trio but the attribute order beds/sqft/baths is also seen
# in the wild (FortressTech-style markup variants). One reversed-order
# capture suffices; permutations are rare enough that the (beds, baths)
# join collapses any duplicates.
_VANITY_DATA_ATTR_ALT_RE = re.compile(
    r"<[^<>]*?\bdata-beds\s*=\s*['\"]\s*(?P<beds>\d+(?:\.\d+)?)"
    r"[^<>]*?\bdata-sqft\s*=\s*['\"]\s*(?P<sqft>\d{2,5})"
    r"[^<>]*?\bdata-baths\s*=\s*['\"]\s*(?P<baths>\d+(?:\.\d+)?)"
    r"[^<>]*?>",
    re.IGNORECASE | re.DOTALL,
)

# "X Bed / Y Bath / Z sq ft" slash-separated text. ardencebloom card label
# uses this exact shape. Tolerates "Bed/Bedroom"/"Bath/Bathroom" and
# whitespace around the slashes.
_VANITY_SLASH_RE = re.compile(
    r"(?P<beds>\d+)\s*(?:Bed|Bedroom)s?\s*/\s*"
    r"(?P<baths>\d+(?:\.\d+)?)\s*(?:Bath|Bathroom)s?\s*/\s*"
    r"(?P<sqft>\d{2,5})\s*sq",
    re.IGNORECASE,
)

# "X Bedroom, Y Bathroom Z sq. ft." natural-language prose (vestaviaplace).
# Requires the comma between bedroom and bathroom and the "sq" anchor to
# avoid catching unrelated number triples in marketing copy.
_VANITY_PROSE_RE = re.compile(
    r"(?P<beds>\d+)\s*Bedroom[s]?\s*,\s*"
    r"(?P<baths>\d+(?:\.\d+)?)\s*Bathroom[s]?\s+"
    r"(?P<sqft>\d{2,5})\s*sq\.?\s*ft",
    re.IGNORECASE,
)


def _slice_balanced_json_array(text: str, start: int) -> str:
    """Return the substring beginning at ``text[start]`` (which must point
    at an opening ``[``) up to and including the matching closing ``]``.

    Tracks bracket depth and respects string-literal escapes; falls back
    to the rest of ``text`` if no balance is found (unbalanced markup
    will then surface as ``json.loads`` failure downstream, which the
    caller treats as a no-op).
    """
    if start >= len(text) or text[start] != "[":
        return ""
    depth = 0
    in_str: str | None = None
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == in_str:
                in_str = None
            continue
        if ch in ('"', "'"):
            in_str = ch
            continue
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return ""


def parse_vanity_floorplans_for_sqft(html: str) -> list[dict[str, Any]]:
    """Extract plan-level (beds, baths, sqft, rent_lo, rent_hi, name)
    records from a rendered vanity ``/floorplans`` page.

    The four recognised patterns are tried independently; their outputs
    are merged + de-duplicated by (beds, baths, sqft). Returns ``[]``
    when no pattern produces any record. Numeric fields are returned
    as strings to match the SecureCafe dict shape (downstream callers
    expect ``unit["sqft"]`` to be a string).

    Each record dict carries:
      - ``beds``: int (0 for studio)
      - ``baths``: float
      - ``sqft``: int (single value; ysi MinSqFt/MaxSqFt collapsed to MinSqFt
        when equal, else stored as MinSqFt for join purposes — sqft cells
        in SC AvailUnitRows are single numeric values, so single-value
        is the right comparison shape)
      - ``rent_lo`` / ``rent_hi``: int or None (for closest-rent tie-break)
      - ``name``: str (best-effort plan name, may be empty)
    """
    if not html:
        return []
    out: list[dict[str, Any]] = []

    # (1) ysi.floorplansList JSON — most reliable. Locate the start, then
    # slice the balanced ``[ ... ]`` block and parse it. Defensive: if
    # parsing fails (template literal placeholders, single-quotes in
    # values), skip silently and let pattern 2-4 take over.
    m = _YSI_FP_LIST_START_RE.search(html)
    if m:
        blob = _slice_balanced_json_array(html, m.start(1))
        if blob:
            try:
                arr = json.loads(blob)
            except (json.JSONDecodeError, ValueError):
                arr = []
            for entry in arr if isinstance(arr, list) else []:
                if not isinstance(entry, dict):
                    continue
                beds_raw = entry.get("Beds", entry.get("beds"))
                if beds_raw is None:
                    continue
                try:
                    beds = int(float(beds_raw))
                except (TypeError, ValueError):
                    continue
                if beds < 0:
                    continue
                baths_raw = entry.get("Baths", entry.get("baths"))
                if baths_raw is None:
                    continue
                try:
                    baths = float(baths_raw)
                except (TypeError, ValueError):
                    continue
                if baths < 0:
                    continue
                # MinSqFt collapsed to int; 0 / blank is treated as missing.
                sqft_raw = (
                    entry.get("MinSqFt")
                    or entry.get("minSqFt")
                    or entry.get("minsqft")
                    or entry.get("min_sqft")
                    or 0
                )
                try:
                    sqft = int(float(sqft_raw))
                except (TypeError, ValueError):
                    continue
                if sqft <= 0:
                    continue
                rent_lo_raw = entry.get("MinRent") or entry.get("minRent")
                rent_hi_raw = (
                    entry.get("MaxRent") or entry.get("maxRent") or rent_lo_raw
                )
                try:
                    rent_lo = int(float(rent_lo_raw)) if rent_lo_raw else None
                except (TypeError, ValueError):
                    rent_lo = None
                try:
                    rent_hi = int(float(rent_hi_raw)) if rent_hi_raw else None
                except (TypeError, ValueError):
                    rent_hi = None
                name = str(
                    entry.get("Name") or entry.get("name") or entry.get("FpName") or ""
                ).strip()
                out.append(
                    {
                        "beds": beds,
                        "baths": baths,
                        "sqft": sqft,
                        "rent_lo": rent_lo,
                        "rent_hi": rent_hi,
                        "name": name,
                        "source": "ysi_floorplansList",
                    }
                )

    # (2) data-beds/baths/sqft attribute clusters.
    for regex, label in (
        (_VANITY_DATA_ATTR_ELEM_RE, "data_attr_bbs"),
        (_VANITY_DATA_ATTR_ALT_RE, "data_attr_bsb"),
    ):
        for em in regex.finditer(html):
            try:
                beds = int(float(em.group("beds")))
                baths = float(em.group("baths"))
                sqft = int(em.group("sqft"))
            except (TypeError, ValueError):
                continue
            if sqft <= 0 or beds < 0:
                continue
            out.append(
                {
                    "beds": beds,
                    "baths": baths,
                    "sqft": sqft,
                    "rent_lo": None,
                    "rent_hi": None,
                    "name": "",
                    "source": label,
                }
            )

    # (3) "X Bed / Y Bath / Z sq ft" slash-separated label.
    for sm in _VANITY_SLASH_RE.finditer(html):
        try:
            beds = int(sm.group("beds"))
            baths = float(sm.group("baths"))
            sqft = int(sm.group("sqft"))
        except (TypeError, ValueError):
            continue
        if sqft <= 0:
            continue
        out.append(
            {
                "beds": beds,
                "baths": baths,
                "sqft": sqft,
                "rent_lo": None,
                "rent_hi": None,
                "name": "",
                "source": "slash_label",
            }
        )

    # (4) "X Bedroom, Y Bathroom Z sq. ft." prose.
    for pm in _VANITY_PROSE_RE.finditer(html):
        try:
            beds = int(pm.group("beds"))
            baths = float(pm.group("baths"))
            sqft = int(pm.group("sqft"))
        except (TypeError, ValueError):
            continue
        if sqft <= 0:
            continue
        out.append(
            {
                "beds": beds,
                "baths": baths,
                "sqft": sqft,
                "rent_lo": None,
                "rent_hi": None,
                "name": "",
                "source": "prose",
            }
        )

    # De-duplicate by (beds, baths, sqft). The ardencebloom template, for
    # example, emits the same plan via both data-attrs AND the slash label
    # → the merge would otherwise see the bucket as ambiguous and trigger
    # the closest-rent tie-break on identical sqft values (still correct,
    # but wastes diagnostic noise).
    seen: set[tuple[int, float, int]] = set()
    deduped: list[dict[str, Any]] = []
    for p in out:
        key = (int(p["beds"]), float(p["baths"]), int(p["sqft"]))
        if key in seen:
            # Preserve rent metadata from the FIRST occurrence (which is
            # ysi.floorplansList when present — that's the structured
            # source carrying rent_lo/rent_hi).
            continue
        seen.add(key)
        deduped.append(p)
    return deduped


def fetch_vanity_floorplans_html(origin: str, timeout: int = 15) -> str:
    """Probe ``{origin}/floorplans`` then ``{origin}/floor-plans``; return
    the first body that yields any parseable plan record, else ``''``.

    Each candidate URL is fetched via curl_cffi (chrome120 impersonation
    through PROBE_PROXY_URL when set). The function does NOT raise — any
    transport error returns ``""`` and the caller's enrichment chain
    silently moves to the operator-gap flag pass.
    """
    if not origin:
        return ""
    try:
        from ma_poc.pms.adapters._probe import probe_get
    except ImportError:
        return ""
    for path in VANITY_FLOORPLAN_PATHS:
        url = f"{origin.rstrip('/')}{path}"
        try:
            r = probe_get(url, timeout=timeout)
        except Exception:
            continue
        if getattr(r, "status_code", 0) != 200:
            continue
        body = getattr(r, "text", "") or ""
        if not body:
            continue
        # Only return if we can actually parse at least one plan record.
        # If the operator serves a near-empty page on /floorplans but the
        # real listing lives at /floor-plans (or vice-versa), keep going.
        if parse_vanity_floorplans_for_sqft(body):
            return body
    return ""


def merge_vanity_floorplans_into_securecafe(
    units: list[dict[str, Any]], plans: list[dict[str, Any]]
) -> int:
    """FK-join plan sqft → SecureCafe units missing sqft, keyed on
    ``(beds, baths)``. When more than one plan shares the (beds, baths)
    bucket, pick the plan whose rent range is closest to the unit's rent
    (best-guess FK). When no plan in the bucket carries rent metadata,
    fall back to the median-sqft plan; when only one plan matches, use
    it directly.

    Per-unit values WIN; the merge only fills units whose sqft is empty
    or "0". Returns the number of units that gained sqft for diagnostics.
    """
    if not units or not plans:
        return 0
    # Bucket plans by (beds, baths).
    buckets: dict[tuple[int, float], list[dict[str, Any]]] = {}
    for p in plans:
        try:
            key = (int(p["beds"]), float(p["baths"]))
        except (TypeError, ValueError, KeyError):
            continue
        buckets.setdefault(key, []).append(p)

    enriched = 0
    for u in units:
        existing = str(u.get("sqft") or "").strip()
        if existing and existing != "0":
            continue
        try:
            beds = int(float(str(u.get("bedrooms") or "").strip() or "x"))
            baths = float(str(u.get("bathrooms") or "").strip() or "x")
        except (TypeError, ValueError):
            continue
        candidates = buckets.get((beds, baths))
        if not candidates:
            continue
        if len(candidates) == 1:
            chosen = candidates[0]
        else:
            # Multi-plan bucket — pick closest by rent. Unit rent comes
            # from market_rent_low (preferred) / market_rent_high; plan
            # rent uses (rent_lo + rent_hi) / 2 when both present, else
            # whichever is present, else fall back to median sqft (most
            # likely the canonical plan).
            unit_rent = u.get("market_rent_low") or u.get("market_rent_high")
            try:
                unit_rent_v = float(unit_rent) if unit_rent is not None else None
            except (TypeError, ValueError):
                unit_rent_v = None

            def _plan_rent(p: dict[str, Any]) -> float | None:
                lo, hi = p.get("rent_lo"), p.get("rent_hi")
                try:
                    if lo is not None and hi is not None:
                        return (float(lo) + float(hi)) / 2.0
                    if lo is not None:
                        return float(lo)
                    if hi is not None:
                        return float(hi)
                except (TypeError, ValueError):
                    return None
                return None

            if unit_rent_v is not None and any(
                _plan_rent(c) is not None for c in candidates
            ):
                # Bind unit_rent_v as a default arg to satisfy B023 — the
                # closure is consumed synchronously by ``min(...)`` so the
                # late-binding bug B023 guards against can't fire here, but
                # the explicit binding keeps the lint clean and intent
                # obvious to a future reader.
                def _dist(
                    p: dict[str, Any], _rent: float = unit_rent_v
                ) -> float:
                    pr = _plan_rent(p)
                    if pr is None:
                        # Plans without rent are deprioritised but still
                        # selectable when no other plan has rent metadata.
                        return float("inf")
                    return abs(pr - _rent)

                chosen = min(candidates, key=_dist)
            else:
                # No rent signal anywhere — pick the median-sqft plan.
                # Stable: sort by sqft ascending, pick the middle index.
                ordered = sorted(candidates, key=lambda p: int(p["sqft"]))
                chosen = ordered[len(ordered) // 2]
        try:
            sqft_val = int(chosen["sqft"])
        except (TypeError, ValueError, KeyError):
            continue
        if sqft_val <= 0:
            continue
        u["sqft"] = str(sqft_val)
        enriched += 1
    return enriched


def _enrich_securecafe_units_with_vanity_floorplans(
    units: list[dict[str, Any]],
    ctx: AdapterContext,
    page_html: str,
    result: AdapterResult,
) -> None:
    """Best-effort: fetch the vanity ``/floorplans`` page, parse plan-
    level sqft, FK-join onto SecureCafe units by (beds, baths).

    Parses the homepage HTML we already have in hand first — for some
    vanity templates the ysi.floorplansList JSON is embedded site-wide.
    Only spends a network round-trip when the in-hand parse returns
    nothing. Any error is swallowed so the SC drill still ships its
    rent + unit_number + plan_name payload (just with sqft gaps left
    for the operator-gap flag pass to stamp honestly).
    """
    if not units:
        return
    # First — try the rendered homepage HTML we already have. Saves a
    # round-trip on the alvista23-style sites where ysi.floorplansList is
    # injected on every page (including the home page).
    plans = parse_vanity_floorplans_for_sqft(page_html or "")
    via = "homepage_html"
    if not plans:
        origin = _origin_from_ctx(ctx)
        if not origin:
            return
        vp_html = fetch_vanity_floorplans_html(origin)
        if not vp_html:
            return
        plans = parse_vanity_floorplans_for_sqft(vp_html)
        via = "vanity_floorplans_probe"
    if not plans:
        return
    n = merge_vanity_floorplans_into_securecafe(units, plans)
    if n:
        result.errors.append(
            f"securecafe-vanity-floorplans-enrich: filled sqft on {n} units "
            f"from {len(plans)} vanity plans (via={via})"
        )
