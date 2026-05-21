"""Aspen Square Management adapter (2026-05-19, greenfield).

Card+unit data captured live from
aspensquare.com/apartments/massachusetts/westfield/southwood-acres
(community) and .../floor-plans/the-woodhaven (unit drill).
"""

from __future__ import annotations

import pytest

from ma_poc.pms.adapters import get_adapter
from ma_poc.pms.adapters.aspensquare import (
    AspenSquareAdapter,
    parse_aspensquare_cards,
)
from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.detector import detect_pms

# Plan w/ a drilled unit list (Woodhaven on Southwood Acres). The 3 unit
# rows below were copied byte-for-byte from the live drill page.
_PLAN_WITH_UNITS = {
    "name": "The Woodhaven",
    "specs": "1 bed1 bath725 sq ft",
    "planRent": "Starting at $2,023",
    "badge": "3 Available",
    "drillPath": "/apartments/massachusetts/westfield/southwood-acres/floor-plans/the-woodhaven",
    "units": [
        {"unit": "13 - 115", "rent": "$2,043", "avail": "Available Now"},
        {"unit": "10 - 63", "rent": "$2,043", "avail": "06/06/2026"},
        {"unit": "10 - 57", "rent": "$2,023", "avail": "08/07/2026"},
    ],
}
# Plan with no drilled units (Maplewood = "Call For Pricing"/Limited Availability).
_PLAN_LIMITED = {
    "name": "The Maplewood",
    "specs": "1 bed1 bath425 sq ft",
    "planRent": "Call For Pricing",
    "badge": "Limited Availability",
    "drillPath": "/apartments/massachusetts/westfield/southwood-acres/floor-plans/the-maplewood",
    "units": [],
}


class _FakePage:
    def __init__(self, cards: object, url: str = "https://www.aspensquare.com/apartments/massachusetts/westfield/southwood-acres") -> None:
        self._cards = cards
        self.url = url

    async def evaluate(self, _js: str, *_a: object) -> object:
        return self._cards


def _ctx(base_url: str = "https://www.aspensquare.com/apartments/massachusetts/westfield/southwood-acres") -> AdapterContext:
    return AdapterContext(
        base_url=base_url,
        detected=detect_pms(base_url),
        profile=None,
        expected_total_units=None,
        property_id="P_TEST",
    )


def test_parse_woodhaven_unit_level() -> None:
    units = parse_aspensquare_cards([_PLAN_WITH_UNITS], "u")
    # 3 unit-level rows expected (no plan-level row when units present)
    assert len(units) == 3
    u0 = units[0]
    assert u0["floor_plan_name"] == "The Woodhaven"
    assert u0["unit_number"] == "13 - 115"
    assert u0["bedrooms"] == "1"
    assert u0["bathrooms"] == "1"
    assert u0["sqft"] == "725"
    assert u0["market_rent_low"] == 2043
    assert u0["availability_status"] == "AVAILABLE"
    assert u0["availability_date"] == ""  # "Available Now" -> blank date
    assert u0["extraction_tier"] == "TIER_1_DOM_ASPENSQUARE"
    # date-bearing unit
    assert units[1]["availability_date"] == "06/06/2026"
    assert units[2]["market_rent_low"] == 2023


def test_parse_plan_level_fallback_when_no_drill_units() -> None:
    units = parse_aspensquare_cards([_PLAN_LIMITED], "u")
    assert len(units) == 1
    p = units[0]
    assert p["unit_number"] == ""  # plan-level
    assert p["floor_plan_name"] == "The Maplewood"
    assert p["bedrooms"] == "1"
    assert p["sqft"] == "425"
    assert p["market_rent_low"] is None  # "Call For Pricing"
    # "Limited Availability" + no rent → UNAVAILABLE per parser policy
    assert p["availability_status"] == "UNAVAILABLE"


def test_parse_mixed_plans() -> None:
    units = parse_aspensquare_cards([_PLAN_WITH_UNITS, _PLAN_LIMITED], "u")
    assert len(units) == 4  # 3 unit-level + 1 plan-level fallback


def test_parse_studio() -> None:
    studio = {
        "name": "The Studio",
        "specs": "Studio1 bath350 sq ft",
        "planRent": "$1,500 starting",
        "badge": "2 Available",
        "drillPath": "",
        "units": [],
    }
    u = parse_aspensquare_cards([studio], "u")[0]
    assert u["bedrooms"] == "0"
    assert u["bed_label"]
    assert u["available_units"] == "2"


@pytest.mark.asyncio
async def test_adapter_extract_woodhaven() -> None:
    result = await AspenSquareAdapter().extract(_FakePage([_PLAN_WITH_UNITS]), _ctx())  # type: ignore[arg-type]
    assert result.tier_used == "TIER_1_DOM_ASPENSQUARE"
    assert len(result.units) == 3
    assert result.units[0]["unit_number"] == "13 - 115"
    assert result.confidence > 0.7


@pytest.mark.asyncio
async def test_adapter_no_cards() -> None:
    result = await AspenSquareAdapter().extract(_FakePage([]), _ctx())  # type: ignore[arg-type]
    assert result.units == []
    assert result.confidence == 0.0


def test_detector_routes_aspensquare_host() -> None:
    det = detect_pms("https://www.aspensquare.com/apartments/ma/westfield/southwood-acres")
    assert det.pms == "aspensquare"
    assert det.recommended_strategy == "dom_first"


def test_adapter_registered() -> None:
    adapter = get_adapter("aspensquare")
    assert isinstance(adapter, AspenSquareAdapter)
    assert adapter.pms_name == "aspensquare"
