"""365 ResidentServices adapter (2026-05-19, greenfield).

Tile data captured live from rusticwoodsapts.com and waterfordpoint.us
/Marketing/FloorPlans — the same SSR .floorplan-tile shape both emit.
"""

from __future__ import annotations

import pytest

from ma_poc.pms.adapters import get_adapter
from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.adapters.residentservices365 import (
    Residentservices365Adapter,
    parse_residentservices365_tiles,
)
from ma_poc.pms.detector import detect_pms

# Live rusticwoodsapts.com — units-available + waitlist + range variants
_RUSTICWOODS_TILES = [
    {"title": "Sedona 1 Bed 1 Bath 675 sqft", "specs": "1 Bed 1 Bath 675 sqft",
     "pricing": "$759 per month", "availability": "3 Units Available"},
    {"title": "Bordeaux 1 Bed 1 Bath 750 sqft", "specs": "1 Bed 1 Bath 750 sqft",
     "pricing": "$849 - $899 per month", "availability": "Join Waitlist"},
]
# Live waterfordpoint.us — Studio + "Special" suffix + multi-unit + range
_WATERFORDPOINT_TILES = [
    {"title": "Stafford Studio 1 Bath 392 sqft Special", "specs": "Studio 1 Bath 392 sqft",
     "pricing": "$1,675 - $1,860 per month", "availability": "5 Units Available"},
    {"title": "Oxford 1 Bed 1 Bath 720 sqft", "specs": "1 Bed 1 Bath 720 sqft",
     "pricing": "$2,195 per month", "availability": "1 Unit Available"},
]


class _FakePage:
    def __init__(self, tiles: object, url: str = "https://www.rusticwoodsapts.com/Marketing/FloorPlans") -> None:
        self._tiles = tiles
        self.url = url

    async def evaluate(self, _js: str, *_a: object) -> object:
        return self._tiles


def _ctx(base_url: str = "https://www.rusticwoodsapts.com/") -> AdapterContext:
    return AdapterContext(
        base_url=base_url,
        detected=detect_pms(base_url),
        profile=None,
        expected_total_units=None,
        property_id="P_TEST",
    )


def test_parse_rusticwoods_basic() -> None:
    units = parse_residentservices365_tiles(_RUSTICWOODS_TILES, "u")
    assert len(units) == 2
    s = units[0]
    assert s["floor_plan_name"] == "Sedona"
    assert s["bedrooms"] == "1"
    assert s["bathrooms"] == "1"
    assert s["sqft"] == "675"
    assert s["market_rent_low"] == 759
    assert s["market_rent_high"] == 759
    assert s["available_units"] == "3"
    assert s["availability_status"] == "AVAILABLE"
    assert s["extraction_tier"] == "TIER_1_DOM_365RESIDENTSERVICES"

    b = units[1]
    assert b["floor_plan_name"] == "Bordeaux"
    assert b["market_rent_low"] == 849
    assert b["market_rent_high"] == 899
    assert b["availability_status"] == "UNAVAILABLE"  # Waitlist


def test_parse_waterfordpoint_studio_and_special_suffix() -> None:
    units = parse_residentservices365_tiles(_WATERFORDPOINT_TILES, "u")
    assert len(units) == 2
    stafford = units[0]
    assert stafford["floor_plan_name"] == "Stafford"  # "Special" suffix stripped
    assert stafford["bedrooms"] == "0"  # Studio
    assert stafford["sqft"] == "392"
    assert stafford["market_rent_low"] == 1675
    assert stafford["market_rent_high"] == 1860
    assert stafford["available_units"] == "5"

    oxford = units[1]
    assert oxford["market_rent_low"] == 2195
    assert oxford["available_units"] == "1"


def test_parse_skips_empty() -> None:
    assert parse_residentservices365_tiles([{}, {"title": "", "specs": ""}], "u") == []


@pytest.mark.asyncio
async def test_adapter_extract_rusticwoods() -> None:
    result = await Residentservices365Adapter().extract(_FakePage(_RUSTICWOODS_TILES), _ctx())  # type: ignore[arg-type]
    assert result.tier_used == "TIER_1_DOM_365RESIDENTSERVICES"
    assert len(result.units) == 2
    assert result.confidence > 0.0


@pytest.mark.asyncio
async def test_adapter_no_tiles() -> None:
    result = await Residentservices365Adapter().extract(_FakePage([]), _ctx())  # type: ignore[arg-type]
    assert result.units == []
    assert result.confidence == 0.0
    assert result.errors


@pytest.mark.asyncio
async def test_adapter_pageless_stub() -> None:
    class _Bare:
        url = "https://x.com/"

    result = await Residentservices365Adapter().extract(_Bare(), _ctx())  # type: ignore[arg-type]
    assert result.units == []
    assert result.confidence == 0.0


def test_detector_routes_365rs() -> None:
    html = (
        '<html><body><img src="https://cdn.365residentservices.com/themes/x.png">'
        '<a href="/Marketing/FloorPlans">Floor Plans</a></body></html>'
    )
    det = detect_pms("https://www.rusticwoodsapts.com/", page_html=html)
    assert det.pms == "residentservices365"
    assert det.recommended_strategy == "dom_first"


def test_adapter_registered() -> None:
    adapter = get_adapter("residentservices365")
    assert isinstance(adapter, Residentservices365Adapter)
    assert adapter.pms_name == "residentservices365"
