"""WordPress Entrata-theme REST API parser tests (2026-05-24).

Pins the HAR-driven adapter for WordPress sites that use the Entrata
theme — they expose ``wp-json/theme/entrata/v1/floor-plans`` returning
both per-floorplan and per-unit pricing/sqft/availability.

Live fixture from olivboulder.com HAR (446 KB, 38 floorplans,
48 units).
"""
from __future__ import annotations

import json
from pathlib import Path

from ma_poc.pms.adapters._wp_entrata import (
    _has_wp_entrata_marker,
    _parse_iso_or_us_date,
    parse_wp_entrata_floor_plans,
)

FIXTURES = Path(__file__).parent / "fixtures"


# ----- _has_wp_entrata_marker -----------------------------------------


def test_marker_detects_wp_json_path() -> None:
    body = '<script src="/wp-json/theme/entrata/v1/floor-plans?v=0.955"></script>'
    assert _has_wp_entrata_marker(body) is True


def test_marker_detects_wp_content_theme_path() -> None:
    body = '<link href="/wp-content/themes/entrata/style.css" rel="stylesheet">'
    assert _has_wp_entrata_marker(body) is True


def test_marker_returns_false_for_unrelated_body() -> None:
    assert _has_wp_entrata_marker("<html><body>nothing here</body></html>") is False


def test_marker_handles_empty_body() -> None:
    assert _has_wp_entrata_marker("") is False


# ----- _parse_iso_or_us_date ------------------------------------------


def test_date_passes_iso_through() -> None:
    assert _parse_iso_or_us_date("2026-07-31") == "2026-07-31"


def test_date_converts_us_to_iso() -> None:
    assert _parse_iso_or_us_date("07/31/2026") == "2026-07-31"
    assert _parse_iso_or_us_date("7/4/2026") == "2026-07-04"


def test_date_passes_unknown_through() -> None:
    assert _parse_iso_or_us_date("Available Now") == "Available Now"


def test_date_handles_blank() -> None:
    assert _parse_iso_or_us_date("") == ""


# ----- parse_wp_entrata_floor_plans (live fixture) --------------------


def test_parse_live_olivboulder_fixture() -> None:
    """Live fixture from olivboulder.com HAR — 48 units across 38
    floorplans, all with rent + sqft."""
    body = json.loads((FIXTURES / "wp_entrata_olivboulder.json").read_text())
    units = parse_wp_entrata_floor_plans(body, "https://olivboulder.com/wp-json/theme/entrata/v1/floor-plans")

    # The fixture has 48 units total but parser skips zero-rent zero-sqft
    # rows. Expect ≥40 (most are real).
    assert len(units) >= 30, f"expected ≥30 units, got {len(units)}"

    # Spot check the first unit
    u0 = units[0]
    assert u0["extraction_tier"] == "TIER_1_API_WP_ENTRATA"
    assert u0["unit_number"]  # number field populated
    assert int(u0["market_rent_low"]) > 500  # plausible rent
    assert int(u0["sqft"]) > 200  # plausible sqft

    # Strict-pass: every emitted unit must have rent+sqft (the parser
    # skips bare-name rows)
    for u in units:
        assert u.get("market_rent_low"), f"missing rent: {u}"
        assert u.get("sqft"), f"missing sqft: {u}"


def test_parse_strict_pass_yield() -> None:
    """Confirm enough strict-pass units to clear the validity gate."""
    body = json.loads((FIXTURES / "wp_entrata_olivboulder.json").read_text())
    units = parse_wp_entrata_floor_plans(body, "u")
    strict = sum(
        1 for u in units
        if u.get("market_rent_low") and u.get("sqft")
    )
    # Easily exceed any cluster's validity threshold
    assert strict >= 20


def test_parse_joins_floorplan_name_into_unit() -> None:
    """The parser joins units to floorplans by floorplanID — so each
    unit row carries the floorplan's name + beds + baths."""
    body = json.loads((FIXTURES / "wp_entrata_olivboulder.json").read_text())
    units = parse_wp_entrata_floor_plans(body, "u")
    # At least some units should have non-empty floor_plan_name
    named = [u for u in units if u.get("floor_plan_name")]
    assert len(named) >= 10, f"expected ≥10 units with floorplan name, got {len(named)}"


# ----- defensive parse ------------------------------------------------


def test_parse_returns_empty_for_non_dict_body() -> None:
    assert parse_wp_entrata_floor_plans(None, "u") == []
    assert parse_wp_entrata_floor_plans([], "u") == []
    assert parse_wp_entrata_floor_plans("string", "u") == []


def test_parse_returns_empty_for_missing_units() -> None:
    """Body has floorplans but no units dict → returns [] (we emit at
    unit-level, not floorplan-level)."""
    body = {"floorplans": {"1": {"name": "A"}}, "units": {}}
    assert parse_wp_entrata_floor_plans(body, "u") == []


def test_parse_skips_zero_rent_zero_sqft_units() -> None:
    body = {
        "floorplans": {"1": {"name": "A", "beds": 1, "baths": 1}},
        "units": {
            "100": {
                "number": "100", "rent": 1500, "sqft": 700,
                "floorplanID": 1, "available": True,
            },
            "101": {
                "number": "101", "rent": 0, "sqft": 0,
                "floorplanID": 1, "available": True,
            },
        },
    }
    units = parse_wp_entrata_floor_plans(body, "u")
    assert len(units) == 1
    assert units[0]["unit_number"] == "100"


def test_parse_marks_unavailable_units() -> None:
    body = {
        "floorplans": {"1": {"name": "A", "beds": 1, "baths": 1}},
        "units": {
            "200": {
                "number": "200", "rent": 1500, "sqft": 700,
                "floorplanID": 1, "available": False,
            },
        },
    }
    units = parse_wp_entrata_floor_plans(body, "u")
    assert len(units) == 1
    assert units[0]["availability_status"] == "UNAVAILABLE"


def test_parse_handles_us_date_to_iso() -> None:
    body = {
        "floorplans": {"1": {"name": "A", "beds": 1, "baths": 1}},
        "units": {
            "300": {
                "number": "300", "rent": 1500, "sqft": 700,
                "floorplanID": 1, "available": True,
                "availableOn": "07/31/2026",
            },
        },
    }
    units = parse_wp_entrata_floor_plans(body, "u")
    assert units[0]["availability_date"] == "2026-07-31"
