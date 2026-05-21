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

        # Failure path: re-stamp tier_used with a structured sub-code so the
        # downstream report can distinguish misrouting from genuine zero data.
        tier_code, err_msg = _classify_rentcafe_failure(api_responses)
        result.tier_used = tier_code
        result.confidence = 0.0
        result.errors.append(err_msg)
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

_SECURECAFE_FP_HDR_RE = re.compile(
    r"Floor\s+Plan:\s*(?P<name>[^<\-]{1,80}?)\s*-\s*"
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


def _find_securecafe_base(html: str, ctx: AdapterContext) -> str | None:
    """Return ``https://<sub>.(securecafe|securecafenet).com/onlineleasing/<slug>``
    or None.

    Looks in the rendered HTML first (marketing sites link/iframe to their
    securecafe portal), then falls back to the property's own host when it
    *is* a securecafe portal.
    """
    if html:
        m = _SECURECAFE_URL_RE.search(html)
        if m:
            return _securecafe_base_from_match(m)
    # 2026-05-17 iter-7: the rendered fetch_result.body frequently lacks a
    # clean securecafe link (patchright DOM differs from raw server HTML;
    # link injected post-render or behind a menu), but the scraper's
    # network log almost always captured a securecafe portal request
    # (guestlogin.aspx / userlogin.aspx / floorplans). taylorspond
    # canary: body had no match, captured response #7 was
    # taylorspond-ticonproperties.securecafe.com/onlineleasing/taylor-s-pond/
    # guestlogin.aspx. Scan captured response URLs as a second source.
    for resp in getattr(ctx, "_api_responses", []) or []:
        u = str(resp.get("url", "") or "")
        ul = u.lower()
        if "securecafe.com/" not in ul and "securecafenet.com/" not in ul:
            continue
        m = _SECURECAFE_URL_RE.search(u)
        if m:
            return _securecafe_base_from_match(m)
    origin = _origin_from_ctx(ctx)
    if "securecafe.com" in origin or "securecafenet.com" in origin:
        # Host is the portal; recover the slug from the effective URL path.
        fr = getattr(ctx, "fetch_result", None)
        final = str(getattr(fr, "final_url", "") or "") if fr else ""
        m = _SECURECAFE_URL_RE.search(final or origin)
        if m:
            return _securecafe_base_from_match(m)
    return None


def _beds_from_text(bedtxt: str) -> int:
    bedtxt = bedtxt.strip().lower()
    if bedtxt.startswith("studio"):
        return 0
    m = re.match(r"(\d+)", bedtxt)
    return int(m.group(1)) if m else 0


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

    base = _find_securecafe_base(html, ctx)
    if not base:
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
        # that for the securecafe base.
        origin = _origin_from_ctx(ctx)
        if origin:
            try:
                from ma_poc.pms.adapters._probe import probe_get

                _hr = probe_get(origin, timeout=20)
                if _hr.status_code == 200 and _hr.text:
                    base = _find_securecafe_base(_hr.text, ctx)
            except Exception as _hp_exc:
                result.errors.append(
                    f"rentcafe-securecafe-homepage-refetch-error: "
                    f"{type(_hp_exc).__name__}: {str(_hp_exc)[:100]}"
                )
    if not base:
        return []
    au_url = f"{base}/availableunits.aspx"

    try:
        from ma_poc.pms.adapters._probe import probe_get
    except ImportError:
        result.errors.append("rentcafe-securecafe: curl_cffi not installed")
        return []

    try:
        r = probe_get(au_url, timeout=25)
    except Exception as exc:
        result.errors.append(
            f"rentcafe-securecafe-fetch-error: {type(exc).__name__}: {str(exc)[:120]}"
        )
        return []
    if r.status_code != 200:
        return []
    page_html = r.text or ""
    # CF challenge shell is tiny and carries no unit rows.
    if "AvailUnitRow" not in page_html:
        return []
    units = parse_securecafe_availableunits(page_html, au_url)
    if units:
        result.api_responses.append(
            {"url": au_url, "status": 200, "body": "<securecafe-html>", "via": "securecafe_probe"}
        )
        result.winning_url = au_url
    return units
