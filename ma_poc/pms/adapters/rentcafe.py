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

import os
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


# ─────────────────────────────────────────────────────────────────────
# 2026-05-22: Per-stage telemetry for RentCafe recovery paths. Shared
# helpers live in ``_adapter_telemetry`` — every PMS adapter calls into
# the same helpers so the analyzer's tier_attempted aggregation picks
# up signals uniformly.
# ─────────────────────────────────────────────────────────────────────


from ma_poc.pms.adapters._adapter_telemetry import (  # noqa: E402  (import-after-docs)
    CF_CHALLENGE_MARKERS as _CF_CHALLENGE_MARKERS,
    log_adapter_diag as _log_adapter_diag,
    log_adapter_stage as _log_adapter_stage,
)


def _log_rc(
    property_id: str,
    stage: str,
    outcome: str,
    *,
    units: int = 0,
    reason: str = "",
    **extra: Any,
) -> None:
    """Thin wrapper over :func:`log_adapter_stage` so the existing
    rentcafe callsites don't have to change. Routes to the shared helper.
    """
    _log_adapter_stage(
        "rentcafe",
        property_id,
        stage,
        outcome,
        units=units,
        reason=reason,
        **extra,
    )


# Backward-compat shims for the in-rentcafe-namespace names used by tests
# elsewhere. The implementations live in ``_adapter_telemetry``.

from ma_poc.pms.adapters._adapter_telemetry import (  # noqa: E402  (import-after-docs)
    capture_body_diagnostics as _capture_body_diagnostics,
)


def _log_rc_diag(property_id: str, stage: str, body: str, *, reason: str = "") -> None:
    """Thin wrapper so the existing rentcafe callsites don't have to change."""
    _log_adapter_diag("rentcafe", property_id, stage, body, reason=reason)


def _classify_probe_body(status: int, body: str) -> tuple[str, str]:
    """Categorise a probe response into (outcome, reason).

    Distinguishes CF challenge shells from genuine 200-OK empty bodies
    from real success — the three look identical to ``status==200`` but
    are very different diagnostically.
    """
    body = body or ""
    body_len = len(body)
    if status != 200:
        # Surface CF/anti-bot status codes distinctly.
        if status in (403, 429, 503):
            return (f"blocked_status_{status}", f"len={body_len}")
        return (f"status_{status}", f"len={body_len}")
    # 200 with a CF challenge shell — body is small (<10KB) and contains
    # a CF-platform marker. The drill treats this as failure but main's
    # silent ``return []`` made it indistinguishable from "no data".
    cf_hit = any(m in body for m in _CF_CHALLENGE_MARKERS)
    if cf_hit and body_len < 15000:
        return ("cf_challenge_shell", f"len={body_len} markers_seen=True")
    if "AvailUnitRow" in body:
        return ("ok", f"len={body_len} avail_rows={body.count('AvailUnitRow')}")
    return ("no_avail_row", f"len={body_len} cf_markers={cf_hit}")

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
    # Gate SHAPE_REJECTED on JSON-shaped responses. The L1 fetcher's
    # network log buffers every response with content-type containing
    # json|xml|html|text — so the entry page's own HTML body alone makes
    # ``api_responses`` non-empty for every successful fetch. Without this
    # gate the run mislabels marketing-shell sites (where no RentCafe JSON
    # XHR was ever intercepted) as SHAPE_REJECTED, hiding the real failure
    # mode (no API capture, usually because data lives behind a CF-blocked
    # securecafe.com portal hop).
    #
    # A response counts as a RentCafe-API candidate when EITHER:
    #  - content-type contains "json" AND the host is in the RentCafe/Yardi
    #    family (rentcafe.com, securecafe.com, yardi*.com, etc.), OR
    #  - the body is already a dict/list with no content_type tag (test
    #    fixtures and pre-parsed callers).
    # Without the host gate, third-party tracking JSON (callrail, gtm,
    # osano consent banners, etc.) sneaks through and the run mislabels
    # marketing-shell properties as SHAPE_REJECTED.
    _RC_FAMILY_HOST_TOKENS = (
        "rentcafe.com",
        "securecafe.com",
        "yardi.com",
        "yardione.com",
        "yardiasp.com",
    )

    def _is_rentcafe_candidate(resp: dict[str, Any]) -> bool:
        ct = str(resp.get("content_type") or "").lower()
        url = str(resp.get("url") or "").lower()
        if "json" in ct:
            # Only count JSONs hosted on the RentCafe/Yardi family — third-party
            # trackers (callrail, gtm, osano, …) are not API candidates here.
            return any(tok in url for tok in _RC_FAMILY_HOST_TOKENS)
        if not ct and isinstance(resp.get("body"), (dict, list)):
            return True
        return False

    json_responses = [r for r in api_responses if _is_rentcafe_candidate(r)]
    if not json_responses:
        return (
            _TIER_NO_RESPONSE,
            f"RENTCAFE_NO_RESPONSE: {len(api_responses)} non-JSON responses captured "
            "(HTML/text only — no API XHR intercepted on entry or hops; "
            "RentCafe data likely behind a portal that didn't fire its API)",
        )
    shape_matches = [r for r in json_responses if _is_rentcafe_response(r.get("body"))]
    if not shape_matches:
        return (
            _TIER_SHAPE_REJECTED,
            f"RENTCAFE_SHAPE_REJECTED: {len(json_responses)} JSON responses captured, "
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

    async def try_dom(self, page: Any, html: str, ctx: AdapterContext) -> Any:
        """2026-05-24 Phase 1 cascade hook — deterministic DOM extraction
        for RentCafe hosted-table marketing pages (the ``.fp-unit`` /
        ``data-unit-name`` shape served by the canonical hosted
        RentCafe layout).

        The nestin per-plan crawl is intentionally NOT invoked here —
        it needs live navigation across sub-pages which is the
        responsibility of the cascade-level link-hop. ``try_dom`` is
        focused on extracting units from the captured HTML body.

        Returns ``AdapterDomResult.empty(...)`` when the HTML doesn't
        contain the ``.fp-unit`` markers OR the parser returns no
        candidates. Returns a high-confidence result when the hosted
        table is recognised.
        """
        from ma_poc.pms.adapters.base import AdapterDomResult

        if not html or "fp-unit" not in html:
            return AdapterDomResult.empty(
                tier="TIER_3_DOM_RENTCAFE_HOSTED",
                reason="no_fp_unit_marker",
            )
        try:
            url = getattr(ctx, "base_url", "") or ""
            raw_units = parse_rentcafe_hosted_table(html, url)
        except Exception as e:
            return AdapterDomResult.empty(
                tier="TIER_3_DOM_RENTCAFE_HOSTED",
                reason=f"parse_exception:{type(e).__name__}",
            )
        if not raw_units:
            return AdapterDomResult.empty(
                tier="TIER_3_DOM_RENTCAFE_HOSTED",
                reason="parser_silent_empty",
            )
        try:
            from ma_poc.extraction.dq_guards import apply_unit_guards
            guarded = apply_unit_guards(
                raw_units,
                property_id=getattr(ctx, "property_id", ""),
                source_html=html,
                detect_same_rent=True,
            )
        except Exception:
            guarded = raw_units
        if not guarded:
            return AdapterDomResult.empty(
                tier="TIER_3_DOM_RENTCAFE_HOSTED",
                reason="dq_guards_rejected_all",
            )
        return AdapterDomResult(
            units=guarded,
            plan_summaries=[],
            tier_used="TIER_3_DOM_RENTCAFE_HOSTED",
            selector_signature="tr.fp-unit[data-unit-name]",
            confidence=0.95,
            debug={"raw_count": len(raw_units), "guarded_count": len(guarded)},
        )

    async def extract(self, page: Page, ctx: AdapterContext) -> AdapterResult:
        """Extract units from RentCafe API responses captured during page load."""
        pid = getattr(ctx, "property_id", "") or "unknown"
        result = AdapterResult(tier_used=_TIER_BASE)
        all_units: list[dict[str, str]] = []

        # XHR-capture stage telemetry: how many of the network_log responses
        # matched our RentCafe-shape predicate. The single most common cause
        # of "all_units empty" is "captured 0 rentcafe-shape responses" —
        # surface that distinctly from "captured many but parser dropped all".
        api_responses: list[dict[str, Any]] = getattr(ctx, "_api_responses", [])
        n_total_resp = len(api_responses)
        n_matched_shape = 0
        for resp in api_responses:
            body = resp.get("body")
            if not _is_rentcafe_response(body):
                continue
            n_matched_shape += 1
            items = _unwrap_rentcafe_list(body)
            if not items:
                continue
            url = resp.get("url", "")
            units = parse_rentcafe_floorplans(items, url)
            if units:
                all_units.extend(units)
                result.api_responses.append(resp)

        _log_rc(
            pid,
            "xhr_capture",
            "ok" if all_units else (
                "shape_matched_no_units" if n_matched_shape else
                "no_rentcafe_shape_responses"
            ),
            units=len(all_units),
            reason=f"network_responses={n_total_resp} matched_shape={n_matched_shape}",
        )

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
                # 2026-05-21 (port of feature-branch SecureCafe drill-down):
                # the network getFloorplans response is often plan-level
                # only (one row per floorplan with min/max rent). When
                # admitted set is plan-level only (no unit_number), drill
                # into the securecafe online-leasing portal which carries
                # the real unit-level inventory. Prefer unit-level; fall
                # back to plan-level if the drill-down fails.
                _has_unit_level = any(
                    str(u.get("unit_number") or "").strip() for u in pp.units
                )
                if not _has_unit_level:
                    sc_units = await _try_rentcafe_securecafe_probe(ctx, result)
                    if sc_units:
                        sc_pp = post_process(
                            sc_units,
                            property_id=getattr(ctx, "property_id", None),
                        )
                        if sc_pp.n_admitted > 0:
                            result.units = list(sc_pp.units)
                            result.plan_summaries = list(sc_pp.plan_summaries)
                            result.post_process_meta = sc_pp.to_meta()
                            result.tier_used = f"{_TIER_BASE}_SECURECAFE"
                            result.confidence = min(
                                0.92, 0.7 + 0.04 * sc_pp.n_admitted
                            )
                            _log_rc(
                                pid, "cascade_exit", "won_securecafe_from_xhr_plan_only",
                                units=len(result.units),
                            )
                            return result
                # Stage 2: surface unit-level AND plan-level lists. The
                # runner promotes ``plan_summaries`` into the V2 record's
                # ``floor_plans[]`` field; verdict treats a property with
                # only plan-level data as SUCCESS_PLAN_LEVEL.
                #
                # D16: strict unit-level / plan-level partition. ``units``
                # carries only unit-level rows; plan-aggregate rows live
                # in ``plan_summaries`` only.
                result.units = list(pp.units)
                result.plan_summaries = list(pp.plan_summaries)
                result.post_process_meta = pp.to_meta()
                result.winning_url = (
                    result.api_responses[0].get("url") if result.api_responses else None
                )
                result.confidence = min(0.95, 0.7 + 0.05 * pp.n_admitted)
                result.tier_used = _TIER_BASE
                _log_rc(pid, "cascade_exit", "won_xhr_capture", units=len(result.units))
                return result
            # All parsed units failed validity — record and fall through
            # to the failure-classification path so the run-report
            # distinguishes "parser produced rows but all invalid" from
            # "parser produced zero rows".
            result.errors.append(
                f"RENTCAFE_VALIDITY_REJECTED: {len(all_units)} parsed rows "
                f"failed unit_validity (no numeric dimension)"
            )

        # 2026-05-13 port (Commit 14 wiring): WP middleware fallback.
        # Many SHAPE_REJECTED RentCafe sites mount the floorplans endpoint
        # at ``<origin>/wp-json/middleware/v1/getFloorplans/?propertyId=<id>``
        # but the XHR doesn't fire client-side during page load. Probe it
        # directly when the captured path didn't yield admissable units.
        wp_units = await _try_rentcafe_wp_probe(ctx, result)
        if wp_units:
            from ma_poc.extraction.post_process import post_process

            pp = post_process(wp_units, property_id=getattr(ctx, "property_id", None))
            if pp.n_admitted > 0:
                result.units = list(pp.units)
                result.plan_summaries = list(pp.plan_summaries)
                result.post_process_meta = pp.to_meta()
                # 2026-05-21 (P0 follow-up): realign with feature branch.
                # WP-probe recovery gets a distinct tier label so the
                # downstream tier-distribution dashboard can quantify how
                # often the captured-API path missed but the probe
                # rescued. The previous "keep _TIER_BASE" collapse was a
                # May-13 partial-rollout decision that is no longer
                # warranted now that the analyzer's PLATFORM_TIERS map
                # includes the WP_PROBE label.
                result.tier_used = f"{_TIER_BASE}_WP_PROBE"
                result.confidence = min(0.90, 0.65 + 0.05 * pp.n_admitted)
                _log_rc(pid, "cascade_exit", "won_wp_probe", units=len(result.units))
                return result

        # 2026-05-21 (port from feature-branch fix/resolver-path-patterns-may13):
        # SecureCafe online-leasing portal drill-down. Highest-leverage
        # RentCafe path — promotes the ~1,060-property floorplan/LLM pool
        # to deterministic Tier-1. The portal carries the real UNIT-LEVEL
        # inventory one drill-down past the floorplan page; reachable via
        # curl_cffi (probe_get) which clears Cloudflare with chrome120
        # impersonation.
        sc_units = await _try_rentcafe_securecafe_probe(ctx, result)
        if sc_units:
            from ma_poc.extraction.post_process import post_process

            pp = post_process(sc_units, property_id=getattr(ctx, "property_id", None))
            if pp.n_admitted > 0:
                result.units = list(pp.units)
                result.plan_summaries = list(pp.plan_summaries)
                result.post_process_meta = pp.to_meta()
                result.tier_used = f"{_TIER_BASE}_SECURECAFE"
                result.confidence = min(0.92, 0.7 + 0.04 * pp.n_admitted)
                _log_rc(pid, "cascade_exit", "won_securecafe_drill", units=len(result.units))
                return result

        # 2026-05-21 (port): RentCafe-HOSTED SSR unit table fallback.
        # The rentcafe.com/.../default.aspx portal (and the same RC widget
        # on rendered vanity pages) SSRs every unit as
        # ``<tr class="fp-unit" data-unit-*>``. Server-200, no bot-wall,
        # no login. Generic extractor misses this markup — parser lives in
        # ``_rentcafe_hosted_table.py``. Render-dependent, so parse the
        # already-rendered page HTML.
        try:
            from ma_poc.pms.adapters.generic import _get_page_html
            _rc_html = await _get_page_html(page, ctx)
        except Exception:
            _rc_html = ""
        _has_fp_unit = bool(_rc_html and "fp-unit" in _rc_html)
        if _has_fp_unit:
            from ma_poc.extraction.post_process import post_process

            hosted = parse_rentcafe_hosted_table(
                _rc_html, str(getattr(ctx, "base_url", "") or "")
            )
            _log_rc(
                pid,
                "hosted_dom",
                "ok" if hosted else "fp_unit_present_parser_empty",
                units=len(hosted),
                reason=f"fp_unit_count={_rc_html.count('fp-unit')}",
            )
            # 2026-05-22 diagnostic: when fp-unit markup is present but
            # the hosted-table parser produced 0 rows, the row attribute
            # inventory is the most useful signal (the parser keys on
            # specific data-* attribute names; if Yardi has reshuffled
            # those, the parse silently drops).
            if not hosted:
                _log_rc_diag(
                    pid,
                    "hosted_dom",
                    _rc_html,
                    reason=f"fp_unit_count={_rc_html.count('fp-unit')}",
                )
            if hosted:
                pp = post_process(
                    hosted, property_id=getattr(ctx, "property_id", None)
                )
                if pp.n_admitted > 0:
                    result.units = list(pp.units)
                    result.plan_summaries = list(pp.plan_summaries)
                    result.post_process_meta = pp.to_meta()
                    result.tier_used = "TIER_1_DOM_RENTCAFE_HOSTED"
                    result.confidence = min(0.92, 0.7 + 0.04 * pp.n_admitted)
                    _log_rc(pid, "cascade_exit", "won_hosted_dom", units=len(result.units))
                    return result
        elif _rc_html:
            _log_rc(
                pid,
                "hosted_dom",
                "skipped_no_fp_unit",
                reason=f"rc_html_len={len(_rc_html)}",
            )

        # 2026-05-21 (port): RentCafe-Nestin per-plan DOM recovery. The
        # 35-prop JSON-LD probe (project_jsonld_recovery_2026-05-20.md)
        # found ~89% of the 298-prop JSON-LD ALL_fail bucket are
        # RentCafe-Nestin marketing sites where the unit + rent + date
        # data lives one nav-hop deeper at /floorplans/{plan-slug}.
        # Detection: resource.rentcafe.com CDN signal in rendered HTML.
        # Probes each /floorplans/{slug} detail page via the live
        # Playwright page (probe_get hits CF-403 even with static
        # cf_clearance cookies — only the browser session works).
        if _rc_html:
            try:
                from ma_poc.pms.adapters._rentcafe_nestin import (
                    is_nestin_template,
                    recover_rentcafe_nestin_per_plan,
                )

                _is_nestin = is_nestin_template(_rc_html)
                if _is_nestin:
                    from ma_poc.extraction.post_process import post_process

                    nestin_units, nestin_source = await recover_rentcafe_nestin_per_plan(
                        _rc_html,
                        str(getattr(ctx, "base_url", "") or ""),
                        page=page,
                        pid_for_log=pid,
                        ctx=ctx,
                    )
                    _log_rc(
                        pid,
                        "nestin_recover",
                        "ok" if nestin_units else "template_matched_no_units",
                        units=len(nestin_units),
                        reason=f"source={nestin_source[:120] if nestin_source else 'None'}",
                    )
                    # 2026-05-22 diagnostic: Nestin recovery is the
                    # last-resort RentCafe path; when it matches the
                    # template but extracts 0 units the failure mode
                    # is invariably a per-plan-page parser miss (the
                    # /floorplans/{slug} layout changed) — capture
                    # signals so we can pin the new layout.
                    if not nestin_units:
                        _log_rc_diag(
                            pid,
                            "nestin_recover",
                            _rc_html,
                            reason=(
                                f"source={nestin_source[:80] if nestin_source else 'None'} "
                                f"rc_html_len={len(_rc_html)}"
                            ),
                        )
                    if nestin_units:
                        pp = post_process(
                            nestin_units, property_id=getattr(ctx, "property_id", None)
                        )
                        if pp.n_admitted > 0:
                            result.units = list(pp.units)
                            result.plan_summaries = list(pp.plan_summaries)
                            result.post_process_meta = pp.to_meta()
                            result.tier_used = "TIER_1_DOM_RENTCAFE_NESTIN"
                            if nestin_source:
                                result.winning_url = nestin_source
                            result.confidence = min(0.90, 0.65 + 0.04 * pp.n_admitted)
                            _log_rc(
                                pid, "cascade_exit", "won_nestin",
                                units=len(result.units),
                            )
                            return result
                else:
                    _log_rc(
                        pid,
                        "nestin_recover",
                        "skipped_not_nestin_template",
                        reason=f"rc_html_len={len(_rc_html)}",
                    )
            except Exception as nestin_exc:
                _log_rc(
                    pid,
                    "nestin_recover",
                    f"exception:{type(nestin_exc).__name__}",
                    reason=str(nestin_exc)[:140],
                )
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
        _log_rc(
            pid,
            "cascade_exit",
            "all_paths_empty",
            reason=f"terminal_tier={tier_code} errors={'|'.join(result.errors[:3])[:160]}",
        )
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


# ────────────────────────────────────────────────────────────────────
# 2026-05-13 port (Commit 9 of MAY13_API_TIER_PORT_PLAN.md):
# WordPress middleware fallback. SHAPE_REJECTED RentCafe sites often
# embed the floorplans endpoint at:
#   <origin>/wp-json/middleware/v1/getFloorplans/?propertyId[]=<id>
# Property ID is server-rendered into the marketing HTML in any of
# three forms (query, data-attribute, JS config). Adapter wiring of
# this probe into RentCafeAdapter.extract() is deferred to Commit 11
# (alongside the SecureCafe drill-down which needs curl_cffi).
# ────────────────────────────────────────────────────────────────────


# Property-ID extraction: vanity sites embed propertyId in any of (a)
# the XHR query string (``propertyId=<id>``), (b) data-attrs on the
# property card (``data-property-id="..."``), and (c) inline script
# tags (``propertyId: <id>``).
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
    """Return the first RentCafe propertyId seen in *html*, or ``None``."""
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
    """``scheme://netloc`` for the property's effective URL (post-redirect).

    Prefers ``fetch_result.final_url`` since redirects may have already
    settled there; falls back to ``ctx.base_url``. Returns ``""`` when
    neither is parseable.
    """
    from urllib.parse import urlparse as _urlparse

    candidate = ""
    fr = getattr(ctx, "fetch_result", None)
    if fr is not None:
        candidate = str(getattr(fr, "final_url", "") or "")
    if not candidate:
        candidate = getattr(ctx, "base_url", "") or ""
    try:
        p = _urlparse(candidate)
    except Exception:
        return ""
    if not p.scheme or not p.netloc:
        return ""
    return f"{p.scheme}://{p.netloc}"


async def _try_rentcafe_wp_probe(
    ctx: AdapterContext, result: AdapterResult
) -> list[dict[str, str]]:
    """Probe ``<origin>/wp-json/middleware/v1/getFloorplans/`` directly.

    Used when XHR capture missed the floorplans endpoint (typical for
    RentCafe sites mounted on WordPress where the XHR fires from a
    client-side bundle whose load was missed by the network listener).
    Returns parsed unit dicts on success, empty list on any failure.

    Never raises -- exceptions append to ``result.errors`` and the
    function returns ``[]``.

    NOTE: This helper is not yet wired into ``RentCafeAdapter.extract()``;
    that wiring lands in Commit 11. The function is exposed here so
    tests can exercise it in isolation and so the adapter can simply
    add a call site once the curl_cffi-based SecureCafe probe is ready
    to land alongside it.
    """
    pid = getattr(ctx, "property_id", "") or "unknown"

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
        _log_rc(pid, "wp_property_id", "skipped_no_html", reason="no fetch_result.body")
        return []

    prop_id = _find_rentcafe_property_id(html)
    if not prop_id:
        _log_rc(pid, "wp_property_id", "not_found", reason=f"body_len={len(html)}")
        return []
    _log_rc(pid, "wp_property_id", "found", reason=f"prop_id={prop_id}")

    origin = _origin_from_ctx(ctx)
    if not origin:
        _log_rc(pid, "wp_probe", "skipped_no_origin", reason="no origin discoverable")
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
            _log_rc(
                pid,
                "wp_probe",
                f"status_{r.status_code}",
                reason=f"url={api_url[:120]}",
                http_status=r.status_code,
            )
            return []
        try:
            payload = r.json()
        except (ValueError, Exception):
            _log_rc(
                pid,
                "wp_probe",
                "non_json_body",
                reason=f"len={len(r.text or '')}",
                http_status=r.status_code,
            )
            return []
    except Exception as exc:
        _log_rc(
            pid,
            "wp_probe",
            f"exception:{type(exc).__name__}",
            reason=f"{str(exc)[:120]} prop_id={prop_id}",
        )
        result.errors.append(
            f"rentcafe-wp-probe-error: prop_id={prop_id!r} "
            f"{type(exc).__name__}: {str(exc)[:120]}"
        )
        return []

    items = _unwrap_rentcafe_list(payload)
    if not items:
        # Capture top-level payload structure so we can diagnose unwrap
        # failures (new envelope shapes, wrapper keys, etc.) from events
        # without re-replaying the probe.
        payload_shape: dict[str, Any] = {"payload_type": type(payload).__name__}
        if isinstance(payload, dict):
            payload_shape["top_keys"] = sorted(list(payload.keys()))[:20]
        elif isinstance(payload, list):
            payload_shape["list_len"] = len(payload)
            if payload and isinstance(payload[0], dict):
                payload_shape["first_item_keys"] = sorted(list(payload[0].keys()))[:20]
        _log_rc(
            pid,
            "wp_parse",
            "no_items_unwrapped",
            reason=f"payload_type={type(payload).__name__}",
            **{f"signal_{k}": v for k, v in payload_shape.items()},
        )
        return []
    units = parse_rentcafe_floorplans(items, api_url)
    # When the WP probe returned a non-empty items list but parse_*
    # produced 0 units, the per-item shape is the missing signal — log
    # the keys of the first item so we can see what new template
    # variant fired without re-fetching.
    if not units and isinstance(items, list) and items:
        first = items[0] if isinstance(items[0], dict) else {}
        first_keys = sorted(list(first.keys()))[:25] if first else []
        _log_rc(
            pid,
            "wp_parse",
            "parse_returned_empty",
            units=0,
            reason=f"items={len(items)} first_keys={first_keys[:8]}",
            signal_items_len=len(items),
            signal_first_item_keys=first_keys,
        )
    else:
        _log_rc(
            pid,
            "wp_parse",
            "ok" if units else "parse_returned_empty",
            units=len(units),
            reason=f"items={len(items) if isinstance(items, list) else '?'}",
        )
    if units:
        result.api_responses.append(
            {"url": api_url, "status": 200, "body": payload, "via": "wp_probe"}
        )
        result.winning_url = api_url
    return units


# ────────────────────────────────────────────────────────────────────
# 2026-05-21 port (P0 — feature-branch fix/resolver-path-patterns-may13):
# SecureCafe online-leasing portal drill-down. Most RentCafe vanity sites
# have only floorplan-level rent on the marketing page; the per-apartment
# inventory lives at ``{sub}.securecafe.com/onlineleasing/{slug}/
# availableunits.aspx``. The Cloudflare/Akamai wall on those pages is
# defeated by curl_cffi chrome120 impersonation (probe_get).
#
# Coverage: ~84% of brochure + ~67% of has-inventory RC residual
# (sc_gap probe, 112 sites, CF-walled=0). Highest single-fix LLM-cost
# reduction in the May-21 post-merge audit (~1,060 of 1,738 detected
# RentCafe properties currently fall through to LLM cascade at 99% rate).
# ────────────────────────────────────────────────────────────────────


_SECURECAFE_URL_RE = re.compile(
    r"""https?://
        (?P<sub>[a-z0-9][a-z0-9-]*)\.securecafe\.com
        /(?P<path>onlineleasing|residentservices)/
        (?P<slug>[a-z0-9][a-z0-9-]*)
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _identity_slug_candidates(ctx: AdapterContext) -> list[str]:
    """Property-identity tokens used to score multi-community SC matches.

    Multi-community RentCafe sites (e.g. landcoapartments.com lists 8
    communities) embed SecureCafe links for every property in the
    portfolio. Picking the first match is a 1-in-N coin flip. Tokens
    sourced from property_name + base_url path so the right SC link
    can be selected. Lowercased + a-z0-9-only.
    """
    out: list[str] = []
    name = (getattr(ctx, "property_name", "") or "").lower()
    if name:
        cleaned = re.sub(r"[^a-z0-9]+", "", name)
        if cleaned:
            out.append(cleaned)
    base_url = getattr(ctx, "base_url", "") or ""
    from urllib.parse import urlparse as _urlparse_for_scoring
    try:
        path = _urlparse_for_scoring(base_url).path.lower()
    except Exception:
        path = ""
    for seg in re.split(r"[/_-]+", path):
        seg_clean = re.sub(r"[^a-z0-9]+", "", seg)
        if len(seg_clean) >= 5 and seg_clean not in ("apartments", "homes", "community", "communities", "rental", "rentals", "leasing"):
            out.append(seg_clean)
    return list(dict.fromkeys(out))


def _score_securecafe_match(m: re.Match[str], identity_tokens: list[str]) -> int:
    """Score a SecureCafe URL match: higher = better fit for this property.

    Scoring:
    +10 if the URL path is ``onlineleasing`` (per-property leasing portal).
         The ``residentservices`` path is a portfolio-wide resident login
         where the sub-domain is the marketing-host slug — never the
         per-property slug we need.
    +5  per identity-token substring hit against the SC subdomain.
    +5  per identity-token substring hit against the SC URL slug.
    """
    score = 0
    path = (m.group("path") or "").lower()
    if path == "onlineleasing":
        score += 10
    sub = (m.group("sub") or "").lower()
    slug = (m.group("slug") or "").lower()
    for tok in identity_tokens:
        if not tok:
            continue
        if tok in sub:
            score += 5
        if tok in slug:
            score += 5
    return score

# 2026-05-22: relaxed to allow `-` inside the visual-name segment. The newer
# SecureCafe template wraps each floorplan-grouping table in a
# ``<caption class="sr-only">Apartment Details and Selection for Floor Plan:
# 2 Bed - 1 Bath - 2 Bedrooms, 1 Bathroom</caption>``. The visual name
# (``"2 Bed - 1 Bath"``) contains literal dash characters, which the
# previous ``[^<\-]`` character class rejected, dropping the header to 0
# matches and returning ``[]`` from parse_securecafe_availableunits. The
# tail still anchors on ``- <N> Bedroom[s], <N.N> Bathroom`` so the
# non-greedy name capture cannot run past the bed/bath suffix.
#
# Diagnosed 2026-05-22 against 5 live PIDs (72944, 24561, 6550, 40584,
# 67750) — all had ≥4 AvailUnitRow rows with the new caption format and
# zero header matches under the old regex. Old fixtures still pass.
_SECURECAFE_FP_HDR_RE = re.compile(
    r"Floor\s+Plan:\s*(?P<name>[^<]{1,150}?)\s*-\s*"
    r"(?P<bedtxt>Studio|\d+\s*Bedroom[s]?)\s*,\s*"
    r"(?P<bathtxt>\d+(?:\.\d+)?)\s*Bathroom",
    re.IGNORECASE,
)

_SECURECAFE_UNIT_ROW_RE = re.compile(
    r"<tr[^>]*class='AvailUnitRow'.*?</tr>", re.IGNORECASE | re.DOTALL
)
_SC_APT_RE = re.compile(
    r"data-label=['\"]?Apartment['\"]?[^>]*>\s*#?\s*([A-Za-z0-9-]+)", re.I
)
_SC_SQFT_RE = re.compile(r"data-label=['\"]?Sq\.?Ft\.?['\"]?[^>]*>\s*([\d,]+)", re.I)
_SC_RENT_RE = re.compile(
    r"data-label=['\"]?Rent['\"]?[^>]*>\s*\$?\s*([\d,]+)\s*(?:-\s*\$?\s*([\d,]+))?", re.I
)
# 2026-05-18: securecafe AvailUnitRow has a ``data-label='Date Available'``
# cell with "Available" or "M/D/YY" inner text. Earlier parser ignored it,
# resulting in 0% available_date on the SECURECAFE tier (21.6k units).
_SC_DATE_RE = re.compile(
    r"data-label=['\"]?Date Available['\"]?[^>]*>(.*?)</td>", re.I | re.S
)


# 2026-05-24 — legacy RentCafe direct-hosting path. A subset of older
# RentCafe deployments (Hampshire Village / Southern Management portfolio
# was the canonical case, PID 220581) does NOT publish a *.securecafe.com
# subdomain at all. The leasing portal lives on the shared
# ``www.rentcafe.com/onlineleasing/<slug>/`` host instead. The page
# markup is identical (same Yardi RentCafe template, same
# ``AvailUnitRow`` rows on ``availableunits.aspx``), so the existing
# ``parse_securecafe_availableunits`` parser handles it verbatim — only
# the URL discovery is different.
_RENTCAFE_LEGACY_URL_RE = re.compile(
    r"""https?://
        (?:www\.)?rentcafe\.com
        /onlineleasing/
        (?P<slug>[a-z0-9][a-z0-9-]*)
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _find_rentcafe_legacy_base(html: str, ctx: AdapterContext) -> str | None:
    """Return ``https://www.rentcafe.com/onlineleasing/<slug>`` or None.

    Fires only when the marketing page exposes a legacy direct-hosted
    RentCafe URL. The slug is taken verbatim — there's no subdomain
    synthesis to get wrong (one shared host). Identity scoring is only
    used to disambiguate when a portfolio page lists multiple legacy
    URLs (rare but possible for management-company landing pages).
    """
    identity = _identity_slug_candidates(ctx)
    if not html:
        return None
    best_slug: str | None = None
    best_score: int = -1
    seen_slugs: set[str] = set()
    for m in _RENTCAFE_LEGACY_URL_RE.finditer(html):
        slug = (m.group("slug") or "").lower()
        if not slug or slug in seen_slugs:
            continue
        seen_slugs.add(slug)
        score = 5  # legacy URL is always +5 for being well-formed
        for tok in identity:
            if tok and tok in slug:
                score += 5
        if score > best_score:
            best_score = score
            best_slug = slug
    if best_slug:
        return f"https://www.rentcafe.com/onlineleasing/{best_slug}"
    return None


def _find_securecafe_base(html: str, ctx: AdapterContext) -> str | None:
    """Return ``https://<sub>.securecafe.com/onlineleasing/<slug>`` or None.

    Looks in the rendered HTML first (marketing sites link/iframe to their
    securecafe portal), then falls back to captured response URLs in
    ``ctx._api_responses`` (link-hop discovered URLs surface here), and
    finally the property's own host when it *is* a securecafe portal.

    Multi-community fix (2026-05-24): when the marketing page exposes
    SecureCafe links for several communities (landcoapartments.com hosts
    8 communities; eight different ``*.securecafe.com`` subdomains visible
    on the same DOM), pre-fix code picked the FIRST regex match — usually
    the portfolio-wide ``residentservices/apartmentsforrent/userlogin.aspx``
    link which uses the *marketing-host* slug as subdomain (e.g.
    ``landcoapartments.securecafe.com``) — never the per-property slug
    we actually need (``cooperslandingapts.securecafe.com``). The result
    was a 404 on every multi-community property. Scoring now prefers
    ``onlineleasing`` paths over ``residentservices`` and tie-breaks by
    similarity to property identity (property_name + base_url path).
    """
    identity = _identity_slug_candidates(ctx)

    def _best_match(text: str) -> re.Match[str] | None:
        best: re.Match[str] | None = None
        best_score: int = -1
        for m in _SECURECAFE_URL_RE.finditer(text):
            s = _score_securecafe_match(m, identity)
            if s > best_score:
                best = m
                best_score = s
        # Require at least one positive signal (``onlineleasing`` path
        # scored +10, OR an identity-token hit). All-zero matches mean
        # we only saw resident-services links — defer to the next
        # source (api_responses, refetch) rather than constructing a
        # known-wrong URL.
        if best is None or best_score < 5:
            return None
        return best

    if html:
        m = _best_match(html)
        if m:
            return f"https://{m.group('sub')}.securecafe.com/onlineleasing/{m.group('slug')}"
    # Scan captured response URLs (link-hop often surfaces a securecafe
    # portal request like guestlogin.aspx / floorplans there even when
    # the rendered body lost the marketing-site anchor).
    sc_response_urls = "\n".join(
        str(resp.get("url", "") or "")
        for resp in (getattr(ctx, "_api_responses", []) or [])
        if "securecafe.com/onlineleasing/" in str(resp.get("url", "") or "").lower()
    )
    if sc_response_urls:
        m = _best_match(sc_response_urls)
        if m:
            return f"https://{m.group('sub')}.securecafe.com/onlineleasing/{m.group('slug')}"
    origin = _origin_from_ctx(ctx)
    if "securecafe.com" in origin:
        fr = getattr(ctx, "fetch_result", None)
        final = str(getattr(fr, "final_url", "") or "") if fr else ""
        m = _best_match(final or origin)
        if m:
            return f"https://{m.group('sub')}.securecafe.com/onlineleasing/{m.group('slug')}"
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
    is followed by ``<tr class='AvailUnitRow'>`` rows, one per apartment.
    Returns ``[]`` on a CF-challenge shell or unparseable HTML.
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
    """Follow the securecafe online-leasing portal and parse
    ``availableunits.aspx`` for unit-level inventory.

    Returns parsed unit dicts on success, ``[]`` on any failure.
    Never raises -- exceptions append to ``result.errors``.

    Emits ``rentcafe:sc_search`` / ``sc_homepage_refetch`` / ``sc_probe`` /
    ``sc_parse`` events so production failures are diagnosable (see
    ``_log_rc`` module-level docstring).
    """
    pid = getattr(ctx, "property_id", "") or "unknown"

    html = ""
    fr = getattr(ctx, "fetch_result", None)
    body = getattr(fr, "body", None) if fr is not None else None
    if isinstance(body, bytes):
        html = body.decode("utf-8", errors="replace")
    elif isinstance(body, str):
        html = body

    # Lazy probe_get import — silently bail when curl_cffi isn't available
    # (test env / partial install). The probe is a best-effort recovery; an
    # absent dependency is not a property-level error.
    try:
        from ma_poc.pms.adapters._probe import probe_get
    except ImportError:
        _log_rc(pid, "sc_search", "skipped_no_curl_cffi", reason="curl_cffi import failed")
        return []

    base = _find_securecafe_base(html, ctx)
    # 2026-05-24 — legacy direct-host fallback. Some RentCafe properties
    # (Southern Management / Hampshire Village portfolio) skip the
    # ``*.securecafe.com`` subdomain entirely and host their leasing
    # portal directly on ``www.rentcafe.com/onlineleasing/<slug>/``. The
    # page format is identical, so the existing parser handles it; only
    # discovery is new. Searched alongside the SC base — if both exist,
    # SC wins (it's the canonical post-2024 host).
    legacy_base = _find_rentcafe_legacy_base(html, ctx) if not base else None
    if base:
        _log_rc(pid, "sc_search", "found_in_body", reason=base[:140])
    elif legacy_base:
        _log_rc(pid, "sc_search", "found_legacy_in_body", reason=legacy_base[:140])
    if not base and not legacy_base:
        # Patchright-rendered body sometimes lacks the securecafe link in
        # a regex-matchable form (link injected post-render or behind a
        # menu), and securecafe direct fetches CF-403 even with cookies.
        # Re-fetch the property's homepage via curl_cffi (bypasses CF +
        # raw server HTML) and scan that for the securecafe base.
        origin = _origin_from_ctx(ctx)
        if not origin:
            _log_rc(pid, "sc_search", "not_in_body_no_origin", reason="no origin to refetch")
            return []
        try:
            # Marketing-site refetch — same-origin with ctx.base_url, so
            # the proxy gate's Layer 1 declines proxy here. We only need
            # the raw HTML body to scan for the SecureCafe URL.
            _hr = probe_get(origin, ctx=ctx, stage="sc_homepage_refetch", timeout=20)
            _hr_len = len(getattr(_hr, "text", "") or "")
            if _hr.status_code == 200 and _hr.text:
                base = _find_securecafe_base(_hr.text, ctx)
                if not base:
                    legacy_base = _find_rentcafe_legacy_base(_hr.text, ctx)
                _log_rc(
                    pid,
                    "sc_homepage_refetch",
                    "found_via_refetch" if base else ("found_legacy_via_refetch" if legacy_base else "no_sc_url_in_refetch"),
                    reason=f"len={_hr_len} base={(base or legacy_base or 'None')[:120]}",
                    homepage_status=_hr.status_code,
                )
            else:
                _log_rc(
                    pid,
                    "sc_homepage_refetch",
                    f"status_{_hr.status_code}",
                    reason=f"len={_hr_len}",
                    homepage_status=_hr.status_code,
                )
        except Exception as exc:
            _log_rc(
                pid,
                "sc_homepage_refetch",
                f"exception:{type(exc).__name__}",
                reason=str(exc)[:140],
            )
            # Preserve main's silent-failure contract for now — emitting
            # is enough; do not append to result.errors.
    # Promote legacy_base if SC didn't resolve. After this line, ``base``
    # is the URL to probe (SC if available, else legacy).
    if not base and legacy_base:
        base = legacy_base
    if not base:
        _log_rc(pid, "sc_probe", "skipped_no_base", reason="no securecafe URL discoverable")
        return []
    au_url = f"{base}/availableunits.aspx"

    try:
        # SecureCafe drill — *.securecafe.com is the canonical
        # cross-origin CF-walled host. proxy_gate Layer 2 (host
        # allowlist) admits the proxy here, subject to the per-
        # property hop budget (default 3 paid-egress hops per pid).
        r = probe_get(au_url, ctx=ctx, stage="sc_probe", timeout=25)
    except Exception as exc:
        _log_rc(
            pid,
            "sc_probe",
            f"exception:{type(exc).__name__}",
            reason=f"{str(exc)[:120]} url={au_url[:80]}",
        )
        return []

    page_html = r.text or ""
    outcome, reason = _classify_probe_body(r.status_code, page_html)
    _log_rc(
        pid,
        "sc_probe",
        outcome,
        reason=reason,
        au_url=au_url[:140],
        http_status=r.status_code,
    )
    if r.status_code != 200:
        return []
    # CF challenge shell is tiny and carries no unit rows.
    if "AvailUnitRow" not in page_html:
        return []
    units = parse_securecafe_availableunits(page_html, au_url)
    # Parser-stage telemetry: headers vs. row count is the most useful
    # diagnostic when AvailUnitRow exists but units == []. The 2026-05-22
    # local sample shows 25% of failures here (newer SecureCafe template
    # lacks the "Floor Plan: A1 - 1 Bedroom" header the regex expects).
    headers_seen = len(list(_SECURECAFE_FP_HDR_RE.finditer(page_html)))
    avail_rows = page_html.count("AvailUnitRow")
    _log_rc(
        pid,
        "sc_parse",
        "ok" if units else "parse_returned_empty",
        units=len(units),
        reason=f"headers={headers_seen} avail_rows={avail_rows}",
        headers_matched=headers_seen,
        avail_rows=avail_rows,
    )
    # 2026-05-22 diagnostic capture: on silent-empty (avail rows visible
    # but parser produced 0 units), dump structural signals so future
    # regex bugs are debuggable from events.jsonl alone. See
    # _capture_body_diagnostics for the field schema.
    if not units and avail_rows > 0:
        _log_rc_diag(
            pid,
            "sc_parse",
            page_html,
            reason=f"au_url={au_url[:100]} headers={headers_seen} avail_rows={avail_rows}",
        )
    if units:
        result.api_responses.append(
            {"url": au_url, "status": 200, "body": "<securecafe-html>", "via": "securecafe_probe"}
        )
        result.winning_url = au_url
    return units
