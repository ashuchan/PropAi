"""Apts247 / RentDynamics adapter — parser + detector wiring tests.

Acceptance (canary iter-15):
- ``/api/v1/floorplans/`` envelope → one unit-level row per nested unit,
  real unit ``id``-backed (NOT inferred), concrete rent.
- ``"$1,225"`` / ``"$899"`` → int; ``"Call for details."`` → falls back
  to the parent floorplan rent (still unit-level, never None-drops).
- ``"Studio"`` → beds ``"0"``; ``"1 Bed"`` → ``"1"``.
- A plan with no available units but a rent → one plan-level fallback row.
- Detector routes ``apts247`` HTML marker → pms="apts247".
- ``api_key`` recoverable from raw homepage HTML (no browser).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import ma_poc.pms.adapters  # noqa: F401  # populate adapter registry
from ma_poc.pms.adapters.apts247 import (
    Apts247Adapter,
    _beds_from_label,
    _rent_to_int,
    find_apts247_api_key,
    parse_apts247_floorplans,
)
from ma_poc.pms.adapters.registry import get_adapter
from ma_poc.pms.detector import _detect_html_markers
from ma_poc.scripts.runners.jugnu import (
    _emit_v2_units_for_property,
    _format_v2_unit,
)

FIXTURES = Path(__file__).parent / "fixtures" / "apts247"


def _load() -> dict:
    return json.loads((FIXTURES / "floorplans.json").read_text(encoding="utf-8"))


def test_rent_to_int_handles_money_and_sentinels() -> None:
    assert _rent_to_int("$899") == 899
    assert _rent_to_int("$1,225") == 1225
    assert _rent_to_int("Call for details.") is None
    assert _rent_to_int("") is None
    assert _rent_to_int(None) is None


def test_beds_from_label() -> None:
    assert _beds_from_label("Studio") == "0"
    assert _beds_from_label("1 Bed") == "1"
    assert _beds_from_label("2 Bedroom") == "2"
    assert _beds_from_label("") == ""


def test_find_api_key_from_raw_html() -> None:
    html = (
        '<script>var cfg={"api_key":"6f920abb01b4b0bf45f3e11f418e7a21bac03625"};'
        "fetch('/api/v1/communitypromotion/?api_key=6f920abb01b4b0bf45f3e11f418e7a21bac03625')</script>"
    )
    assert find_apts247_api_key(html) == "6f920abb01b4b0bf45f3e11f418e7a21bac03625"
    assert find_apts247_api_key("<html>no key here</html>") is None
    assert find_apts247_api_key("") is None


def test_parse_emits_unit_level_rows() -> None:
    rows = parse_apts247_floorplans(_load(), "https://x.com/api/v1/floorplans/?api_key=k")
    # 1 (studio) + 2 (A1 units) + 1 (B2 plan-level fallback) = 4
    assert len(rows) == 4

    studio = next(r for r in rows if r["floor_plan_name"] == "S1")
    assert studio["market_rent_low"] == 899
    assert studio["bedrooms"] == "0"
    assert studio["availability_status"] == "AVAILABLE"
    assert studio["availability_date"] == "2025-12-02"
    # Regression: blank API ``number`` must fall back to the real unit
    # ``id`` so the row keeps a natural identity (not an inferred_ id)
    # and is admitted as true unit-level, not demoted to floorplan.
    assert studio["unit_number"] == "apt-1622720"
    assert studio["unit_id"] == "1622720"
    assert studio["source_ids"] == {
        "apts247_floor_plan_id": "56703",
        "apts247_slug": "s1-98",
        "apts247_unit_id": "1622720",
    }

    a1_units = [r for r in rows if r["floor_plan_name"] == "A1"]
    assert {u["unit_number"] for u in a1_units} == {"204", "311"}
    u204 = next(u for u in a1_units if u["unit_number"] == "204")
    assert u204["market_rent_low"] == 1225
    assert u204["bedrooms"] == "1"
    assert u204["floor"] == "2"
    assert u204["unit_id"] == "1622800"
    assert u204["unit_name"] == "204"
    # "Call for details." unit falls back to the floorplan rent ($1,199)
    u311 = next(u for u in a1_units if u["unit_number"] == "311")
    assert u311["market_rent_low"] == 1199

    # Plan with no units but a rent → one plan-level fallback row.
    b2 = [r for r in rows if r["floor_plan_name"] == "B2"]
    assert len(b2) == 1
    assert b2[0]["market_rent_low"] == 1650
    assert b2[0]["availability_status"] == "UNKNOWN"


def test_parse_rejects_non_envelope() -> None:
    assert parse_apts247_floorplans({}, "u") == []
    assert parse_apts247_floorplans({"objects": "nope"}, "u") == []


def test_native_id_prevents_same_number_cross_building_collision() -> None:
    body = {
        "objects": [
            {
                "id": 700,
                "slug": "a1",
                "name": "A1",
                "display_bed": "1 Bed",
                "bath": 1,
                "sq_ft": 700,
                "units": [
                    {
                        "id": 829515,
                        "number": "523",
                        "building": "North",
                        "rent": "$1,900",
                        "available_date": "2026-08-10",
                    },
                    {
                        "id": 767763,
                        "number": "523",
                        "building": "South",
                        "rent": "$1,900",
                        "available_date": "2026-08-10",
                    },
                ],
            }
        ]
    }
    parsed = parse_apts247_floorplans(body, "https://example/api/v1/floorplans/")
    final = _emit_v2_units_for_property(
        [_format_v2_unit(row, datetime(2026, 8, 2, 12, 0), "64390") for row in parsed]
    )

    assert [row["unit_number"] for row in parsed] == ["523", "523"]
    assert {row["building"] for row in parsed} == {"North", "South"}
    assert {row["unit_id"] for row in final} == {"829515", "767763"}
    assert {row["source_ids"]["apts247_unit_id"] for row in final} == {
        "829515",
        "767763",
    }


def test_detector_routes_apts247_html_marker() -> None:
    html = (
        "<html><script src='https://static2.apts247.info/widget.js'></script>"
        "<script>var c={api_key:'deadbeefdeadbeefdeadbeefdeadbeefdeadbeef'}</script></html>"
    ).lower()
    res = _detect_html_markers(html)
    assert res is not None
    assert res[0] == "apts247"


def test_adapter_registered() -> None:
    a = get_adapter("apts247")
    assert isinstance(a, Apts247Adapter)
    assert a.pms_name == "apts247"
    assert a.matches_response_body("...static2.apts247.info...") is True
    assert a.matches_response_body("nothing here") is False
