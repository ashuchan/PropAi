"""apts247 direct-JSON adapter tests (2026-05-23).

Pins the apts247 (Yardi small-property tenant) extractor:
  • detect on ``static2.apts247.info`` / ``apts247_api`` markers
  • pull the 40-char hex ``api_key`` from inline JS
  • build the ``/api/v1/floorplans/`` URL on the SAME host
  • walk the response, emitting one row per unit (rent + sqft +
    availability date inherited from per-unit data, bed/bath inherited
    from the floor plan)

Background: foxrundothan.com (Vergence Multifamily portfolio) returned
zero rent strings in static HTML — Chrome MCP probe revealed the
``/api/v1/floorplans/`` endpoint with full unit-level data (3 plans, 9
units, rent + sqft + available_date on every unit). Same pattern across
the apts247 cohort.

Fixture: ``ma_poc/tests/fixtures/apts247/foxrun_floorplans.json``
(live response pulled 2026-05-23).
"""
from __future__ import annotations

import json
from pathlib import Path

from ma_poc.pms.adapters._apts247 import (
    build_floorplans_url,
    detect_apts247,
    extract_api_key,
    parse_apts247_floorplans,
)

_FIXTURE = Path("ma_poc/tests/fixtures/apts247/foxrun_floorplans.json")


def _read_fixture() -> dict:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


# ─── detector ────────────────────────────────────────────────────────


def test_detect_matches_static2_apts247() -> None:
    html = '<script src="https://static2.apts247.info/js/apartments247_api.min.js"></script>'
    assert detect_apts247(html) is True


def test_detect_matches_media_apts247() -> None:
    html = '<img src="https://media.apts247.info/abc/logo/community/logo.png">'
    assert detect_apts247(html) is True


def test_detect_matches_apts247_api_bundle_name() -> None:
    html = '<script>var apts247_api = {};</script>'
    assert detect_apts247(html) is True


def test_detect_rejects_unrelated_html() -> None:
    assert detect_apts247("<html><body>plain page</body></html>") is False
    assert detect_apts247("") is False


# ─── api_key extraction ──────────────────────────────────────────────


def test_extract_api_key_finds_double_quoted_assignment() -> None:
    html = (
        '<script>window.foo = 1; '
        'api_key = "aae294c5e900e74edc21e0ce97aa1e332d2b449f"; </script>'
    )
    assert (
        extract_api_key(html)
        == "aae294c5e900e74edc21e0ce97aa1e332d2b449f"
    )


def test_extract_api_key_finds_single_quoted_assignment() -> None:
    html = "<script>api_key = 'aae294c5e900e74edc21e0ce97aa1e332d2b449f';</script>"
    assert (
        extract_api_key(html)
        == "aae294c5e900e74edc21e0ce97aa1e332d2b449f"
    )


def test_extract_api_key_tolerates_whitespace_around_equals() -> None:
    html = '<script>api_key   =   "aae294c5e900e74edc21e0ce97aa1e332d2b449f";</script>'
    assert (
        extract_api_key(html)
        == "aae294c5e900e74edc21e0ce97aa1e332d2b449f"
    )


def test_extract_api_key_returns_none_when_missing() -> None:
    assert extract_api_key("<html>no key</html>") is None
    assert extract_api_key("") is None


def test_extract_api_key_rejects_non_40_hex_strings() -> None:
    """An ``api_key`` of the wrong length is suspicious — guard against
    accidental collisions with other hex-like assignments (UUIDs etc.)."""
    html = '<script>api_key = "shortkey";</script>'
    assert extract_api_key(html) is None


# ─── build_floorplans_url ────────────────────────────────────────────


def test_build_url_uses_same_host_as_page_url() -> None:
    url = build_floorplans_url(
        "https://www.foxrundothan.com/floorplans/",
        "aae294c5e900e74edc21e0ce97aa1e332d2b449f",
    )
    assert (
        url
        == "https://www.foxrundothan.com/api/v1/floorplans/"
        "?api_key=aae294c5e900e74edc21e0ce97aa1e332d2b449f"
    )


def test_build_url_returns_none_for_empty_inputs() -> None:
    assert build_floorplans_url("", "abc") is None
    assert build_floorplans_url("https://x.com/", "") is None


def test_build_url_returns_none_for_malformed_page_url() -> None:
    assert build_floorplans_url("not-a-url", "abc") is None


# ─── parser: live fixture ────────────────────────────────────────────


def test_parse_live_fixture_emits_unit_level_rows() -> None:
    """The Fox Run fixture has 3 floor plans, each with 3 units. We
    must emit a unit-level row for every unit that has both rent and
    sqft (per-unit data is canonical when present)."""
    rows = parse_apts247_floorplans(
        _read_fixture(),
        "https://www.foxrundothan.com/api/v1/floorplans/?api_key=X",
    )
    # 3 plans × 3 units (every fixture unit has rent + sqft)
    assert len(rows) == 9


def test_parse_live_fixture_rows_carry_rent_and_sqft() -> None:
    rows = parse_apts247_floorplans(
        _read_fixture(),
        "https://www.foxrundothan.com/api/v1/floorplans/?api_key=X",
    )
    for r in rows:
        assert r["market_rent_low"] > 0
        assert int(r["sqft"]) > 0


def test_parse_live_fixture_includes_peanut_unit_172() -> None:
    """Sanity check on the canonical Peanut-R unit (the one whose
    fragment is the user's example: ``#peanut-r/172``)."""
    rows = parse_apts247_floorplans(
        _read_fixture(),
        "https://www.foxrundothan.com/api/v1/floorplans/?api_key=X",
    )
    peanut_172 = [
        r for r in rows if r["unit_number"] == "172" and r["floor_plan_name"] == "Peanut-R"
    ]
    assert len(peanut_172) == 1
    u = peanut_172[0]
    assert u["sqft"] == "693"
    assert u["market_rent_low"] == 915
    assert u["bedrooms"] == "1"
    assert u["bathrooms"] == "1"
    assert u["availability_date"] == "2025-09-26"


def test_parse_live_fixture_inherits_plan_metadata() -> None:
    """Every unit gets bedrooms/bathrooms from its plan, even when the
    unit-level dict doesn't carry them."""
    rows = parse_apts247_floorplans(_read_fixture(), "https://x/api")
    azalea = [r for r in rows if r["floor_plan_name"] == "Azalea-R"]
    assert azalea
    for u in azalea:
        assert u["bedrooms"] == "2"
        assert u["bathrooms"] == "1.5"
        assert u["sqft"] == "921"


def test_parse_live_fixture_extraction_tier_is_marked() -> None:
    rows = parse_apts247_floorplans(_read_fixture(), "https://x/api")
    assert rows
    assert (
        rows[0]["extraction_tier"]
        == "TIER_1_API_APTS247_FLOORPLANS"
    )


# ─── parser: edge cases ──────────────────────────────────────────────


def test_parse_returns_empty_for_non_apts247_envelope() -> None:
    assert parse_apts247_floorplans({}, "") == []
    assert parse_apts247_floorplans({"results": []}, "") == []
    assert parse_apts247_floorplans({"objects": "not a list"}, "") == []
    assert parse_apts247_floorplans(None, "") == []


def test_parse_skips_units_missing_per_unit_rent() -> None:
    """Per-unit rent is canonical when present — and when ABSENT we
    don't substitute the plan-level starting rent (that would imply
    knowledge we don't have). Units without rent are skipped."""
    body = {
        "objects": [
            {
                "id": 1, "name": "P1", "bed": 1, "bath": 1.0, "sq_ft": 500,
                "rent": "$1000",
                "units": [
                    {"id": 10, "number": "101", "rent": "$1000", "sq_ft": 500},
                    {"id": 11, "number": "102", "rent": None, "sq_ft": 500},
                ],
            }
        ]
    }
    rows = parse_apts247_floorplans(body, "")
    assert len(rows) == 1
    assert rows[0]["unit_number"] == "101"


def test_parse_falls_back_to_plan_sqft_when_unit_sqft_missing() -> None:
    """Per-unit sqft falls back to plan sqft — apts247 floor plans
    have the same sqft for every unit of that plan, so the fallback
    is safe and matches operator intent."""
    body = {
        "objects": [
            {
                "id": 1, "name": "P1", "bed": 1, "bath": 1.0, "sq_ft": 500,
                "rent": "$1000",
                "units": [
                    {"id": 12, "number": "103", "rent": "$1000", "sq_ft": None},
                ],
            }
        ]
    }
    rows = parse_apts247_floorplans(body, "")
    assert len(rows) == 1
    assert rows[0]["sqft"] == "500"


def test_parse_emits_plan_level_row_when_no_units_array() -> None:
    """Empty units[] → plan-level placeholder with PLAN_LEVEL_NO_VACANT_UNIT
    flag so the property still clears the success bar honestly."""
    body = {
        "objects": [
            {
                "id": 1, "name": "P1", "bed": 2, "bath": 2.0, "sq_ft": 900,
                "rent": "$1500", "units": [],
            }
        ]
    }
    rows = parse_apts247_floorplans(body, "")
    assert len(rows) == 1
    r = rows[0]
    assert r["data_quality_flag"] == "PLAN_LEVEL_NO_VACANT_UNIT"
    assert r["market_rent_low"] == 1500
    assert r["sqft"] == "900"
    assert r["availability_status"] == "UNAVAILABLE"
    assert "unit_number" in r["data_gaps"]


def test_parse_handles_studio_plans() -> None:
    """bed=0 must produce ``bed_label="Studio"`` not ``"0 Bed"``."""
    body = {
        "objects": [
            {
                "id": 1, "name": "Studio-A", "bed": 0, "bath": 1.0,
                "sq_ft": 400, "rent": "$1100",
                "units": [{"id": 10, "number": "S1", "rent": "$1100", "sq_ft": 400}],
            }
        ]
    }
    rows = parse_apts247_floorplans(body, "")
    assert len(rows) == 1
    assert rows[0]["bed_label"] == "Studio"
    assert rows[0]["bedrooms"] == "0"


def test_parse_preserves_half_bath_as_decimal_string() -> None:
    body = {
        "objects": [
            {
                "id": 1, "name": "P", "bed": 2, "bath": 1.5, "sq_ft": 800,
                "rent": "$1200",
                "units": [{"id": 10, "number": "1", "rent": "$1200", "sq_ft": 800}],
            }
        ]
    }
    rows = parse_apts247_floorplans(body, "")
    assert rows[0]["bathrooms"] == "1.5"


def test_parse_drops_cents_from_money_strings() -> None:
    """apts247 rent strings include ``.50`` cents — our schema rounds
    to whole-dollar ints."""
    body = {
        "objects": [
            {
                "id": 1, "name": "P", "bed": 1, "bath": 1.0, "sq_ft": 500,
                "rent": "$954.50",
                "units": [
                    {"id": 10, "number": "1", "rent": "$954.50", "sq_ft": 500}
                ],
            }
        ]
    }
    rows = parse_apts247_floorplans(body, "")
    assert rows[0]["market_rent_low"] == 954


def test_parse_attaches_apts247_source_ids() -> None:
    """Per-unit + per-plan source IDs surfaced so downstream profile
    learning can persist them."""
    rows = parse_apts247_floorplans(_read_fixture(), "https://x/api")
    assert rows
    sid = rows[0]["source_ids"]
    assert "apts247_floor_plan_id" in sid
    assert "apts247_unit_id" in sid
    assert "apts247_slug" in sid
