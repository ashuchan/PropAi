"""F10: schema_v2 _format_v2_unit pass-through for concessions, amenities,
and validation provenance flags. H16, H17 invariants."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ma_poc.core.schema_v2 import _format_v2_unit, _normalize_amenities

_TS = datetime(2026, 5, 5, 12, 0, 0, tzinfo=UTC)


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


def test_rent_present_status_unavailable_defaults_to_scrape_date() -> None:
    """Canary 1ef1060 signature case: Knock-style unit with rent +
    sqft + plan + unit_number, but status=UNAVAILABLE and no parseable
    date. Pre-fix → available_date=None. Post-fix → scrape date.
    """
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
    assert out["available_date"] == _TS.strftime("%Y-%m-%d")
    # Status is preserved as-is — we don't rewrite it, just fix the date.
    assert out["availability_status"] == "UNAVAILABLE"
    # All 5 core fields populated → row is now "full"
    assert out["unit_id"] == "u-knock-001"
    assert out["rent_low"] == 1495.0
    assert out["area"] == 750
    assert out["floor_plan_name"] == "A1"
    assert out["available_date"] == _TS.strftime("%Y-%m-%d")


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
