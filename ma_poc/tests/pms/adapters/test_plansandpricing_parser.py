"""Plansandpricing AJAX parser tests.

Live-capture fixture is the Hazel at National Landing (PID 264589)
response trimmed to 3 floor-plan rows. The third row carries a
synthesised multi-cohort ``UnitsDatesAvailable`` so the row-expansion
path is also exercised.
"""

from __future__ import annotations

import json
from pathlib import Path

from ma_poc.pms.adapters._plansandpricing_parser import (
    _norm_date,
    _parse_units_dates_available,
    is_plansandpricing_url,
    parse_plansandpricing_units,
    try_parse_plansandpricing,
)

FIXTURES = Path(__file__).parent / "fixtures" / "plansandpricing"


def _load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# URL gate (path-based, host-agnostic)
# ---------------------------------------------------------------------------


def test_is_plansandpricing_url_matches_hazel() -> None:
    assert is_plansandpricing_url(
        "https://www.livehazelnationallanding.com/ajax/api/plansandpricing/"
    )


def test_is_plansandpricing_url_matches_other_host_same_path() -> None:
    # Gate is path-based on purpose — many custom WP sites use this same
    # AJAX route. Any property hitting /ajax/api/plansandpricing/ gets
    # the deterministic parser regardless of host.
    assert is_plansandpricing_url(
        "https://www.someotherapts.com/ajax/api/plansandpricing/"
    )


def test_is_plansandpricing_url_rejects_unrelated_route() -> None:
    assert not is_plansandpricing_url("https://www.example.com/api/floorplans/")
    assert not is_plansandpricing_url("")


# ---------------------------------------------------------------------------
# Date normalisation
# ---------------------------------------------------------------------------


def test_norm_date_us_dash_to_iso() -> None:
    assert _norm_date("07-01-2026") == "2026-07-01"


def test_norm_date_us_slash_to_iso() -> None:
    assert _norm_date("07/01/2026") == "2026-07-01"


def test_norm_date_already_iso_passes_through() -> None:
    assert _norm_date("2026-07-01") == "2026-07-01"


def test_norm_date_empty_returns_empty() -> None:
    assert _norm_date("") == ""
    assert _norm_date("   ") == ""


def test_norm_date_unparseable_returns_original() -> None:
    assert _norm_date("invalid-string") == "invalid-string"


# ---------------------------------------------------------------------------
# UnitsDatesAvailable decoder
# ---------------------------------------------------------------------------


def test_parse_units_dates_available_single_cohort() -> None:
    assert _parse_units_dates_available("1|07/01/2026") == [(1, "2026-07-01")]


def test_parse_units_dates_available_multi_cohort() -> None:
    decoded = _parse_units_dates_available("2|07-01-2026;3|08-15-2026")
    assert decoded == [(2, "2026-07-01"), (3, "2026-08-15")]


def test_parse_units_dates_available_skips_malformed_parts() -> None:
    decoded = _parse_units_dates_available("1|07/01/2026;garbage;|;2|08-15-2026")
    assert decoded == [(1, "2026-07-01"), (2, "2026-08-15")]


def test_parse_units_dates_available_empty_inputs() -> None:
    assert _parse_units_dates_available("") == []
    assert _parse_units_dates_available(None) == []


# ---------------------------------------------------------------------------
# Body-shape gate
# ---------------------------------------------------------------------------


def test_try_parse_plansandpricing_rejects_non_matching_url() -> None:
    units, matched = try_parse_plansandpricing(
        {"url": "https://example.com/api/x", "body": _load("hazel_capture.json")}
    )
    assert not matched
    assert units == []


def test_try_parse_plansandpricing_rejects_unrelated_list_at_root() -> None:
    units, matched = try_parse_plansandpricing(
        {
            "url": "https://example.com/ajax/api/plansandpricing/",
            "body": [{"some_unrelated_key": "value"}],
        }
    )
    assert not matched
    assert units == []


# ---------------------------------------------------------------------------
# Field projection
# ---------------------------------------------------------------------------


def test_parse_plansandpricing_extracts_first_row_fields() -> None:
    body = _load("hazel_capture.json")
    units = parse_plansandpricing_units(body, "https://example.com/ajax/api/plansandpricing/")
    # First fixture row: S1, sqft 521, UnitMinPrice 2210, single date.
    first = units[0]
    assert first["floor_plan_name"] == "S1"
    assert first["bedrooms"] == "0"
    assert first["bathrooms"] == "1"
    assert first["sqft"] == "521"
    assert first["market_rent_low"] == 2210
    assert first["market_rent_high"] == 2210
    assert first["available_units"] == "1"
    assert first["availability_date"] == "2026-07-01"
    assert first["availability_status"] == "AVAILABLE"
    assert first["lease_term"] == "12"
    assert first["extraction_tier"] == "TIER_1_API_PLANSANDPRICING"


def test_parse_plansandpricing_expands_multi_cohort_into_separate_rows() -> None:
    body = _load("hazel_capture.json")
    units = parse_plansandpricing_units(body, "")
    # Row index 2 (S4) has UnitsDatesAvailable="2|07-01-2026;3|08-15-2026".
    # Expansion → 2 rows for S4 with cohort-specific counts and dates.
    s4_rows = [u for u in units if u["floor_plan_name"] == "S4"]
    assert len(s4_rows) == 2
    counts = sorted(u["available_units"] for u in s4_rows)
    dates = sorted(u["availability_date"] for u in s4_rows)
    assert counts == ["2", "3"]
    assert dates == ["2026-07-01", "2026-08-15"]


def test_parse_plansandpricing_total_unit_count_after_expansion() -> None:
    body = _load("hazel_capture.json")
    units = parse_plansandpricing_units(body, "")
    # 2 single-cohort rows (S1, S3) + 2 expanded rows for S4 = 4 emitted rows.
    assert len(units) == 4


def test_parse_plansandpricing_unavailable_plan_emits_unavailable_status() -> None:
    body = [
        {
            "FloorPlanName": "Phantom",
            "Bedrooms": 1.0,
            "Bathrooms": 1.0,
            "MinSqFt": 700,
            "MaxSqFt": 700,
            "MinPrice": 1500,
            "MaxPrice": 1500,
            "UnitMinPrice": 0,
            "UnitMaxPrice": 0,
            "UnitsAvailable": 0,
            "Available": False,
            "EarliestUnitAvailable": "",
            "UnitsDatesAvailable": "",
            "SpecialsDescription": None,
            "LeaseTerm": 0,
        }
    ]
    units = parse_plansandpricing_units(body, "")
    assert len(units) == 1
    # Available=False → status maps to UNAVAILABLE so the row still ships
    # with pricing data for not-currently-leasable plans.
    assert units[0]["availability_status"] == "UNAVAILABLE"


def test_parse_plansandpricing_synthesises_off_baseline_concession() -> None:
    # MinPrice=2700 > UnitMinPrice=2500 → we synthesise "$200 off baseline"
    # since SpecialsDescription is null. This is the case where the LLM
    # had to guess; we surface a deterministic diff instead.
    body = [
        {
            "FloorPlanName": "DiscountedPlan",
            "Bedrooms": 1.0,
            "Bathrooms": 1.0,
            "MinSqFt": 700,
            "MaxSqFt": 700,
            "MinPrice": 2700,
            "MaxPrice": 2700,
            "UnitMinPrice": 2500,
            "UnitMaxPrice": 2500,
            "UnitsAvailable": 1,
            "Available": True,
            "EarliestUnitAvailable": "06-01-2026",
            "UnitsDatesAvailable": "1|06-01-2026",
            "SpecialsDescription": None,
            "LeaseTerm": 12,
        }
    ]
    units = parse_plansandpricing_units(body, "")
    assert units[0]["concession"] == "$200 off baseline"
    # Rent surfaces the effective per-unit value, not the market list.
    assert units[0]["market_rent_low"] == 2500


def test_parse_plansandpricing_uses_specials_description_when_present() -> None:
    body = [
        {
            "FloorPlanName": "WithSpecial",
            "Bedrooms": 1.0,
            "Bathrooms": 1.0,
            "MinSqFt": 700,
            "MaxSqFt": 700,
            "MinPrice": 2700,
            "MaxPrice": 2700,
            "UnitMinPrice": 2500,
            "UnitMaxPrice": 2500,
            "UnitsAvailable": 1,
            "Available": True,
            "EarliestUnitAvailable": "06-01-2026",
            "UnitsDatesAvailable": "1|06-01-2026",
            "SpecialsDescription": "1 month free on 13-month lease",
            "LeaseTerm": 12,
        }
    ]
    units = parse_plansandpricing_units(body, "")
    # Free-text description wins over synthesised "$X off baseline".
    assert units[0]["concession"] == "1 month free on 13-month lease"


def test_parse_plansandpricing_sqft_range_when_min_neq_max() -> None:
    body = [
        {
            "FloorPlanName": "RangePlan",
            "Bedrooms": 2.0,
            "Bathrooms": 1.5,
            "MinSqFt": 900,
            "MaxSqFt": 1000,
            "MinPrice": 3000,
            "MaxPrice": 3500,
            "UnitMinPrice": 3000,
            "UnitMaxPrice": 3500,
            "UnitsAvailable": 3,
            "Available": True,
            "EarliestUnitAvailable": "06-01-2026",
            "UnitsDatesAvailable": "3|06-01-2026",
            "SpecialsDescription": None,
            "LeaseTerm": 12,
        }
    ]
    units = parse_plansandpricing_units(body, "")
    assert units[0]["sqft"] == "900-1000"
    # Bathrooms=1.5 — preserve the decimal, don't truncate.
    assert units[0]["bathrooms"] == "1.5"


def test_try_parse_plansandpricing_round_trip() -> None:
    resp = {
        "url": "https://www.livehazelnationallanding.com/ajax/api/plansandpricing/",
        "body": _load("hazel_capture.json"),
    }
    units, matched = try_parse_plansandpricing(resp)
    assert matched
    assert len(units) == 4  # 2 + 2 (S4 expansion)
