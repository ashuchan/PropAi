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
    """F4 placeholder string surfaces under the canonical key."""
    unit = {
        "unit_id": "u101",
        "floor_plan_name": "A1",
        "beds": 1,
        "area": 750,
        "rent_low": 1500,
        "_date_placeholder": "Spring 2026",
    }
    out = _format_v2_unit(unit, _TS)
    assert out["_date_placeholder"] == "Spring 2026"
    # available_date should be None when only the placeholder is present
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
    case stays unchanged)."""
    unit = {"unit_id": "u-no-status", "floor_plan_name": "A1"}
    out = _format_v2_unit(unit, _TS)
    assert out["available_date"] is None
    assert out["availability_status"] is None


@pytest.mark.parametrize("text", [
    "ready",
    "Move-in Ready",
    "MOVE IN READY",
    "vacant",
    "Available Immediately",
    "Available Today",
    "TBA",
    "TBD",
    "to be announced",
])
def test_format_date_widened_text_recognizer(text: str) -> None:
    """The text-recognizer also widened: operator-specific phrasings
    that mean 'available now' now resolve to scrape date instead of
    None. Catches Mark-Taylor 'vacant', RentCafe 'TBA', etc."""
    from datetime import UTC, datetime

    from ma_poc.core.schema_v2 import _format_date
    out = _format_date(text)
    assert out == datetime.now(UTC).strftime("%Y-%m-%d"), (
        f"{text!r} should resolve to today; got {out!r}"
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
