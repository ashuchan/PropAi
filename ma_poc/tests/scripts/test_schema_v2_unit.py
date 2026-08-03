"""F10: schema_v2 _format_v2_unit pass-through for concessions, amenities,
and validation provenance flags. H16, H17 invariants."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ma_poc.core.schema_v2 import (
    _format_v2_floor_plan,
    _format_v2_unit,
    _normalize_amenities,
    _normalize_baths,
)


def test_baths_zero_is_missing_not_confirmed() -> None:
    """Data-audit defect #2: 0 baths is never a real dwelling — a source ``0`` is
    a 'not provided' placeholder → None, not a confirmed count (unlike beds=0
    studio). Real bath counts pass through unchanged."""
    assert _normalize_baths(0) is None
    assert _normalize_baths("0") is None
    assert _normalize_baths(0.0) is None
    assert _normalize_baths(None) is None
    assert _normalize_baths(1) == 1.0
    assert _normalize_baths(2.5) == 2.5

_TS = datetime(2026, 5, 5, 12, 0, 0, tzinfo=UTC)


def test_public_unit_number_survives_canonical_formatting() -> None:
    out = _format_v2_unit(
        {
            "unit_id": "knock_unit_id-7254a4ab-b615-4bc7-b088-4824a25fc03a",
            "unit_number": "4122",
            "unit_name": "4122",
            "rent_low": 1547,
            "availability_status": "AVAILABLE",
        },
        _TS,
    )
    assert out["unit_id"].startswith("knock_unit_id-")
    assert out["unit_number"] == "4122"


def test_baths_cross_field_sanity_clamp() -> None:
    """Data-audit defect #1: a bath count impossible relative to beds (a source
    data-entry error, e.g. regentsparkchicago.com's ``2bn09`` units shipping
    Baths=9) drops to None. Legitimate N-bed/N-bath layouts are never touched."""
    # impossible (beds known >=1) → clamped to None. Studios normalise beds to
    # None (carried via the label), so a bed-aware clamp intentionally can't fire
    # there — we never guess against an unknown bed count.
    for beds, baths in [(2, 9), (1, 4), (3, 6)]:
        out = _format_v2_unit(
            {"unit_number": "u", "beds": beds, "baths": baths, "rent_low": 2000}, _TS
        )
        assert out["baths"] is None, f"{beds}bd/{baths}ba should clamp"
    # legitimate → preserved
    for beds, baths in [(5, 5), (4, 4), (2, 2.5), (1, 2), (0, 1)]:
        out = _format_v2_unit(
            {"unit_number": "u", "beds": beds, "baths": baths, "rent_low": 2000}, _TS
        )
        assert out["baths"] == float(baths), f"{beds}bd/{baths}ba must be preserved"


def test_h16_v2_unit_schema_includes_new_keys() -> None:
    """All F10 keys are always present (None when unset) so downstream
    readers see a stable schema."""
    out = _format_v2_unit({"floor_plan_name": "A1"}, _TS)
    for key in (
        "concession_text",
        "concession_value",
        "concession_source",
        "amenities",
        "_inferred_id",
        "_date_placeholder",
    ):
        assert key in out, f"F10 key {key} missing from _format_v2_unit output"


def test_h17_concession_text_passthrough() -> None:
    """LLM-tier output with concession_text survives the v2 transform."""
    unit = {
        "unit_id": "1004",
        "floor_plan_name": "A1",
        "beds": 1,
        "area": 750,
        "rent_low": 1500,
        "concession_text": "$500 off first month",
        "concession_value": 500.0,
        "concession_source": "specials_section",
    }
    out = _format_v2_unit(unit, _TS)
    assert out["concession_text"] == "$500 off first month"
    assert out["concession_value"] == 500.0
    assert out["concession_source"] == "specials_section"


def test_sightmap_specials_description_maps_to_concession_text() -> None:
    """SightMap stores the specials text under specials_description; F10
    surfaces it as concession_text when no canonical key is present."""
    unit = {
        "unit_id": "u101",
        "floor_plan_name": "A1",
        "beds": 1,
        "area": 750,
        "rent_low": 1500,
        "specials_description": "1 month free on 13-month lease",
    }
    out = _format_v2_unit(unit, _TS)
    assert out["concession_text"] == "1 month free on 13-month lease"


def test_legacy_concession_key_maps_to_canonical() -> None:
    """A unit dict carrying the legacy `concession` (no `_text` suffix) key
    surfaces under the canonical name. SightMap path uses this shape."""
    unit = {
        "unit_id": "u101",
        "floor_plan_name": "A1",
        "beds": 1,
        "area": 750,
        "rent_low": 1500,
        "concession": "Move-in special",
    }
    out = _format_v2_unit(unit, _TS)
    assert out["concession_text"] == "Move-in special"


def test_concession_dict_value_does_not_poison_text_field() -> None:
    """Bug-hunt regression: some legacy adapter paths emit ``concession``
    as a dict ({"description": "...", "value": 500}). build_concessions_report
    iterates string content, so non-string values must coerce to None
    rather than ending up in the report.
    """
    unit = {
        "unit_id": "u101",
        "floor_plan_name": "A1",
        "beds": 1,
        "area": 750,
        "rent_low": 1500,
        "concession": {"description": "Move-in special", "value": 500},
    }
    out = _format_v2_unit(unit, _TS)
    assert out["concession_text"] is None  # dict coerced away


def test_empty_string_concession_text_normalizes_to_none() -> None:
    """``concession_text=""`` must become None so the report's
    ``properties_with_any_concession`` count doesn't get inflated by
    empty strings adapters might leak through."""
    unit = {
        "unit_id": "u101",
        "floor_plan_name": "A1",
        "beds": 1,
        "area": 750,
        "rent_low": 1500,
        "concession_text": "",
        "concession": "   ",  # whitespace-only legacy value
    }
    out = _format_v2_unit(unit, _TS)
    assert out["concession_text"] is None


def test_amenities_normalized_and_deduped() -> None:
    """Adapter emits amenities with mixed casing; v2 output normalizes
    and de-duplicates."""
    unit = {
        "unit_id": "u101",
        "floor_plan_name": "A1",
        "beds": 1,
        "area": 750,
        "rent_low": 1500,
        "amenities": ["Pool", "pool ", "POOL", "Gym"],
    }
    out = _format_v2_unit(unit, _TS)
    assert out["amenities"] == ["pool", "gym"]


def test_inferred_id_flag_passthrough() -> None:
    """Schema-gate-set _inferred_id propagates to v2 output."""
    unit = {
        "unit_id": "inferred_aabbccddeeff0011",
        "floor_plan_name": "A1",
        "beds": 1,
        "area": 750,
        "rent_low": 1500,
        "_inferred_id": True,
    }
    out = _format_v2_unit(unit, _TS)
    assert out["_inferred_id"] is True


def test_date_placeholder_flag_passthrough() -> None:
    """F4 placeholder string surfaces under the canonical key,
    independently of whatever available_date resolves to.

    2026-05-25: under the has_rent fallback added in the canary 1ef1060
    follow-up, a unit with positive rent_low gets available_date
    defaulted to the scrape date (rent published = rentable now). The
    placeholder is still preserved on the separate ``_date_placeholder``
    key for forensic visibility into the operator's original phrasing.
    """
    unit = {
        "unit_id": "u101",
        "floor_plan_name": "A1",
        "beds": 1,
        "area": 750,
        "rent_low": 1500,
        "_date_placeholder": "Spring 2026",
    }
    out = _format_v2_unit(unit, _TS)
    # F4 invariant: placeholder pass-through.
    assert out["_date_placeholder"] == "Spring 2026"
    # New (post-canary): rent published → scrape-date fallback fires.
    assert out["available_date"] == _TS.strftime("%Y-%m-%d")


def test_date_placeholder_without_rent_stays_none() -> None:
    """Sister test to the above: with NO rent published, the
    has_rent gate doesn't fire and the placeholder stays as the
    only signal — available_date is None."""
    unit = {
        "unit_id": "u102",
        "floor_plan_name": "A1",
        "beds": 1,
        "area": 750,
        # no rent_low / rent_high
        "_date_placeholder": "Spring 2026",
    }
    out = _format_v2_unit(unit, _TS)
    assert out["_date_placeholder"] == "Spring 2026"
    assert out["available_date"] is None


@pytest.mark.parametrize("raw,expected", [
    (None, None),
    ([], None),
    ("not a list", None),
    (["pool", "gym"], ["pool", "gym"]),
    (["Pool", "  pool  ", "pool"], ["pool"]),
    (["Pool", 42, "Gym"], ["pool", "gym"]),  # non-strings filtered
])
def test_normalize_amenities_table_driven(raw, expected) -> None:
    assert _normalize_amenities(raw) == expected


# ── Available-date bug 2026-05-13 ─────────────────────────────────────
# All Tier-1 adapters emit the long-form key ``availability_date``; the
# reader previously looked for the short-form ``available_date`` only,
# so ~6,900 Tier-1 rows/day across RentCafe/Entrata/AvalonBay/AppFolio/
# OneSite/SightMap had a NULL availability date even though the source
# payload contained one. The fix accepts either key in the reader and
# emits both keys from the canonical writer (``make_unit_dict``). These
# tests pin both directions so neither side can regress silently.


def test_available_date_reads_short_form() -> None:
    """Reader returns ``available_date`` when the unit dict uses the
    short-form key (canonical going forward)."""
    unit = {
        "unit_id": "u101",
        "floor_plan_name": "A1",
        "available_date": "2026-06-20",
    }
    out = _format_v2_unit(unit, _TS)
    assert out["available_date"] == "2026-06-20"


def test_available_date_reads_long_form_fallback() -> None:
    """Reader falls back to ``availability_date`` (long form) when the
    short-form key is absent — covers every adapter that emits via
    ``make_unit_dict`` plus the three direct-write paths in
    ``adapters/_api_parser.py``. This is the actual regression that the
    fix corrects."""
    unit = {
        "unit_id": "u202",
        "floor_plan_name": "B1",
        "availability_date": "2026-07-15",
    }
    out = _format_v2_unit(unit, _TS)
    assert out["available_date"] == "2026-07-15"


def test_available_date_neither_key_returns_none() -> None:
    """Unit dict with neither key → ``available_date`` is None.
    Distinguishes a real null (no date in the source) from the silent
    drop the bug used to produce."""
    unit = {"unit_id": "u303", "floor_plan_name": "C1"}
    out = _format_v2_unit(unit, _TS)
    assert out["available_date"] is None


def test_available_date_short_form_wins_when_both_set() -> None:
    """When BOTH keys are populated with different values, the short
    form (``available_date``) wins. This is the documented tiebreaker
    so callers can override the long-form value by also setting the
    short form."""
    unit = {
        "unit_id": "u404",
        "floor_plan_name": "D1",
        "available_date": "2026-06-01",
        "availability_date": "2026-07-01",
    }
    out = _format_v2_unit(unit, _TS)
    assert out["available_date"] == "2026-06-01"


def test_available_date_empty_string_long_form_falls_through() -> None:
    """An empty-string short-form key (which is falsy) falls through to
    the long form. Adapters that emit ``available_date=""`` for missing
    data should still surface a populated long-form key when present."""
    unit = {
        "unit_id": "u505",
        "floor_plan_name": "E1",
        "available_date": "",
        "availability_date": "2026-08-10",
    }
    out = _format_v2_unit(unit, _TS)
    assert out["available_date"] == "2026-08-10"


def test_make_unit_dict_emits_both_available_date_keys() -> None:
    """Option A in ``adapters/_parsing.py``: the canonical writer emits
    BOTH ``availability_date`` (legacy long form, used by every adapter)
    AND ``available_date`` (short form, matches schema_v2 reader). This
    pins the contract so a future cleanup that drops the alias is a
    deliberate, test-flagged change."""
    from ma_poc.pms.adapters._parsing import make_unit_dict

    unit = make_unit_dict(
        unit_number="101",
        availability_date="2026-06-20",
    )
    assert unit["availability_date"] == "2026-06-20"
    assert unit["available_date"] == "2026-06-20"


def test_make_unit_dict_no_date_emits_empty_both_keys() -> None:
    """When no availability date is passed, both keys are present as
    empty strings (the existing default) — keeps the v2 schema reader
    seeing a stable shape across rows."""
    from ma_poc.pms.adapters._parsing import make_unit_dict

    unit = make_unit_dict(unit_number="101")
    assert unit["availability_date"] == ""
    assert unit["available_date"] == ""


# ─────────────────────────────────────────────────────────────────────
# 2026-05-24 — AVAILABLE-no-date handling (user Q)
#
# When an operator ships availability_status="AVAILABLE" but the date
# field is empty / unparseable, downstream previously got
# available_date=None which made the row look incomplete. The fix:
# default to scrape_ts when status says AVAILABLE.
# ─────────────────────────────────────────────────────────────────────


def test_available_no_date_defaults_to_scrape_date() -> None:
    """The user-Q signature case: empty available_date + AVAILABLE
    status → use scrape_ts as the date (unit IS available NOW)."""
    unit = {
        "unit_id": "u-avail-001",
        "floor_plan_name": "A1",
        "availability_status": "AVAILABLE",
        "availability_date": "",  # empty — would have been None
    }
    out = _format_v2_unit(unit, _TS)
    assert out["available_date"] == _TS.strftime("%Y-%m-%d")
    assert out["availability_status"] == "AVAILABLE"
    # Raw preserved for forensics
    assert out["_available_date_raw"] is None  # empty string → None passthrough


def test_available_with_real_date_preserves_date() -> None:
    """When BOTH date and AVAILABLE status are set, the date wins
    (status fallback only fires when date is empty/None)."""
    unit = {
        "unit_id": "u-avail-002",
        "floor_plan_name": "A1",
        "availability_status": "AVAILABLE",
        "availability_date": "2026-08-15",
    }
    out = _format_v2_unit(unit, _TS)
    assert out["available_date"] == "2026-08-15"


def test_unavailable_with_empty_date_stays_none() -> None:
    """Only AVAILABLE status triggers the scrape-date fallback —
    UNAVAILABLE / UNKNOWN / missing status preserves None."""
    unit = {
        "unit_id": "u-unavail",
        "floor_plan_name": "A1",
        "availability_status": "UNAVAILABLE",
        "availability_date": "",
    }
    out = _format_v2_unit(unit, _TS)
    assert out["available_date"] is None
    assert out["availability_status"] == "UNAVAILABLE"


def test_no_status_with_empty_date_stays_none() -> None:
    """Status field absent → no fallback (the prior 'genuinely unknown'
    case stays unchanged) — UNLESS rent is published. See the
    ``rent_present_*`` block below for the rent-present escape hatch
    added 2026-05-25."""
    unit = {"unit_id": "u-no-status", "floor_plan_name": "A1"}
    out = _format_v2_unit(unit, _TS)
    assert out["available_date"] is None
    assert out["availability_status"] is None


# ─────────────────────────────────────────────────────────────────────
# 2026-05-25 — canary 1ef1060 regression follow-up: has_rent escape
#
# The canary at SHA 1ef1060 lost ~605 full-rows because the Knock
# adapter (and to a lesser degree G5 inventory + MERGED_CROSS_PAGE +
# TIER_1_5_EMBEDDED) shipped units with rent + sqft + plan name + unit
# number, but with status=UNAVAILABLE or null. The Q1 fallback only
# fired on status="AVAILABLE", so available_date stayed None and the
# row failed the 5-of-5 completeness check despite being a valid
# rent-published listing.
#
# Fix: when a unit has positive rent published (rent_low or rent_high
# > 1 after _format_rent normalization), treat that as the
# rentable-now signal even if status is silent. Operators don't list
# prices on units they can't rent.
# ─────────────────────────────────────────────────────────────────────


def test_rent_present_status_unavailable_does_not_invent_scrape_date() -> None:
    """A catalogue/full-roster rent cannot override explicit UNAVAILABLE."""
    unit = {
        "unit_id": "u-knock-001",
        "floor_plan_name": "A1",
        "bedrooms": 1,
        "bathrooms": 1,
        "sqft": 750,
        "rent_low": 1495.0,
        "rent_high": 1495.0,
        "availability_status": "UNAVAILABLE",
        # No date field at all
    }
    out = _format_v2_unit(unit, _TS)
    assert out["available_date"] is None
    # Status is preserved as-is — we don't rewrite it, just fix the date.
    assert out["availability_status"] == "UNAVAILABLE"
    # The source-backed fields remain populated; date completeness must not be
    # achieved by contradicting the explicit status.
    assert out["unit_id"] == "u-knock-001"
    assert out["rent_low"] == 1495.0
    assert out["area"] == 750
    assert out["floor_plan_name"] == "A1"
    assert out["available_date"] is None


def test_rent_present_status_null_defaults_to_scrape_date() -> None:
    """G5 / MERGED_CROSS_PAGE / EMBEDDED case: rent published but
    no status field at all. Pre-fix → None. Post-fix → scrape date."""
    unit = {
        "unit_id": "u-g5-001",
        "floor_plan_name": "B2",
        "bedrooms": 2,
        "sqft": 1050,
        "rent_low": 2100.0,
        # No availability_status, no date
    }
    out = _format_v2_unit(unit, _TS)
    assert out["available_date"] == _TS.strftime("%Y-%m-%d")


def test_rent_present_rent_high_only_still_triggers() -> None:
    """Only rent_high populated (rent_range parser sometimes hits this
    path) still counts as ``has_rent`` and triggers the fallback."""
    unit = {
        "unit_id": "u-rh-only",
        "floor_plan_name": "C1",
        "rent_high": 1850.0,
    }
    out = _format_v2_unit(unit, _TS)
    assert out["available_date"] == _TS.strftime("%Y-%m-%d")


def test_rent_present_via_rent_range_string_triggers() -> None:
    """Adapters that ship ``rent_range`` as a string (some DOM paths)
    flow through parse_rent_range → rent_lo/rent_hi → has_rent=True."""
    unit = {
        "unit_id": "u-range",
        "floor_plan_name": "D1",
        "rent_range": "$1,800 - $2,200",
    }
    out = _format_v2_unit(unit, _TS)
    assert out["available_date"] == _TS.strftime("%Y-%m-%d")


def test_no_rent_no_status_still_none() -> None:
    """Genuine 'we have no data' case: no rent, no status, no date.
    Must still produce available_date=None (don't manufacture a date
    from thin air). This is the invariant that protects against
    over-filling the column."""
    unit = {
        "unit_id": "u-nothing",
        "floor_plan_name": "E1",
        # No rent, no status, no date
    }
    out = _format_v2_unit(unit, _TS)
    assert out["available_date"] is None


def test_rent_zero_or_negative_does_not_trigger() -> None:
    """``_format_rent`` rejects values ≤ 1 (sentinel / placeholder).
    Those must NOT count as has_rent — they're the same as no rent."""
    unit = {
        "unit_id": "u-zero-rent",
        "floor_plan_name": "F1",
        "rent_low": 0,
        "rent_high": -1,
    }
    out = _format_v2_unit(unit, _TS)
    assert out["available_date"] is None
    # Confirm the rent columns are also None (proving _format_rent
    # gated both fields the same way)
    assert out["rent_low"] is None
    assert out["rent_high"] is None


def test_rent_present_real_date_still_wins() -> None:
    """When a real parseable date IS present, it wins over the
    scrape-date fallback — regardless of has_rent. This protects
    the existing date-precedence invariant."""
    unit = {
        "unit_id": "u-real-date",
        "floor_plan_name": "G1",
        "rent_low": 1700.0,
        "availability_date": "2026-08-20",
    }
    out = _format_v2_unit(unit, _TS)
    assert out["available_date"] == "2026-08-20"


def test_rent_present_status_available_still_wins() -> None:
    """Mixed-signal case: rent published AND status=AVAILABLE AND no
    date. Either signal alone triggers fallback; both together still
    produce scrape date (no double-counting or weird interaction)."""
    unit = {
        "unit_id": "u-both",
        "floor_plan_name": "H1",
        "rent_low": 1600.0,
        "availability_status": "AVAILABLE",
    }
    out = _format_v2_unit(unit, _TS)
    assert out["available_date"] == _TS.strftime("%Y-%m-%d")


# ─────────────────────────────────────────────────────────────────────
# 2026-05-25 user-flagged via Cedar Ridge + Pleasant View Gardens —
# Phase 16 has_rent fallback PLAN_LEVEL guard.
#
# Pre-guard: ANY unit with positive rent + no date got
# available_date=scrape_date. This over-fired on plan-level rows
# emitted by TIER_1_DOM_GENERIC_PLAN_TEXT_PLAN_LEVEL,
# TIER_1_DOM_ENTRATA_PP_SSR_PLAN_LEVEL, TIER_1_API_REPLI360_PLAN_LEVEL,
# etc. where the operator publishes:
#   * plan-level rent range ($1,475 1BR, $1,750 2BR)
#   * a "Check Availability" CTA button (NOT a real availability list)
# and our parser emits a synthetic row per plan with an ``inferred_*``
# fallback unit_id. Manufacturing a date here is incorrect — the
# operator never said any unit is actually available right now.
#
# Post-guard: require BOTH (a) rent published AND (b) a real
# (non-empty, non-"null") unit_id on the source dict. Plan-level rows
# always lack a real unit_id (rescue assigns ``inferred_*``), so they
# now correctly stay with available_date=None.
#
# Knock / G5 / EMBEDDED / MERGED cohorts that motivated Phase 16 all
# carry real unit_ids ("1833", "U-101", etc.) — unaffected by the
# guard. The has_rent fallback still recovers their dates.
# ─────────────────────────────────────────────────────────────────────


def test_plan_level_no_unit_id_does_not_get_date_manufactured() -> None:
    """The Cedar Ridge / Pleasant View signature: plan-level row with
    rent + no unit_id source. Must NOT get a manufactured date.

    Pre-guard: would have produced available_date=scrape_date.
    Post-guard: available_date stays None (operator hasn't published
    per-unit availability)."""
    unit = {
        # NO unit_id, unit_number, or _unit_number — plan-level
        "floor_plan_name": "1 Bedroom / 1 Bath",
        "beds": 1,
        "baths": 1,
        "rent_low": 1475.0,
        "rent_high": 1475.0,
    }
    out = _format_v2_unit(unit, _TS)
    assert out["available_date"] is None, (
        f"plan-level row (no unit_id source) must NOT get a "
        f"manufactured date; got {out['available_date']!r}"
    )
    # Confirm the row IS otherwise valid (rent + plan name preserved)
    assert out["rent_low"] == 1475.0
    assert out["floor_plan_name"] == "1 Bedroom / 1 Bath"


def test_plan_level_empty_string_unit_id_does_not_get_date() -> None:
    """Empty-string unit_id is also plan-level (gets rescued to
    inferred_* downstream). Same guard applies."""
    unit = {
        "unit_id": "",
        "floor_plan_name": "2 Bedroom",
        "rent_low": 1750.0,
    }
    out = _format_v2_unit(unit, _TS)
    assert out["available_date"] is None


def test_plan_level_null_string_unit_id_does_not_get_date() -> None:
    """``unit_id="null"`` (string literal, observed in some adapters)
    is also plan-level."""
    unit = {
        "unit_id": "null",
        "floor_plan_name": "Studio",
        "rent_low": 1200.0,
    }
    out = _format_v2_unit(unit, _TS)
    assert out["available_date"] is None


def test_real_unit_id_still_gets_has_rent_fallback() -> None:
    """The Knock / G5 / EMBEDDED signature that motivated Phase 16:
    REAL unit_id + rent + no date → date defaults to scrape date.
    Guard MUST NOT regress this."""
    unit = {
        "unit_id": "1833",  # real per-apartment id
        "floor_plan_name": "B1",
        "rent_low": 1505.0,
    }
    out = _format_v2_unit(unit, _TS)
    assert out["available_date"] == _TS.strftime("%Y-%m-%d")


def test_real_unit_number_also_satisfies_the_guard() -> None:
    """Some adapters emit ``unit_number`` instead of ``unit_id``.
    Both satisfy the real-identity check via the alias chain."""
    unit = {
        "unit_number": "Apt 101",  # alias for unit_id
        "floor_plan_name": "B1",
        "rent_low": 1500.0,
    }
    out = _format_v2_unit(unit, _TS)
    assert out["available_date"] == _TS.strftime("%Y-%m-%d")


def test_plan_level_with_status_available_still_fires() -> None:
    """The guard only affects the has_rent path. If the operator
    explicitly says status=AVAILABLE, the original Q1 fallback still
    fires regardless of unit_id presence (operator explicitly stated
    availability — we trust that)."""
    unit = {
        # No unit_id, but status is explicit
        "floor_plan_name": "1 Bedroom",
        "rent_low": 1200.0,
        "availability_status": "AVAILABLE",
    }
    out = _format_v2_unit(unit, _TS)
    assert out["available_date"] == _TS.strftime("%Y-%m-%d")


@pytest.mark.parametrize("text", [
    # Existing widened phrasings (proven 2026-05-24 morning fixed-list)
    "ready",
    "Move-in Ready",
    "MOVE IN READY",
    "vacant",
    "Available Immediately",
    "Available Today",
    "TBA",
    "TBD",
    "to be announced",
    # 2026-05-24 (user follow-up): regex-based recognizer adds these
    # CTA-style phrasings operators put in the date field instead of
    # a real date. "Apply Now" / "Apply Today" / "Lease Today" / etc.
    "Apply Now",
    "Apply Today",
    "Apply By",
    "Lease Now",
    "Lease Today",
    "Currently Available",
    "Currently Vacant",
    "Currently Leasing",
    "Now Available",
    "Available 24/7",
    "Immediately",
    "Move In",
    "MOVE-IN",
    "Move-In Now",
    "Inquire",
    "Inquire For Details",
    "Call For Details",
    "Call Today",
    "Call Now",
    "Call Us",
    "to be determined",
    "to be set",
])
def test_format_date_widened_text_recognizer(text: str) -> None:
    """The text-recognizer is regex-based (not fixed-string): operator-
    specific phrasings that mean 'available now' resolve to scrape
    date instead of None. Catches Mark-Taylor 'vacant', RentCafe 'TBA',
    AppFolio 'Apply Now', 'Lease Today', 'Call For Details', etc.
    """
    from datetime import UTC, datetime

    from ma_poc.core.schema_v2 import _format_date
    out = _format_date(text)
    assert out == datetime.now(UTC).strftime("%Y-%m-%d"), (
        f"{text!r} should resolve to today; got {out!r}"
    )


@pytest.mark.parametrize("text", [
    # These look date-ish or placeholder-ish but should NOT resolve
    # to today — they're real future dates or unparseable placeholders.
    # Negative cases to prove the regex isn't too aggressive.
    "Spring 2026",       # season placeholder, not "available now"
    "Q3 2026",           # quarter placeholder
    "End of June",       # vague date reference, no apply/avail anchor
    "Pending",           # status word, not availability
    "Sold",              # final state, not available
    "Reserved",          # not available
    "Off-Market",        # not available
])
def test_format_date_recognizer_does_not_overmatch(text: str) -> None:
    """Negative cases: phrases that look date-ish but DON'T claim
    'available now' should NOT default to today. Prevents the regex
    from being too aggressive."""
    from datetime import UTC, datetime

    from ma_poc.core.schema_v2 import _format_date

    out = _format_date(text)
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    assert out != today, (
        f"{text!r} should NOT default to today (no available/apply intent); "
        f"got {out!r}"
    )


def test_make_unit_dict_then_format_v2_unit_integration() -> None:
    """End-to-end: writer → reader chain produces a populated
    ``available_date``. This is the failure mode of the bug — adapters
    wrote correctly, but the reader dropped it. Pinning this asserts
    both halves of the fix work together."""
    from ma_poc.pms.adapters._parsing import make_unit_dict

    unit = make_unit_dict(
        unit_number="101",
        bedrooms="1",
        floor_plan_name="A1",
        availability_date="2026-06-20",
    )
    out = _format_v2_unit(unit, _TS)
    assert out["available_date"] == "2026-06-20"


# ── #36: explicit floor-plan-level placeholder flag ─────────────────────────
def test_is_floor_plan_level_sightmap_plan_presence() -> None:
    out = _format_v2_unit(
        {"floor_plan_name": "The Blue Elderberry", "data_quality_flag": "SIGHTMAP_PLAN_PRESENCE"},
        _TS,
    )
    assert out["is_floor_plan_level"] is True


def test_is_floor_plan_level_plan_level_tier() -> None:
    out = _format_v2_unit(
        {"floor_plan_name": "1 Bedroom", "extraction_tier": "TIER_1_DOM_GENERIC_PLAN_TEXT_PLAN_LEVEL"},
        _TS,
    )
    assert out["is_floor_plan_level"] is True


def test_is_floor_plan_level_false_for_real_unit() -> None:
    out = _format_v2_unit(
        {"floor_plan_name": "A1", "unit_id": "101", "rent_low": 1500,
         "availability_status": "AVAILABLE", "extraction_tier": "TIER_1_API_ENTRATA"},
        _TS,
    )
    assert out["is_floor_plan_level"] is False


def test_floor_plan_wrapper_clears_identity_but_keeps_public_plan_data() -> None:
    """Floor-plan output cannot be mistaken for an apartment row."""
    out = _format_v2_floor_plan(
        {
            "unit_id": "inferred_a1_700_1_1",
            "floor_plan_name": "A1",
            "beds": 1,
            "baths": 1,
            "area": 700,
            "rent_low": 1500,
        },
        _TS,
        "P1",
    )
    assert out["unit_id"] is None
    assert out["unit_name"] is None
    assert out["is_floor_plan_level"] is True
    assert out["rent_low"] == 1500.0
    assert out["area"] == 700
    assert "PLAN_LEVEL_NO_UNIT_ANCHOR" in out["data_quality_flag"]


# ── plan-level-flag completeness (2026-07-28) ───────────────────────────────
#
# Measured on run-2026-07-27-full-0d54ca7 (4,982 properties / 104,964 unit
# rows): 5,427 rows carried ``is_floor_plan_level`` and 5,399 of them (99.5%)
# were SightMap — the one adapter that stamps a ROW-level
# ``data_quality_flag``. 1,675 rows were plan-level in shape yet unflagged,
# including 259 from ``TIER_1_API_RENTCAFE_SHAPE_REJECTED_PLAN_LEVEL`` and 145
# from ``TIER_1_DOM_GENERIC_PLAN_TEXT`` — tiers that declare plan-ness in their
# own names. The flag is now derived at the single output-boundary choke point
# from the row's markers AND the property-level marker, gated on the row having
# no real apartment anchor.

# Every tier literal here is a real code in this repo (harvested with
# ``grep -rhoE '"TIER[A-Z0-9_]*"' --include=*.py ma_poc | sort -u``).
_PLAN_MARKER_TABLE: list[tuple[str, str, bool, str]] = [
    # (extraction_tier, data_quality_flag, marker_expected, why)
    # ---- MUST match -------------------------------------------------------
    ("TIER_3_PLAN_TEXT", "", True, "plan_text.py marketing plan rows"),
    ("TIER_1_DOM_GENERIC_PLAN_TEXT", "", True, "generic_plan_text.py"),
    ("TIER_1_DOM_GENERIC_PLAN_TEXT_UNIT_STREET", "", True,
     "marker present; the ANCHOR gate is what saves this row"),
    ("TIER_1_API_RENTCAFE_SHAPE_REJECTED_PLAN_LEVEL", "", True, "scraper Path-C"),
    ("TIER_1_DOM_RENTALADDRESS_PLAN_LEVEL", "", True, "rentaladdress plan rows"),
    ("TIER_2_JSONLD_PLAN_LEVEL", "", True, "post_process-suffixed tier"),
    ("", "PLAN_RANGE_ONLY", True, "generic_plan_text.py:1096"),
    ("", "PLAN_LEVEL_MIN_ONLY", True, "_mark_taylor.py:286"),
    ("", "PLAN_LEVEL_STARTING_RENT", True, "_mark_taylor.py:410"),
    ("", "PLAN_LEVEL_NO_VACANT_UNIT", True, "_apts247.py:317"),
    ("", "PLAN_LEVEL_NO_UNIT_ANCHOR", True, "post_process.py:155"),
    ("", "SQFT_NOT_PUBLISHED|PLAN_RANGE_ONLY", True, "pipe-delimited, 2nd token"),
    # ---- MUST NOT match ---------------------------------------------------
    ("TIER_1_API_SIGHTMAP", "", False, "real SightMap unit rows"),
    ("TIER_1_API_SIGHTMAP_DIRECT", "", False, "real SightMap unit rows"),
    ("TIER_1_API_APTS247_FLOORPLANS", "", False,
     "'FLOORPLANS' is a route name, not a plan-level marker"),
    ("TIER_1_API_RENTCAFE_SECURECAFE_FROM_PLAN", "", False,
     "a unit sourced FROM a plan page is still a unit"),
    ("TIER_1_DOM_WIX_FLOOR_PLANS", "", False, "route name"),
    ("TIER_1_DOM_MARK_TAYLOR_RENDERED_PLAN_CARD", "", False,
     "carries its own PLAN_LEVEL_* dqf; the tier alone must not fire"),
    ("ENCORESKYLINE_NO_PLAN_LINKS", "", False, "'NO_PLAN_LINKS' is a diagnosis"),
    ("TIER_1_DOM_UNIT_TABLE", "", False, "plan_text.py unit-table rows"),
    ("TIER_1_DOM_APPFOLIO_VANITY", "", False, "real units"),
    ("TIER_MERGED_CROSS_PAGE", "", False, "no plan marker"),
    ("TIER_1_API_RENTCAFE_SECURECAFE", "", False, "no plan marker"),
    ("", "SQFT_NOT_PUBLISHED", False, "rentcafe.py:1912 / appfolio.py:516"),
    ("", "RENT_NOT_PUBLISHED", False, "sightmap.py:733"),
    ("", "NO_AVAILABILITY_NOW", False, "_no_availability.py:362"),
    ("", "UNIT_LEVEL_PRICING_MISSING", False, "scraper.py:616 — a UNIT flag"),
    ("", "UNIT_LEVEL_PARTIAL_MISSING_SQFT", False, "scraper.py:618 — a UNIT flag"),
    ("", "UNIT_ROUTE_UNVERIFIED", False,
     "'we could not verify the unit route', not 'this is a plan'"),
]


@pytest.mark.parametrize(
    ("tier", "dqf", "expected", "why"),
    _PLAN_MARKER_TABLE,
    ids=[f"{t or 'no-tier'}|{d or 'no-dqf'}" for t, d, _, _ in _PLAN_MARKER_TABLE],
)
def test_plan_marker_table(tier: str, dqf: str, expected: bool, why: str) -> None:
    """Table test — the marker matcher must not over-reach onto 'PLAN' words."""
    from ma_poc.core.schema_v2 import _has_plan_marker

    assert _has_plan_marker(tier.upper(), dqf.upper()) is expected, why


# Every distinct ``*_PLAN_LEVEL`` / ``*PLAN_TEXT`` tier this repo emits, plus
# the plan-level tiers observed in the 2026-07-27 run.
_PLAN_TIERS: tuple[str, ...] = (
    "TIER_1_API_APTS247_PLAN_LEVEL",
    "TIER_1_API_ENTRATA_PLAN_LEVEL",
    "TIER_1_API_RENTCAFE_NO_RESPONSE_PLAN_LEVEL",
    "TIER_1_API_RENTCAFE_PLAN_LEVEL",
    "TIER_1_API_RENTCAFE_SECURECAFE_PLAN_LEVEL",
    "TIER_1_API_RENTCAFE_SHAPE_REJECTED_PLAN_LEVEL",
    "TIER_1_API_REPLI360_PLAN_LEVEL",
    "TIER_1_API_SIGHTMAP_IFRAME_PLAN_LEVEL",
    "TIER_1_API_SIGHTMAP_PLAN_LEVEL",
    "TIER_1_DOM_GENERIC_PLAN_TEXT",
    "TIER_1_DOM_GENERIC_PLAN_TEXT_FROM_PRICE",
    "TIER_1_DOM_GENERIC_PLAN_TEXT_JSONLD_PRICERANGE",
    "TIER_1_DOM_GENERIC_PLAN_TEXT_LABELED_PRICE",
    "TIER_1_DOM_GENERIC_PLAN_TEXT_PLAN_LEVEL",
    "TIER_1_DOM_GENERIC_PLAN_TEXT_REALPAGE_BLOB",
    "TIER_1_DOM_GENERIC_PLAN_TEXT_WIX_LABELED_BLOCK",
    "TIER_1_DOM_GENERIC_PLAN_TEXT_WIX_SECTION_PLAN",
    "TIER_1_DOM_REALPAGE_CWS_PLAN_LEVEL",
    "TIER_1_DOM_RENTALADDRESS_PLAN_LEVEL",
    "TIER_2_JSONLD_PLAN_LEVEL",
    "TIER_3_DOM_GENERIC_PLAN_LEVEL",
    "TIER_3_DOM_PLAN_LEVEL",
    "TIER_3_PLAN_TEXT",
)


@pytest.mark.parametrize("tier", _PLAN_TIERS)
def test_no_plan_level_tier_can_emit_an_unflagged_row(tier: str) -> None:
    """No tier whose name contains PLAN_LEVEL or PLAN_TEXT may ship a row with
    ``is_floor_plan_level`` unset, when that row has no apartment anchor.

    This is the guard the 2026-07-27 run needed: 145 TIER_1_DOM_GENERIC_PLAN_TEXT
    and 259 TIER_1_API_RENTCAFE_SHAPE_REJECTED_PLAN_LEVEL rows shipped unflagged.
    """
    out = _format_v2_unit(
        {"floor_plan_name": "A1", "beds": 1, "baths": 1, "extraction_tier": tier},
        _TS,
    )
    assert out["is_floor_plan_level"] is True, tier


# Tiers that MARK plan-text extraction without asserting the row is a plan.
# A ``*_PLAN_LEVEL`` SUFFIX is deliberately excluded: that suffix is an
# adapter's explicit "this row IS a plan" assertion (and
# ``scraper.promote_verified_unit_rows`` strips it the moment the row gains a
# native anchor), so it stays authoritative — pinned by the pre-existing
# ``test_is_floor_plan_level_plan_level_tier``.
_PLAN_TEXT_TIERS: tuple[str, ...] = tuple(
    t for t in _PLAN_TIERS if not t.endswith("_PLAN_LEVEL")
) + ("TIER_1_DOM_GENERIC_PLAN_TEXT_UNIT_STREET",)


@pytest.mark.parametrize("tier", _PLAN_TEXT_TIERS)
def test_plan_level_tier_with_a_real_anchor_stays_unit_level(tier: str) -> None:
    """INVERSE-ERROR guard. ``generic_plan_text`` legitimately emits a
    unit-level row (``..._PLAN_TEXT_UNIT_STREET``) carrying a real unit number;
    flagging it would move a genuine apartment out of the client's unit set."""
    out = _format_v2_unit(
        {
            "floor_plan_name": "A1",
            "unit_number": "20H",
            "rent_low": 2600,
            "availability_status": "AVAILABLE",
            "extraction_tier": tier,
        },
        _TS,
    )
    assert out["is_floor_plan_level"] is False, tier


def test_property_level_plan_marker_flags_rows_carrying_a_plain_tier() -> None:
    """The property records plan-ness on ``extraction_tier_used`` (scraper.py
    :2151 / :2308) while the ROWS keep the plain adapter tier. The row-only
    predicate never saw it — 605 TIER_1_API_RENTCAFE_NO_RESPONSE_PLAN_LEVEL
    rows in the 2026-07-27 run shipped unflagged for exactly this reason."""
    from ma_poc.core.schema_v2 import property_is_plan_level

    result = {"extraction_tier_used": "TIER_1_API_RENTCAFE_NO_RESPONSE_PLAN_LEVEL"}
    assert property_is_plan_level(result) is True
    plan_row = {"floor_plan_name": "A1", "extraction_tier": "TIER_1_API_RENTCAFE"}
    out = _format_v2_unit(
        plan_row, _TS, "P1", property_plan_level=property_is_plan_level(result)
    )
    assert out["is_floor_plan_level"] is True
    # …and the same property's anchored rows stay units.
    unit_row = {
        "floor_plan_name": "A1",
        "unit_number": "412",
        "extraction_tier": "TIER_1_API_RENTCAFE",
    }
    out2 = _format_v2_unit(
        unit_row, _TS, "P1", property_plan_level=property_is_plan_level(result)
    )
    assert out2["is_floor_plan_level"] is False


def test_property_is_plan_level_reads_verdict_quality() -> None:
    """``_verdict_quality=SUCCESS_PLAN_LEVEL`` is the other property-level
    convention the scraper writes at the same two sites."""
    from ma_poc.core.schema_v2 import property_is_plan_level

    assert property_is_plan_level({"_verdict_quality": "SUCCESS_PLAN_LEVEL"}) is True
    assert property_is_plan_level({"extraction_tier_used": "TIER_1_API_SIGHTMAP"}) is False
    assert property_is_plan_level({}) is False
    assert property_is_plan_level(None) is False


def test_plan_level_flag_never_un_flags_a_previously_flagged_row() -> None:
    """Monotonicity — the pre-2026-07-28 True arms must still return True.

    50 flagged SightMap rows in the 2026-07-27 run carry a REAL area (662, 950,
    691, 300, 450 sqft): a plan row may legitimately publish a size while having
    no availability. Those must not be 'fixed' back into units."""
    from ma_poc.core.schema_v2 import _is_floor_plan_level

    for unit in (
        {"data_quality_flag": "SIGHTMAP_PLAN_PRESENCE", "area": 662,
         "unit_number": "A1", "floor_plan_name": "A1"},
        {"extraction_tier": "TIER_1_API_SIGHTMAP_IFRAME_PLAN_LEVEL",
         "unit_number": "101", "area": 950},
        {"is_floor_plan_level": True, "unit_number": "202", "area": 691},
    ):
        assert _is_floor_plan_level(unit) is True, unit


# ── 2026-07-29 zero-inventory availability contract ─────────────────────────
# Product-owner decision: zero-inventory plan rows are still EMITTED (the
# client wants to know the plan exists) but must read cleanly UNAVAILABLE —
# never null, never UNKNOWN. Measured on run-2026-07-27 (104,964 rows, offline
# replay): 1,036 plan rows shipped AVAILABLE / null / UNKNOWN with no rent and
# no unit anchor, and 637 of them additionally carried a manufactured
# "available today" scrape-date stamp. The 5,427 rows already flagged
# plan-level were already clean and must not move.
#
# The trap this guards: 3,113 plan rows in that same run carry a REAL published
# price. The contract is RENT-BEARING, not flag-bearing — coercing those to
# UNAVAILABLE would destroy real data and is the worst possible outcome.


def test_zero_inventory_plan_row_is_cleanly_unavailable() -> None:
    """No rent + no unit anchor + plan-level -> UNAVAILABLE, and no
    manufactured availability date."""
    for status in ("AVAILABLE", None, "UNKNOWN", ""):
        unit = {
            "floor_plan_name": "1 Bedroom",
            "extraction_tier": "TIER_1_DOM_GENERIC_PLAN_TEXT_PLAN_LEVEL",
            "availability_status": status,
        }
        out = _format_v2_unit(unit, _TS)
        assert out["is_floor_plan_level"] is True, status
        assert out["availability_status"] == "UNAVAILABLE", status
        # The AVAILABLE branch of _resolve_available_date must no longer fire:
        # stamping the scrape date on a plan with no inventory and no price
        # invents a fact about the world.
        assert out["available_date"] is None, status


def test_rent_bearing_plan_row_is_never_coerced_to_unavailable() -> None:
    """A plan row WITH a published price is not zero-inventory.

    Real shape from the 2026-07-27 run: a Squarespace plan card at
    ``rent_low=2967.0`` with an availability date. The property genuinely
    offers that plan at that price.
    """
    out = _format_v2_unit(
        {
            "floor_plan_name": "The Chelsea",
            "extraction_tier": "SYNDICATION_ONLY_SQUARESPACE_PLAN_LEVEL",
            "availability_status": "AVAILABLE",
            "rent_low": 2967.0,
            "rent_high": 2967.0,
            "available_date": "2026-08-15",
        },
        _TS,
    )
    assert out["is_floor_plan_level"] is True
    assert out["availability_status"] == "AVAILABLE"
    assert out["rent_low"] == 2967.0
    assert out["available_date"] == "2026-08-15"


def test_rent_bearing_plan_row_with_no_status_reads_unknown_not_null() -> None:
    """Source silent + row still published -> UNKNOWN is honest; null is not.

    UNKNOWN asserts nothing about the world, unlike AVAILABLE / UNAVAILABLE.
    207 rows on the 2026-07-27 run.
    """
    out = _format_v2_unit(
        {
            "floor_plan_name": "B2",
            "extraction_tier": "TIER_1_API_ENTRATA_SHAPE_REJECTED_PLAN_LEVEL",
            "rent_low": 1850,
        },
        _TS,
    )
    assert out["is_floor_plan_level"] is True
    assert out["availability_status"] == "UNKNOWN"


def test_zero_inventory_contract_does_not_touch_real_units() -> None:
    """A non-plan row passes through untouched, including a null status."""
    out = _format_v2_unit(
        {"floor_plan_name": "A1", "unit_number": "101",
         "extraction_tier": "TIER_1_API_ENTRATA"},
        _TS,
    )
    assert out["is_floor_plan_level"] is False
    assert out["availability_status"] is None


def test_zero_inventory_contract_keeps_a_source_published_date() -> None:
    """Coercing the status must never delete a date the operator published."""
    out = _format_v2_unit(
        {
            "floor_plan_name": "Studio",
            "extraction_tier": "TIER_3_PLAN_TEXT",
            "availability_status": "AVAILABLE",
            "available_date": "2026-09-01",
        },
        _TS,
        property_plan_level=True,
    )
    assert out["availability_status"] == "UNAVAILABLE"
    assert out["available_date"] == "2026-09-01"


def test_already_flagged_plan_rows_do_not_regress() -> None:
    """The 5,427 rows already flagged in run-2026-07-27 were already clean
    (5,425 UNAVAILABLE, 2 UNKNOWN, zero nulls). Pin both shapes."""
    zero_inv = _format_v2_unit(
        {"data_quality_flag": "SIGHTMAP_PLAN_PRESENCE",
         "floor_plan_name": "The Blue Elderberry", "area": 662,
         "availability_status": "UNAVAILABLE"},
        _TS,
    )
    assert zero_inv["availability_status"] == "UNAVAILABLE"
    rent_bearing = _format_v2_unit(
        {"data_quality_flag": "SIGHTMAP_PLAN_PRESENCE",
         "floor_plan_name": "A1", "rent_low": 1495,
         "availability_status": "UNKNOWN", "available_date": "2026-08-02"},
        _TS,
    )
    assert rent_bearing["availability_status"] == "UNKNOWN"
    assert rent_bearing["rent_low"] == 1495.0


def test_floor_plan_wrapper_applies_the_zero_inventory_contract() -> None:
    """``_format_v2_floor_plan`` forces the flag AFTER formatting, so the
    contract has to be re-applied there or the row ships flagged-but-null."""
    out = _format_v2_floor_plan(
        {"floor_plan_name": "A1", "beds": 1, "baths": 1, "area": 700}, _TS, "P1"
    )
    assert out["is_floor_plan_level"] is True
    assert out["availability_status"] == "UNAVAILABLE"
    priced = _format_v2_floor_plan(
        {"floor_plan_name": "A1", "beds": 1, "baths": 1, "area": 700,
         "rent_low": 1500}, _TS, "P1"
    )
    assert priced["availability_status"] == "UNKNOWN"
    assert priced["rent_low"] == 1500.0


def test_resolve_plan_row_availability_table() -> None:
    """Table test WITH must-NOT-match rows. Every row of this table was
    observed as a real bucket in the run-2026-07-27 replay."""
    from ma_poc.core.schema_v2 import resolve_plan_row_availability as _r

    cases: list[tuple[str | None, bool, bool, bool, str | None]] = [
        # (status, plan_level, has_rent, has_anchor, expected)
        # -- must coerce: zero-inventory
        ("AVAILABLE", True, False, False, "UNAVAILABLE"),
        (None, True, False, False, "UNAVAILABLE"),
        ("UNKNOWN", True, False, False, "UNAVAILABLE"),
        ("UNAVAILABLE", True, False, False, "UNAVAILABLE"),
        # Preserve the operator's more specific explicit negative state.
        ("WAITLIST", True, False, False, "WAITLIST"),
        # -- must NOT coerce: rent-bearing plan rows
        ("AVAILABLE", True, True, False, "AVAILABLE"),
        ("UNAVAILABLE", True, True, False, "UNAVAILABLE"),
        ("PENDING", True, True, False, "PENDING"),
        (None, True, True, False, "UNKNOWN"),
        # -- must NOT coerce: a row anchoring ONE REAL APARTMENT
        ("AVAILABLE", True, False, True, "AVAILABLE"),
        (None, True, False, True, "UNKNOWN"),
        # -- must NOT touch: non-plan rows pass straight through
        ("AVAILABLE", False, False, False, "AVAILABLE"),
        (None, False, False, False, None),
        ("LEASED", False, True, True, "LEASED"),
        ("UNKNOWN", False, False, False, "UNKNOWN"),
    ]
    for status, plan, rent, anchor, want in cases:
        got = _r(status, plan_level=plan, has_rent=rent, has_anchor=anchor)
        assert got == want, (status, plan, rent, anchor, got, want)
