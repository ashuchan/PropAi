"""
V2 schema adapters — input parsing and output formatting.
==========================================================

The scraper core is schema-agnostic. This module provides pure transformer
layers that convert between V2 external formats and the internal canonical
representation that the pipeline already uses.

  1. parse_v2_csv_row()   — maps V2 CSV columns → internal canonical dict
  2. build_v2_property()  — maps internal scrape result → V2 JSON output
  3. validate_v2_property() — V2-specific post-transform validation

No scraping logic, no profile logic, no state tracking lives here.
"""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime
from typing import Any

from ma_poc.core import issue_log as V
from ma_poc.core.concession_clean import (
    classify_concession_quality as _concession_quality,
)
from ma_poc.core.concession_clean import (
    clean_concession_text as _concession_clean,
)
from ma_poc.core.concession_normalize import normalize_concession

# ── V2 CSV column mapping ────────────────────────────────────────────────────
#
# The V2 CSV ("Apartments v2") has exactly 7 columns:
#   apartmentid, name, address, city, state, zip, website
#
# We map these to the internal keys that daily_runner / identity.py expect.

V2_CSV_COLUMN_MAP: dict[str, list[str]] = {
    "apartment_id": ["apartmentid", "apartment_id", "ApartmentID"],
    "property_name": ["name", "Name"],
    "property_address": ["address", "Address"],
    "city": ["city", "City"],
    "state": ["state", "State"],
    "zip_code": ["zip", "Zip", "zip_code"],
    "website": ["website", "Website"],
}

# Key aliases for csv_get() — used by daily_runner when schema_version == "v2"
V2_ID_KEYS = ("apartmentid", "apartment_id", "ApartmentID")
V2_NAME_KEYS = ("name", "Name")
V2_ADDRESS_KEYS = ("address", "Address")
V2_CITY_KEYS = ("city", "City")
V2_STATE_KEYS = ("state", "State")
V2_ZIP_KEYS = ("zip", "Zip", "zip_code")
V2_WEBSITE_KEYS = ("website", "Website")


def get_schema_version(args: Any = None) -> str:
    """Resolve schema version from CLI args > env > default.

    Args:
        args: argparse namespace with optional ``schema_version`` attribute.

    Returns:
        ``"v1"`` or ``"v2"``.
    """
    if args and getattr(args, "schema_version", None):
        return args.schema_version
    return os.getenv("SCHEMA_VERSION", "v1").strip().lower()


# ── Output adapter ───────────────────────────────────────────────────────────

# Platform guess → human-readable website design label
_PLATFORM_LABELS: dict[str, str] = {
    "entrata": "Powered by Entrata",
    "rentcafe": "Powered by RentCafe",
    "appfolio": "Powered by AppFolio",
    "yardi": "Powered by RentCafe (Yardi)",
    "realpage": "Powered by RealPage",
    "sightmap": "Powered by SightMap",
    "knock": "Powered by Knock",
    "respage": "Powered by Respage",
}


def build_v2_property(
    row: dict,
    ident: Any,
    scrape_result: dict,
    target_units: list[dict],
    scrape_ts: datetime | None = None,
) -> dict:
    """Transform internal property + units into V2 output schema.

    Takes the SAME row dict, identity, scrape_result, and unit list that
    ``build_property_record()`` receives. Returns a V2-shaped dict.

    The scraper core is untouched — this is a pure post-processing step.
    """
    from ma_poc.core.identity import csv_get

    if scrape_ts is None:
        scrape_ts = datetime.now(UTC)

    md = scrape_result.get("property_metadata") or {}

    # Resolve fields with CSV priority, scraped fallback
    def _pick(csv_val: Any, scraped_val: Any) -> Any:
        if csv_val not in (None, "", "null", "None"):
            return csv_val
        return scraped_val if scraped_val not in (None, "", "null", "None") else None

    # CSV values
    csv_id = csv_get(row, *V2_ID_KEYS)
    csv_name = csv_get(row, *V2_NAME_KEYS)
    csv_addr = csv_get(row, *V2_ADDRESS_KEYS)
    csv_city = csv_get(row, *V2_CITY_KEYS)
    csv_state = csv_get(row, *V2_STATE_KEYS)
    csv_zip = csv_get(row, *V2_ZIP_KEYS)
    csv_website = csv_get(row, *V2_WEBSITE_KEYS)

    # Platform / website design
    platform = scrape_result.get("platform_detected") or (md.get("api_provider") if md else None) or ""
    website_design = _PLATFORM_LABELS.get(platform.lower(), platform or None)

    # Concessions — prefer scraped banner text. Raw text is ALWAYS retained
    # (capture-first); concessions_json is the deterministic RealPage-shaped
    # normalization (None when unparseable — not data loss, raw stays).
    concessions_text = scrape_result.get("concessions_text") or md.get("concessions") or None
    concessions_json = normalize_concession(concessions_text)

    prop: dict[str, Any] = {
        # ── Property-level fields ────────────────────────────────────────
        "apartment_id": _safe_int(csv_id),
        "proj_name": _pick(csv_name, md.get("name") or md.get("title")),
        "address": _pick(csv_addr, md.get("address")),
        "city": _pick(csv_city, md.get("city")),
        "state": _pick(csv_state, md.get("state")),
        "zip_code": _format_zip_5(_pick(csv_zip, md.get("zip"))),
        "country": md.get("country") or None,
        "phone": _pick(
            csv_get(row, "Phone", "phone"),
            md.get("telephone"),
        )
        or None,
        "email_address": md.get("email") or md.get("email_address") or None,
        # CANONICAL property URL — ALWAYS the input CSV/property URL
        # (scheme-normalized base_url fallback). NEVER overwritten by a
        # winning/resolved/final URL. Provenance URLs are SEPARATE
        # columns below (added 2026-05-19 per "keep property url + add
        # any url column separately"). apartment_id likewise = CSV id.
        "website": csv_website or scrape_result.get("base_url") or None,
        # Separate, additive URL provenance (capture-first; do not feed
        # identity/dedup off these — they vary run-to-run).
        "winning_url": _raw_str(
            scrape_result.get("_winning_page_url")
            or scrape_result.get("_winning_url")
        ),
        "resolved_url": _raw_str(
            (scrape_result.get("_resolved_target") or {}).get("resolved_url")
            if isinstance(scrape_result.get("_resolved_target"), dict)
            else None
        ),
        "pmc": _pick(
            csv_get(row, "Management Company", "pmc"),
            md.get("management_company"),
        )
        or None,
        "website_design": website_design if website_design else None,
        "concessions": concessions_text,
        "concessions_json": concessions_json,
        # ── Units ────────────────────────────────────────────────────────
        # ``property_plan_level`` carries the PROPERTY-level plan marker into
        # the per-row flag: adapters that record plan-ness on
        # ``AdapterResult.tier_used`` (rather than on every row) would
        # otherwise ship unflagged plan rows.
        "units": [
            _format_v2_unit(
                u,
                scrape_ts,
                str(_safe_int(csv_id) or ""),
                property_plan_level=property_is_plan_level(scrape_result),
            )
            for u in target_units
        ],
        # Public plan cards are preserved separately from physical apartments.
        # A plan may advertise rent and area but must never ship as a unit.
        "floor_plans": [
            _format_v2_floor_plan(plan, scrape_ts, str(_safe_int(csv_id) or ""))
            for plan in (scrape_result.get("plan_summaries") or [])
            if isinstance(plan, dict)
        ],
    }

    return prop


# Extraction-tier fragments that MARK a row as a floor-plan summary. Matched as
# substrings of the tier code, WITH the leading underscore, so only a real tier
# segment can trip them: ``TIER_1_API_RENTCAFE_SECURECAFE_FROM_PLAN``,
# ``TIER_1_API_APTS247_FLOORPLANS``, ``TIER_1_DOM_WIX_FLOOR_PLANS`` and
# ``TIER_1_DOM_MARK_TAYLOR_RENDERED_PLAN_CARD`` all contain "PLAN" and none of
# them is a plan-level marker — see the table test in
# tests/scripts/test_schema_v2_unit.py.
_PLAN_TIER_MARKERS: tuple[str, ...] = ("_PLAN_LEVEL", "_PLAN_TEXT")


def _has_plan_marker(tier: str, dqf: str) -> bool:
    """True when a row's own tier / data-quality flag declares it plan-level.

    ``dqf`` is pipe-delimited; each token is tested with ``startswith("PLAN_")``
    rather than a substring search so ``SQFT_NOT_PUBLISHED``,
    ``UNIT_LEVEL_PRICING_MISSING`` and ``NO_AVAILABILITY_NOW`` cannot match.

    Args:
        tier: Upper-cased extraction tier code.
        dqf: Upper-cased pipe-delimited data_quality_flag string.

    Returns:
        Whether a plan-level marker is present.
    """
    if any(marker in tier for marker in _PLAN_TIER_MARKERS):
        return True
    return any(part.strip().startswith("PLAN_") for part in dqf.split("|"))


def property_is_plan_level(scrape_result: Any) -> bool:
    """True when the PROPERTY's own result metadata declares a plan-level roster.

    Several emission sites (``pms/scraper.py`` Path-C at :2151 and the
    empty-exit plan path at :2308, plus every adapter that stamps its
    ``AdapterResult.tier_used`` with a ``_PLAN_LEVEL`` suffix) record plan-ness
    on the *property* result and leave the individual rows carrying the plain
    adapter tier. The output formatter only ever saw the row, so the flag was
    lost for every one of those adapters.

    Args:
        scrape_result: Internal result mapping for one property. Any object
            exposing ``.get`` is accepted; anything else yields ``False``.

    Returns:
        Whether the property-level metadata marks this roster plan-level.
    """
    getter = getattr(scrape_result, "get", None)
    if not callable(getter):
        return False
    tier = str(getter("extraction_tier_used") or "").upper()
    if any(marker in tier for marker in _PLAN_TIER_MARKERS):
        return True
    return str(getter("_verdict_quality") or "").upper() == "SUCCESS_PLAN_LEVEL"


def _is_floor_plan_level(
    unit: dict[str, Any], *, property_plan_level: bool = False
) -> bool:
    """True if the unit is a plan-LEVEL placeholder, not a real individual unit.

    Stamped explicitly on the output so downstream (and our own audits) can
    tell a floor-plan placeholder apart from a real-unit-missing-an-id without
    fragile inference over the synthetic-id + UNAVAILABLE + no-available_units
    combo.

    This is the SINGLE choke point for the client-facing flag: both v2 unit
    formatters (``core.schema_v2._format_v2_unit`` and the production fork in
    ``scripts/runners/jugnu.py``) delegate here, so an adapter cannot ship an
    unflagged plan row by forgetting to set the flag itself — it only has to
    use one of the plan conventions the codebase already has.

    Signals, in order:

      1. An explicit ``is_floor_plan_level=True`` already decided upstream
         (``pms.scraper.promote_verified_unit_rows`` writes the boolean).
      2. The adapter's own ``data_quality_flag`` — SightMap sets
         ``SIGHTMAP_PLAN_PRESENCE``; ``extraction.post_process`` appends
         ``PLAN_LEVEL_NO_UNIT_ANCHOR``.
      3. A ``*_PLAN_LEVEL`` extraction tier.
      4. (2026-07-28) A plan marker anywhere in the row's tier / flag tokens
         (``TIER_3_PLAN_TEXT``, ``PLAN_RANGE_ONLY``, …) **or** on the property
         result (``property_plan_level``) — gated on the row having no real
         apartment anchor.

    The step-4 anchor gate is what stops the inverse error: a plan-text parser
    can legitimately emit a UNIT-level row (``generic_plan_text`` line 903
    emits ``TIER_1_DOM_GENERIC_PLAN_TEXT_UNIT_STREET`` *with* a unit number),
    and a plan-level property can still contain anchored apartments. Those keep
    ``is_floor_plan_level=False``. Steps 1-3 are unchanged, so the predicate is
    monotone: nothing that used to be flagged becomes unflagged.

    Args:
        unit: Internal (pre-format) unit dict from an adapter.
        property_plan_level: Result of :func:`property_is_plan_level` for the
            property this row belongs to. Defaults to ``False`` so existing
            single-row callers keep working.

    Returns:
        Whether the row is a floor-plan summary rather than one apartment.
    """
    if unit.get("is_floor_plan_level") is True:
        return True
    dqf = str(unit.get("data_quality_flag") or "").upper()
    if "PLAN_PRESENCE" in dqf or "PLAN_LEVEL" in dqf:
        return True
    tier = str(unit.get("extraction_tier") or unit.get("_extraction_tier") or "").upper()
    if tier.endswith("_PLAN_LEVEL"):
        return True
    if not (property_plan_level or _has_plan_marker(tier, dqf)):
        return False
    # A row that proves ONE REAL APARTMENT is never a plan summary, whatever
    # the surrounding tier is called. ``unit_has_real_anchor`` is the codebase's
    # canonical unit-vs-plan classifier (verdict + Path-B retry gate use it too)
    # and accepts pre-format rows (unit_number / source_ids evidence).
    from ma_poc.core.identity import unit_has_real_anchor

    return not unit_has_real_anchor(unit)


def _format_v2_unit(
    unit: dict,
    scrape_ts: datetime,
    property_id: str = "",
    *,
    property_plan_level: bool = False,
) -> dict:
    """Transform a single internal unit dict to V2 unit format.

    Internal unit dicts carry private fields (prefixed with ``_``) from
    ``scrape_properties.py`` that are not part of the V1 public schema but
    contain the raw data we need for V2.

    ``property_id`` seeds the deterministic ``floor_plan_id`` so two
    properties with identically-named plans don't collide.
    """
    # 2026-05-19 capture-first alias hardening. Static audit of every
    # adapter showed the DOM (_html_extract), generic LLM/DOM, funnel,
    # repli360 and _api_parser paths emit camelCase / alt names the prior
    # ``or``-chains missed → silent loss of beds/baths/sqft/unit_id/
    # floor-plan for those FORMATS even though the value was surfaced.
    # _first() = alias-tolerant, additive, zero-risk when absent.
    beds_raw = _first(unit, "_bedrooms", "bedrooms", "beds",
                      "numberOfBeds", "bedroom", "bed", "num_beds")
    baths_raw = _first(unit, "_bathrooms", "bathrooms", "baths",
                       "numberOfBaths", "bathroom", "bath", "num_baths")
    fp_name = _first(unit, "_floor_plan", "floor_plan_name",
                     "floorplan_name", "floorPlanName", "floorplanName",
                     "fp_name", "floorplan", "plan_name")
    sqft = _first(unit, "_sqft", "sqft", "area", "squareFeet",
                  "square_feet", "size", "sq_ft")

    # unit_id alias (adapters emit unit_number / camelCase / uid)
    uid = _first(unit, "unit_id", "unit_number", "_unit_number",
                 "unitNumber", "unitId", "uid", "apartment_number")

    # Bed/bath fallback inference from the floor-plan name. Mirrors the
    # Jugnu transform so both pipelines fill the same gaps.
    if (beds_raw in (None, "")) or (baths_raw in (None, "")):
        try:
            from ma_poc.pms.adapters._parsing import infer_bed_bath_from_name

            inferred_beds, inferred_baths = infer_bed_bath_from_name(fp_name)
            if beds_raw in (None, "") and inferred_beds is not None:
                beds_raw = inferred_beds
            if baths_raw in (None, "") and inferred_baths is not None:
                baths_raw = inferred_baths
        except Exception:
            pass

    # rent: numeric fields first (alias-tolerant — generic/_merge emit
    # rent/minRent/totalRent/price camelCase), then parse rent_range.
    rent_lo = _first(unit, "market_rent_low", "rent_low", "asking_rent",
                     "minRent", "min_rent", "rent", "totalRent", "price")
    rent_hi = _first(unit, "market_rent_high", "rent_high", "asking_rent",
                     "maxRent", "max_rent", "rent", "totalRent", "price")
    if rent_lo is None and rent_hi is None:
        rent_range = _first(unit, "rent_range", "_rent_range", "rentRange",
                            "priceRange", "price_range")
        if rent_range:
            try:
                from ma_poc.pms.adapters._parsing import parse_rent_range

                rent_lo, rent_hi = parse_rent_range(str(rent_range))
            except Exception:
                pass

    # F10: pass-through unit-level concessions, amenities, and validation flags.
    # Schema stability — keys are always present (None when unset) so downstream
    # readers (observation_reports.build_concessions_report,
    # build_amenities_report) see a consistent shape.
    #
    # The legacy ``concession`` key is occasionally a dict on older adapter
    # paths (Phase A scrape_properties has historically emitted both string
    # and dict shapes). build_concessions_report iterates string content, so
    # coerce non-strings to None here rather than poisoning the report.
    concession_text = unit.get("concession_text")
    if not isinstance(concession_text, str) or not concession_text:
        concession_text = None
    if not concession_text:
        # 2026-05-19 capture-first: concessions arrive under many names
        # across parsers (concession/concessions/special/specials/promo/
        # offer/incentive/deal/savings/free_rent/look_and_lease). Accept
        # any string variant into the canonical field; dicts/lists fall
        # through to _extra (capture-everything net) so nothing is lost.
        legacy = _first(
            unit, "concession", "concessions", "specials_description",
            "special", "specials", "promotion", "promo", "offer",
            "offers", "incentive", "incentives", "deal", "savings",
            "discount", "free_rent", "look_and_lease", "move_in_special",
        )
        if isinstance(legacy, str) and legacy.strip():
            concession_text = legacy
    raw_amenities = unit.get("amenities")
    norm_amenities = _normalize_amenities(raw_amenities) if raw_amenities else None

    norm_beds = _normalize_beds(beds_raw)
    norm_baths = _normalize_baths(baths_raw)
    try:
        from ma_poc.pms.adapters._parsing import compute_floor_plan_id

        floor_plan_id = compute_floor_plan_id(
            property_id, fp_name, norm_beds, norm_baths
        )
    except Exception:
        floor_plan_id = None

    return {
        "beds": norm_beds,
        "baths": norm_baths,
        "floor_plan_name": fp_name if fp_name else None,
        "floor_plan_id": floor_plan_id,
        "area": _format_area(sqft),
        "unit_id": str(uid) if uid not in (None, "", "null") else None,
        # As-displayed operator label ("HOME 302", "APT PH14", an AppFolio
        # street address). Capture-only and frequently NULL — see
        # _parsing.make_unit_dict for why it is never composed. MUST stay in
        # lock-step with the forked copy in scripts/runners/jugnu.py.
        "unit_name": (
            str(unit.get("unit_name")).strip() or None
            if unit.get("unit_name") not in (None, "", "null")
            else None
        ),
        # Explicit placeholder marker (#36) — True for plan-level rows (e.g.
        # SightMap plans with no available units) so consumers don't mistake
        # them for real units missing an id.
        "is_floor_plan_level": _is_floor_plan_level(
            unit, property_plan_level=property_plan_level
        ),
        # Per-unit provenance — which extraction tier produced THIS unit, so a
        # consumer can trust/filter (a Tier-1 API row vs an LLM guess vs a
        # plan-level placeholder). Captured on the internal unit dict but was
        # dropped by this transform until now.
        "extraction_tier": (
            unit.get("extraction_tier") or unit.get("_extraction_tier") or None
        ),
        "rent_low": _format_rent(rent_lo),
        "rent_high": _format_rent(rent_hi),
        "date_captured": scrape_ts.strftime("%Y-%m-%d %H:%M:%S"),
        # Bug 2026-05-13: most adapters emit the long-form key
        # ``availability_date`` (via ``make_unit_dict`` in
        # ``adapters/_parsing.py``). Three direct-write paths in
        # ``adapters/_api_parser.py`` (SightMap line 305, RealPage line
        # 450, generic line 611) also emit the long form. Accept either
        # — ``available_date`` wins when both are populated.
        # 2026-05-24: when availability_status="AVAILABLE" AND the
        # date field is empty/unparseable, default to the scrape date
        # (the unit IS available now — that's what the status says).
        # Previously this case produced available_date=None which made
        # the row look incomplete to downstream consumers even though
        # the operator explicitly flagged it as available. Real cases:
        # UDR JSON-LD ships available_date="" + status AVAILABLE; some
        # Cortland / RentCafe rows ship the same combo when their API
        # has no specific move-in date. _available_date_raw still
        # preserves the original empty/odd string for forensics.
        # 2026-05-25 (canary 1ef1060 follow-up): pass ``has_rent`` so the
        # date resolver can default to scrape-date for units that carry
        # a published rent but whose status field is null / UNAVAILABLE
        # (Knock, G5, MERGED_CROSS_PAGE, TIER_1_5_EMBEDDED cohorts). A
        # positive ``_format_rent`` return (>1) is the rentable-now
        # signal — operators do not publish prices on un-rentable units.
        # Note: rent_lo / rent_hi feed ``_format_rent`` separately below;
        # we re-run the same gate here so the date logic sees the same
        # truth as the rent columns will display.
        #
        # 2026-05-25 (user-flagged via Cedar Ridge + Pleasant View Gardens
        # / JCM Living cohort): gate has_rent on a REAL unit identity.
        # PLAN_LEVEL tiers (GENERIC_PLAN_TEXT_PLAN_LEVEL,
        # APPFOLIO_VANITY_PLAN_LEVEL, REPLI360_PLAN_LEVEL, etc.) emit
        # synthetic rows for plans where the operator advertises rent
        # ranges + a "Check Availability" CTA button but DOES NOT
        # publish per-unit availability. Each row gets an
        # ``inferred_*`` fallback unit_id (via assign_fallback_unit_id
        # downstream). Manufacturing an available_date on these rows
        # would be incorrect — the operator never said any unit was
        # actually available. Only fire has_rent fallback when the
        # unit dict carries a real (non-empty, non-"null") identifier
        # — Knock / G5 / etc. always do; plan-level summaries don't.
        # Cohort impact: protects ~310 plan-level rows across 16 tier
        # categories (TIER_1_DOM_GENERIC_PLAN_TEXT_PLAN_LEVEL,
        # TIER_1_DOM_ENTRATA_PP_SSR_PLAN_LEVEL, etc.) from getting
        # a fabricated scrape-date stamp.
        "available_date": _resolve_available_date(
            _format_date(_first(
                unit, "available_date", "availability_date",
                "internalAvailableDate", "availableDate",
                "date_available", "dateAvailable")),
            _norm_status(
                unit.get("availability_status")
                or unit.get("_availability_status")
            ),
            scrape_ts,
            has_rent=(
                (
                    _format_rent(rent_lo) is not None
                    or _format_rent(rent_hi) is not None
                )
                and uid not in (None, "", "null")
            ),
        ),
        # 2026-05-18 (capture-first): preserve the RAW availability string
        # even when _format_date can't normalize it (text/word/odd format).
        # Data has value; cleaning can be done later off the raw. Clean
        # consumers keep using ``available_date`` (ISO-or-None) unchanged;
        # this never drops a value. Underscore = private passthrough
        # (same convention as _inferred_id / _date_placeholder).
        "_available_date_raw": _raw_str(_first(
            unit, "available_date", "availability_date", "internalAvailableDate",
            "availableDate", "date_available", "dateAvailable")),
        # 2026-05-18: availability_status is emitted by many parsers
        # ("AVAILABLE"/"UNAVAILABLE") via make_unit_dict but the v2
        # transform never mapped it -> 99.7% missing in output. Capture
        # it (same class as available_date). Raw-preserving: light
        # upper-normalize known tokens, else passthrough; None when unset.
        "availability_status": _norm_status(
            unit.get("availability_status") or unit.get("_availability_status")
        ),
        # 2026-05-18: deposit is emitted by securecafe/onesite/others and
        # was dropped. Raw passthrough (clean later) — value has worth.
        "deposit": _raw_str(unit.get("deposit") or unit.get("_deposit")),
        # 2026-05-19 capture-first sweep: floor / building / available_units
        # / rent_range are emitted by make_unit_dict AND direct-write
        # parsers but the v2 transform never mapped them -> silently
        # dropped (same class as available_date). Alias-tolerant (parsers
        # name them differently); raw passthrough, clean later. Additive,
        # None when unset (F10/underscore precedent; validation is
        # required-field-based, no unknown-key rejection).
        "floor": _raw_str(_first(unit, "floor", "_floor", "floor_number",
                                 "floorNumber", "floor_no")),
        "building": _raw_str(_first(unit, "building", "_building",
                                    "building_name", "buildingName",
                                    "building_id", "bldg")),
        "available_units": _raw_str(_first(
            unit, "available_units", "_available_units", "availableUnits",
            "units_available", "available_unit_count", "numberOfUnits",
            "availableUnitsCount", "availableunitscount")),
        "_rent_range_raw": _raw_str(_first(unit, "rent_range",
                                           "_rent_range", "rentRange")),
        "lease_term": _safe_lease_term(unit.get("lease_term") or unit.get("_lease_term")),
        "move_in_date": _format_date(unit.get("move_in_date") or unit.get("_move_in_date")),
        # F10 additions — always present (None when unset).
        "concession_text": concession_text or None,
        # 2026-05-20 preserve-and-flag (per user "error on side of unclean
        # rather than discard"): emit a best-effort cleaned variant and a
        # quality label alongside the raw text. The raw is ALWAYS the
        # ``concession_text`` field above; consumers that prefer a
        # display-ready version can read ``concession_text_clean``.
        # See ma_poc/core/concession_clean.py for the classifier.
        "concession_text_clean": (
            _concession_clean(concession_text) if concession_text else None
        ),
        "_concession_quality": (
            _concession_quality(concession_text) if concession_text else None
        ),
        "concession_value": _safe_float(unit.get("concession_value")),
        "concession_source": unit.get("concession_source") or None,
        # 2026-05-24 offer-taxonomy fields (xlsx reference schema parity).
        # All 5 are populated by make_unit_dict via ma_poc/core/offer_extract.py
        # when concession text is present. None when no offer signal.
        # See ma_poc/tests/core/test_offer_extract.py for the regression
        # oracle anchored on real xlsx rows.
        "offer_banner": unit.get("offer_banner") or None,
        "offer_type": unit.get("offer_type") or None,
        "offer_target": unit.get("offer_target") or None,
        "offer_value": unit.get("offer_value") or None,
        "offer_conditions": unit.get("offer_conditions") or None,
        # 2026-05-20 (canary-output surfacing): PMS-native identifiers
        # the adapters populate via ``source_ids={...}`` in make_unit_dict
        # — used as JOIN keys against external sources (RealPage, SurgeX,
        # cross-canary diffs). Was silently dropped by the v2 transform
        # despite being captured upstream. Examples:
        #   SightMap   → {sightmap_unit_id, sightmap_floor_plan_id}
        #   AppFolio   → {appfolio_listing_id}
        #   Spherexx   → {spherexx_unit_id, spherexx_floorplan_id}
        # Carry through as a dict; xlsx export stringifies for the cell.
        # Empty {} when the adapter hasn't wired it yet (additive,
        # non-breaking).
        "source_ids": dict(unit.get("source_ids") or {}),
        "amenities": norm_amenities,
        # Validation provenance flags (surfaced from schema_gate).
        "_inferred_id": bool(unit.get("_inferred_id")) if "_inferred_id" in unit else None,
        "_date_placeholder": unit.get("_date_placeholder") or None,
        # Capture-everything net: any surfaced attribute-looking key not
        # already mapped (future/unknown column-name variant) preserved
        # raw so nothing is silently lost. None when nothing extra.
        "_extra": _extra_attrs(unit),
    }


def _format_v2_floor_plan(
    plan: dict, scrape_ts: datetime, property_id: str = ""
) -> dict:
    """Format public plan evidence without publishing a synthetic unit ID.

    The shared unit formatter gives the plan the normal schema shape, then
    this wrapper removes identity fields and marks the row as plan-level.
    This keeps plan rent/area evidence available without allowing downstream
    consumers to count it as an available apartment.
    """
    out = _format_v2_unit(plan, scrape_ts, property_id)
    out["unit_id"] = None
    out["unit_name"] = None
    out["is_floor_plan_level"] = True
    flags = [
        part.strip()
        for part in str(out.get("data_quality_flag") or "").split("|")
        if part.strip()
    ]
    if not any("PLAN" in flag.upper() or "UNVERIFIED" in flag.upper() for flag in flags):
        flags.append("PLAN_LEVEL_NO_UNIT_ANCHOR")
    out["data_quality_flag"] = "|".join(flags)
    return out


def _safe_float(val: Any) -> float | None:
    """Coerce to float, return None on failure or empty/null."""
    if val is None or val == "" or val == "null":
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _normalize_amenities(raw: Any) -> list[str] | None:
    """Normalize an amenities list: lowercase, strip, de-duplicate.

    Returns None when input isn't a list or yields no items so the schema
    distinguishes "not extracted" from "explicitly empty".
    """
    if not isinstance(raw, list):
        return None
    seen: set[str] = set()
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        norm = re.sub(r"\s+", " ", item.strip().lower())
        if not norm or norm in seen:
            continue
        seen.add(norm)
        out.append(norm)
    return out or None


# ── Formatting helpers ───────────────────────────────────────────────────────


def _safe_int(val: Any) -> int | None:
    """Convert to int, return None on failure."""
    if val is None:
        return None
    try:
        return int(float(str(val).replace(",", "")))
    except (ValueError, TypeError):
        return None


def _normalize_beds(val: Any) -> int | None:
    """Convert bedroom value to integer. Studio -> 0, clamp [0, 7].

    Returns ``None`` when the source emitted nothing so callers can
    distinguish "studio confirmed" (0) from "not extracted" (None).
    """
    if val is None or val == "":
        return None
    s = str(val).strip().lower()
    if s in ("studio", "s"):
        return 0
    try:
        n = int(float(s))
        return max(0, min(n, 7))
    except (ValueError, TypeError):
        return None


def _normalize_baths(val: Any) -> float | None:
    """Convert bathroom value to nearest 0.5 multiple, clamp [0, 10].

    Returns ``None`` on missing input (same rationale as ``_normalize_beds``).
    """
    if val is None or val == "":
        return None
    try:
        n = float(str(val).strip())
        # Round to nearest 0.5
        n = round(n * 2) / 2
        return max(0.0, min(n, 10.0))
    except (ValueError, TypeError):
        return None


def _format_zip_5(val: Any) -> str | None:
    """Extract first 5 digits from a ZIP code. Strips +4 suffix."""
    if val is None:
        return None
    s = str(val).strip()
    # Match first 5 consecutive digits
    m = re.search(r"\d{5}", s)
    if m:
        return m.group(0)
    # If fewer than 5 digits, left-pad with zeros (e.g. "8854" -> "08854")
    digits = re.sub(r"\D", "", s)
    if digits:
        return digits.zfill(5)[:5]
    return None


def _format_rent(val: Any) -> float | None:
    """Clean rent value: strip currency symbols, commas. Must be > 1 or None."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val) if val > 1 else None
    s = str(val).strip().replace("$", "").replace(",", "").strip()
    try:
        n = float(s)
        return n if n > 1 else None
    except (ValueError, TypeError):
        return None


def _format_area(val: Any) -> int:
    """Convert sqft to int. Clamp to [150, 10000]; -1 is the absent sentinel.

    Rejects values outside realistic apartment bounds (150-10000 sqft). This
    prevents bedroom counts / floor numbers / truncated strings (observed in
    the 2026-04-19 run: 9, 12, 50, 70, 100, 127-129) from leaking as sqft.
    """
    if val is None or val == -1:
        return -1
    try:
        n = int(float(str(val)))
    except (ValueError, TypeError):
        return -1
    if 150 <= n <= 10_000:
        return n
    return -1


# 2026-05-24 (user follow-up to Q1): "apply now / apply" should also be
# considered AVAILABLE. The prior fixed-string set missed common operator
# CTA-style phrasings. This regex matches any phrase where the operator
# is plausibly saying "available now" — including "Apply Now", "Lease
# Today", "Move-In Immediately", "Currently Vacant", "Call For Details"
# (operator-gated date = available now). The status field is the
# authoritative signal anyway; the date-text recognizer just rescues
# rows where the operator wrote a phrase instead of a date.
_AVAILABLE_NOW_RE = re.compile(
    r"\bavail"                                   # available / availability / availabilities
    r"|\bapply\s+(?:now|today|by)\b"             # CTAs in date field
    r"|\blease\s+(?:now|today|by)\b"
    r"|\bmove[\s-]?in"                           # Move-in / Move In / Movein
    r"|\bmoves?[\s-]?in\b"                       # Move In Now / Moves In
    r"|\bready\b"
    r"|\bvacant\b"
    r"|\bcurrently\b"                            # "Currently Vacant" / "Currently Leasing"
    r"|\b(?:now|today|immediate|immediately)\b"  # standalone time tokens
    r"|\bcall\s+(?:for|us|today|now)\b"          # "Call For Details" — operator-gated
    r"|\b(?:tba|tbd)\b"                          # to be announced / determined
    r"|\bto\s+be\s+(?:announced|determined|set)\b"
    r"|\binquire\b",                             # "Inquire For Details" — operator-gated
    re.IGNORECASE,
)


def _resolve_available_date(
    parsed_date: str | None,
    status: str | None,
    scrape_ts: datetime,
    *,
    has_rent: bool = False,
) -> str | None:
    """When the operator effectively says the unit IS rentable but
    ships no parseable move-in date, default the date to the scrape
    timestamp (i.e. "available today / now").

    A unit is treated as rentable-now when EITHER:
      * status explicitly says ``"AVAILABLE"``, OR
      * ``has_rent`` — the unit has a positive rent value
        published. The presence of a price is itself a strong
        rentability signal: operators don't list rents on units
        they can't rent. This catches the canary 1ef1060 regression
        where the Knock adapter mis-flagged ~8,580 of 8,597
        rent-published units as ``UNAVAILABLE`` because Knock's
        ``available`` boolean is a separate signal that's often
        False even when the unit IS being offered. The Knock adapter
        was fixed in parallel, but ``has_rent`` is a defence-in-depth
        for the next operator whose status field is similarly noisy.

    2026-05-24 (user Q): "if it does not show availability date but
    says available, what do we do?". Prior behaviour was to ship
    ``None`` which made the row look incomplete; consumers reading
    just ``available_date`` would treat the unit as date-unknown.
    The fix preserves the raw value in ``_available_date_raw`` so
    forensic analysis can still distinguish the two cases.

    Behaviour:
      * parsed_date present                                  → parsed_date
      * parsed_date None + status == "AVAILABLE"             → scrape date
      * parsed_date None + has_rent=True                     → scrape date
      * parsed_date None + status none/unknown + no rent     → None (unchanged)
    """
    if parsed_date:
        return parsed_date
    if status and status.upper() == "AVAILABLE":
        return scrape_ts.strftime("%Y-%m-%d")
    if has_rent:
        return scrape_ts.strftime("%Y-%m-%d")
    return parsed_date


_DATE_SANITY_YEARS = 5  # ±5 years from today is the acceptance window


def _is_date_in_sane_range(iso_date: str) -> bool:
    """True when the ISO date is within ±_DATE_SANITY_YEARS of today.

    Operators sometimes leave decade-old ``available_date`` values on
    their pages (87b837b QC: 32 dates from 2009 / 2019 on real
    properties, status=AVAILABLE). These are unambiguous data-entry
    garbage and shouldn't propagate. Range chosen conservatively:
    ±5 years catches the obvious junk without dropping legit
    ``available since 8 months ago`` data."""
    try:
        d = datetime.strptime(iso_date, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return False
    today = datetime.now(UTC).date()
    delta_days = (d - today).days
    return -_DATE_SANITY_YEARS * 365 <= delta_days <= _DATE_SANITY_YEARS * 365


def _format_date(val: Any) -> str | None:
    """Normalize date to YYYY-MM-DD. Returns None if unparseable.

    2026-05-18: widened. The prior version accepted only ISO and
    4-digit-year ``m/d/Y`` forms and silently dropped the very common
    AppFolio ``"Available 6/25/26"`` form and securecafe ``"Available"``
    / ``"Available Now"`` text — the root cause of fleet-wide ~0%
    available_date on AppFolio-vanity (parser fills it 100%; transform
    dropped it) and other tiers. Now also handles: a leading
    ``Available|Avail|Move-in|Ready`` prefix; 2-digit years; month-name
    forms; and relative "now/today/immediate/available" → scrape date.
    ISO and 4-digit ``m/d/Y`` behave EXACTLY as before (additive only).

    2026-05-26 (canary 87b837b QC): every parsed date now passes
    through _is_date_in_sane_range — operator-emitted garbage like
    "2009-07-08" returns None.
    """
    def _accept(iso: str | None) -> str | None:
        """Inner-helper: apply the ±5yr sanity bound to every parsed
        output. Calls that return today's date for relative tokens
        (Available Now / etc.) are safe — today is within range."""
        if iso is None:
            return None
        return iso if _is_date_in_sane_range(iso) else None

    if val is None or val == "":
        return None
    s_orig = str(val).strip()
    s = s_orig
    # Already ISO format (unchanged)
    if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        return _accept(s)
    # 2026-05-26 (canary 87b837b QC follow-up): reject negative-status
    # tokens BEFORE the availability-prefix strip. Inputs like
    # "Not Available" / "Not Avail." / "Unavailable" / "Leased" /
    # "Occupied" / "Rented" indicate UNAVAILABLE units — the
    # ``availability_status`` field is the right place to track this,
    # and ``available_date`` should stay empty. Pre-fix, the strip
    # turned "Not Available" → "Not " → fell into ``not s`` → returned
    # today (wrong: implies the unit IS available now). 14 cases in
    # 87b837b's canary output.
    _s_low = s.lower()
    _NEGATIVE_TOKENS = (
        "not available", "not avail", "unavailable",
        "leased", "occupied", "rented", "off market", "off-market",
        "no availability",
        # 2026-07-12: "Call/Contact/Inquire FOR availability" means the
        # operator does NOT publish a date — letting it reach the CTA
        # fallback below fabricates an "available now" stamp. Distinct
        # from "Call For Details"/"Call Now" CTAs, which stay
        # available-now per the 2026-05-24 rule. Prefix forms cover the
        # full words ("call for avail" ⊂ "call for availability").
        # 0 units in the 2026-07-11 canary carry these in the date
        # field — this closes the path (and the long-failing
        # "call for availability"→None test) before an operator
        # exercises it.
        "call for avail", "contact for avail", "inquire for avail",
        "call about avail", "email for avail",
    )
    if any(tok in _s_low for tok in _NEGATIVE_TOKENS):
        return None
    # Strip a leading availability label, e.g. "Available 6/25/26",
    # "Avail. 6/25/26", "Move-in 6/25/26", "Ready 6/25/26".
    s = re.sub(
        r"^\s*(available|avail\.?|move[- ]?in|ready|date available)\s*[:\-]?\s*",
        "",
        s,
        flags=re.IGNORECASE,
    ).strip()
    if not s:
        # Pure text like "Available" with no date ⇒ available now.
        return _accept(datetime.now(UTC).strftime("%Y-%m-%d"))
    # 2026-05-26: YYYYMMDD numeric (no separator). 268 cases in 87b837b
    # — common from RentManager / older RealPage XMLs that emit dates
    # as packed 8-digit ints. Checked BEFORE the strptime cascade so
    # the bare-numeric form is parsed cleanly.
    if re.match(r"^\d{8}$", s):
        try:
            return _accept(datetime.strptime(s, "%Y%m%d").strftime("%Y-%m-%d"))
        except ValueError:
            pass
    # Try common formats — 4-digit-year set unchanged; 2-digit-year and
    # month-name forms added.
    for fmt in (
        "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d", "%m-%d-%Y", "%d-%m-%Y",
        "%m/%d/%y", "%m-%d-%y",
        "%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%B %d %Y",
        "%b %d, %y", "%b %d %y",
    ):
        try:
            return _accept(datetime.strptime(s, fmt).strftime("%Y-%m-%d"))
        except ValueError:
            continue
    # If it's a datetime string, take just the date part (unchanged)
    if len(s) >= 10 and re.match(r"^\d{4}-\d{2}-\d{2}", s):
        return _accept(s[:10])
    # 2026-05-19: no-year month-name forms ("May 19", "Jun. 7", "Jul. 18")
    # — Razz/Spherexx embedded portals omit the year. Product rule: assume
    # the current (run) year. Strip a trailing '.' on an abbreviated month
    # ("Jun." -> "Jun"). Additive: only reached after all year-bearing
    # formats fail, so no existing input changes behavior.
    s_no_year = re.sub(r"^([A-Za-z]{3,9})\.", r"\1", s)
    for fmt in ("%b %d", "%B %d"):
        try:
            return _accept(
                datetime.strptime(s_no_year, fmt)
                .replace(year=datetime.now(UTC).year)
                .strftime("%Y-%m-%d")
            )
        except ValueError:
            continue
    # 2026-05-24 (user follow-up): final fallback — run the AVAILABLE-NOW
    # regex on the ORIGINAL string (before prefix strip) so phrasings
    # like "Available 24/7" (strips to "24/7" which isn't a date) still
    # resolve to today. The regex uses fuzzy anchors (\\bavail / apply
    # \\s+(?:now|today) / lease \\s+(?:now|today) / move[\\s-]?in /
    # ready / vacant / call \\s+(?:for|us|today|now) / inquire / tba /
    # tbd / currently / standalone now/today/immediate) so any operator
    # CTA-style phrasing intent ⇒ available now. Runs LAST so real
    # date strings always win (e.g. "Available 6/25/26" parses 6/25/26
    # via earlier date-format pass, never reaches here).
    if _AVAILABLE_NOW_RE.search(s_orig.lower()):
        return _accept(datetime.now(UTC).strftime("%Y-%m-%d"))
    return None


def _raw_str(val: Any) -> str | None:
    """Capture-first: return the raw value as a trimmed string, or None
    if empty. Never normalizes — preserves text/words/odd formats so
    cleaning can be done later. Data has value."""
    if val is None:
        return None
    s = str(val).strip()
    return s or None


# Fuzzy "this looks like a unit attribute" / "this is noise" token sets.
# `_extra` is the capture-everything safety net: any surfaced key that
# *looks* like an attribute (token match) but isn't a known mapped name
# is preserved raw so a future column-name variant is never silently
# lost. Noise (urls/provenance/telemetry/request bodies) is excluded so
# `_extra` doesn't bloat with non-data.
_ATTR_TOKEN_RE = re.compile(
    r"bed|bath|sq_?ft|square|\barea\b|rent|price|avail|date|floor|unit|"
    r"deposit|concession|special|lease|term|move[_-]?in|building|bldg|"
    r"balcon|parking|\bpet\b|amenit|level|wing|exposure|view|sqfeet|"
    r"sqfootage|occup|ready|waitlist|fee\b",
    re.IGNORECASE,
)
_NOISE_TOKEN_RE = re.compile(
    r"url|source|_tier|extraction|outcome|reason|duration|http|status_code|"
    r"\bbody\b|\bvia\b|\bpmc\b|property_id|property_name|website|"
    r"\bcity\b|\bstate\b|\bzip\b|\bmode\b|site_id|template|\benv\b|"
    r"community_?id|request|response|header|placeholder|inferred|"
    r"date_captured|canonical|provider_id|image|link\b|api\b",
    re.IGNORECASE,
)
# Primary names already pulled by the transform — don't duplicate them.
_MAPPED_SRC = {
    "_bedrooms", "bedrooms", "beds", "numberofbeds", "bedroom", "bed",
    "num_beds", "_bathrooms", "bathrooms", "baths", "numberofbaths",
    "bathroom", "bath", "num_baths", "_floor_plan", "floor_plan_name",
    "floorplan_name", "floorplanname", "fp_name", "floorplan",
    "plan_name", "_sqft", "sqft", "area", "squarefeet", "square_feet",
    "size", "sq_ft", "unit_id", "unit_number", "_unit_number",
    "unitnumber", "unitid", "uid", "apartment_number",
    "market_rent_low", "market_rent_high", "rent_low", "rent_high",
    "asking_rent", "minrent", "min_rent", "rent", "totalrent", "price",
    "maxrent", "max_rent", "rent_range", "_rent_range", "rentrange",
    "pricerange", "price_range", "available_date", "availability_date",
    "internalavailabledate", "availabledate", "date_available",
    "dateavailable", "lease_term", "_lease_term", "move_in_date",
    "_move_in_date", "availability_status", "_availability_status",
    "deposit", "_deposit", "floor", "_floor", "floor_number",
    "floornumber", "floor_no", "building", "_building", "building_name",
    "buildingname", "building_id", "bldg", "available_units",
    "_available_units", "availableunits", "units_available",
    "available_unit_count", "numberofunits", "availableunitscount",
    "concession", "concession_text", "concession_value",
    "concession_source", "specials_description", "amenities",
    "bed_label", "floor_plan_id", "source_api_url", "extraction_tier",
}


def _extra_attrs(unit: dict) -> dict | None:
    """Capture-everything net: surfaced keys that look like a unit
    attribute but aren't a known mapped name, preserved raw. Excludes
    noise/provenance. None when nothing extra."""
    out: dict[str, str] = {}
    for k, v in unit.items():
        kl = str(k).lower()
        if kl in _MAPPED_SRC or kl.startswith("_") and kl in _MAPPED_SRC:
            continue
        if _NOISE_TOKEN_RE.search(kl) or not _ATTR_TOKEN_RE.search(kl):
            continue
        rv = _raw_str(v)
        if rv is not None:
            out[str(k)] = rv
    return out or None


def _first(unit: dict, *keys: str) -> Any:
    """Return the first non-empty value among *keys* (alias-tolerant —
    parsers name the same field differently). Capture-first: no
    normalization, just locate the surfaced value."""
    for k in keys:
        v = unit.get(k)
        if v not in (None, "") and not (isinstance(v, (int, float)) and v == 0):
            return v
    return None


def _norm_status(val: Any) -> str | None:
    """Normalise availability_status into the enum
    {AVAILABLE, UNAVAILABLE, WAITLIST, WAITLISTED, LEASED, PENDING, UNKNOWN}
    or None when unset.

    2026-05-31 QC update: prior capture-first behaviour leaked 181
    free-text values into output. Now every recognized phrase maps to
    its enum value; any unrecognized non-empty string maps to UNKNOWN.

    Mirrors _norm_avail_status() in ma_poc/scripts/runners/jugnu.py —
    keep in sync.
    """
    if val is None:
        return None
    s = str(val).strip()
    if not s:
        return None
    u = s.upper()
    if u in ("AVAILABLE", "UNAVAILABLE", "WAITLIST", "WAITLISTED",
             "LEASED", "PENDING", "UNKNOWN"):
        return u
    if "PENDING" in u or "APPLICATION" in u:
        return "PENDING"
    if "WAITLIST" in u or "WAIT LIST" in u or "WAIT-LIST" in u:
        return "WAITLIST"
    if (
        "NOT AVAILABLE" in u
        or "UNAVAILABLE" in u
        or "SOLD OUT" in u
        or "LEASED OUT" in u
    ):
        return "UNAVAILABLE"
    if "LEASED" in u or "OCCUPIED" in u:
        return "LEASED"
    if "AVAILABLE" in u or "VACANT" in u or "NOW LEASING" in u:
        return "AVAILABLE"
    return "UNKNOWN"


def _safe_lease_term(val: Any) -> int | None:
    """Lease term in months. Must be > 1 if present, else None."""
    if val is None:
        return None
    try:
        n = int(float(str(val)))
        return n if n > 1 else None
    except (ValueError, TypeError):
        return None


# ── V2 Validation ────────────────────────────────────────────────────────────
#
# Post-transform validation on the already-formatted V2 output.
# Returns issues using the same ValidationIssue shape as validation.py.

# V2-specific issue codes — defined in validation.py, imported here.
V2_MISSING_REQUIRED = V.V2_MISSING_REQUIRED
V2_INVALID_APARTMENT_ID = V.V2_INVALID_APARTMENT_ID
V2_INVALID_ZIP = V.V2_INVALID_ZIP
V2_INVALID_BEDS = V.V2_INVALID_BEDS
V2_INVALID_BATHS = V.V2_INVALID_BATHS
V2_INVALID_AREA = V.V2_INVALID_AREA
V2_INVALID_RENT = V.V2_INVALID_RENT
V2_INVALID_LEASE_TERM = V.V2_INVALID_LEASE_TERM

_V2_REQUIRED_PROP_FIELDS = ("apartment_id", "proj_name", "address", "city", "state", "zip_code", "website")


def validate_v2_property(prop: dict, canonical_id: str | None = None) -> list[V.ValidationIssue]:
    """Run V2-specific validation on an already-transformed V2 property dict.

    Returns a list of ValidationIssue objects (same shape as validation.py).
    Empty list means the property passes V2 checks.
    """
    issues: list[V.ValidationIssue] = []
    cid = canonical_id or str(prop.get("apartment_id", "unknown"))

    # ── Property-level required fields ───────────────────────────────────
    for field in _V2_REQUIRED_PROP_FIELDS:
        val = prop.get(field)
        if val is None or (isinstance(val, str) and not val.strip()):
            issues.append(
                V.error(
                    V2_MISSING_REQUIRED,
                    f"V2 required field '{field}' is null or empty",
                    canonical_id=cid,
                    details={"field": field, "value": val},
                )
            )

    # apartment_id: must be integer > 1
    aid = prop.get("apartment_id")
    if aid is not None and (not isinstance(aid, int) or aid < 1):
        issues.append(
            V.error(
                V2_INVALID_APARTMENT_ID,
                f"apartment_id must be integer > 1, got {aid!r}",
                canonical_id=cid,
                details={"value": aid},
            )
        )

    # zip_code: must be exactly 5 digits
    zc = prop.get("zip_code")
    if zc is not None and not re.match(r"^\d{5}$", str(zc)):
        issues.append(
            V.warning(
                V2_INVALID_ZIP,
                f"zip_code is not 5 digits: {zc!r}",
                canonical_id=cid,
                details={"value": zc},
            )
        )

    # ── Unit-level validation ────────────────────────────────────────────
    for idx, unit in enumerate(prop.get("units") or []):
        uid = unit.get("unit_id") or f"unit_{idx}"

        # beds: 0-7
        beds = unit.get("beds")
        if beds is not None and (not isinstance(beds, int) or beds < 0 or beds > 7):
            issues.append(
                V.warning(
                    V2_INVALID_BEDS,
                    f"beds={beds!r} outside [0, 7]",
                    canonical_id=cid,
                    details={"unit_id": uid, "value": beds},
                )
            )

        # baths: 0-10, multiple of 0.5
        baths = unit.get("baths")
        if baths is not None:
            if not isinstance(baths, (int, float)) or baths < 0 or baths > 10:
                issues.append(
                    V.warning(
                        V2_INVALID_BATHS,
                        f"baths={baths!r} outside [0, 10]",
                        canonical_id=cid,
                        details={"unit_id": uid, "value": baths},
                    )
                )
            elif (baths * 2) != int(baths * 2):
                issues.append(
                    V.warning(
                        V2_INVALID_BATHS,
                        f"baths={baths!r} not a multiple of 0.5",
                        canonical_id=cid,
                        details={"unit_id": uid, "value": baths},
                    )
                )

        # area: must be > 0 or exactly -1
        area = unit.get("area")
        if area is not None and area != -1 and (not isinstance(area, int) or area <= 0):
            issues.append(
                V.warning(
                    V2_INVALID_AREA,
                    f"area={area!r} must be > 0 or -1",
                    canonical_id=cid,
                    details={"unit_id": uid, "value": area},
                )
            )

        # rent: must be > 1 if present
        for rent_field in ("rent_low", "rent_high"):
            rv = unit.get(rent_field)
            if rv is not None and (not isinstance(rv, (int, float)) or rv <= 1):
                issues.append(
                    V.warning(
                        V2_INVALID_RENT,
                        f"{rent_field}={rv!r} must be > 1",
                        canonical_id=cid,
                        details={"unit_id": uid, "field": rent_field, "value": rv},
                    )
                )

        # rent_low <= rent_high
        rl = unit.get("rent_low")
        rh = unit.get("rent_high")
        if isinstance(rl, (int, float)) and isinstance(rh, (int, float)) and rl > rh:
            issues.append(
                V.warning(
                    V2_INVALID_RENT,
                    f"rent_low ({rl}) > rent_high ({rh})",
                    canonical_id=cid,
                    details={"unit_id": uid, "low": rl, "high": rh},
                )
            )

        # lease_term: must be > 1 if present
        lt = unit.get("lease_term")
        if lt is not None and (not isinstance(lt, int) or lt <= 1):
            issues.append(
                V.warning(
                    V2_INVALID_LEASE_TERM,
                    f"lease_term={lt!r} must be > 1",
                    canonical_id=cid,
                    details={"unit_id": uid, "value": lt},
                )
            )

        # date_captured: NOT NULL
        dc = unit.get("date_captured")
        if not dc:
            issues.append(
                V.error(
                    V2_MISSING_REQUIRED,
                    f"V2 required field 'date_captured' is null for unit {uid}",
                    canonical_id=cid,
                    details={"unit_id": uid, "field": "date_captured"},
                )
            )

    return issues
