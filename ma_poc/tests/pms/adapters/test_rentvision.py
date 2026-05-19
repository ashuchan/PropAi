"""RentVision adapter (2026-05-19, greenfield).

Card data captured live from westgateirving.com/floorplans (single-price +
vacancy variants) and loftsatlittlecreek.com/floorplans (price-range +
"Call for Details" variant) — the shape the SSR .floorplanItem grid emits.
"""

from __future__ import annotations

import pytest

from ma_poc.pms.adapters import get_adapter
from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.adapters.rentvision import RentVisionAdapter, parse_rentvision_cards
from ma_poc.pms.detector import detect_pms

# Live westgateirving.com/floorplans + loftsatlittlecreek.com/floorplans.
_RV_CARDS = [
    {"name": "B2", "bedsAttr": "2", "beds": "2 Bed", "baths": "2 Bath",
     "sqft": "926 Sq Ft square feet", "price": "Pricing Starting at $1,339",
     "avail": "Only 1 Vacant Apartment Left!"},
    {"name": "A3", "bedsAttr": "1", "beds": "1 Bed", "baths": "1 Bath",
     "sqft": "723 Sq Ft square feet", "price": "Pricing Starting at $1,006",
     "avail": "Available"},
    {"name": "Hillside II - Greensboro", "bedsAttr": "Studio",
     "beds": "Studio Bed", "baths": "1 Bath", "sqft": "572 Sq Ft square feet",
     "price": "Price $1,075 - $1,100", "avail": "Call for Details!"},
]


class _FakePage:
    def __init__(self, cards: object, url: str = "https://www.westgateirving.com/floorplans") -> None:
        self._cards = cards
        self.url = url

    async def evaluate(self, _js: str, *_a: object) -> object:
        return self._cards


def _ctx(base_url: str = "https://www.westgateirving.com/") -> AdapterContext:
    return AdapterContext(
        base_url=base_url,
        detected=detect_pms(base_url),
        profile=None,
        expected_total_units=None,
        property_id="P_TEST",
    )


def test_parse_rentvision_cards_fields() -> None:
    units = parse_rentvision_cards(_RV_CARDS, "https://www.westgateirving.com/floorplans")
    assert len(units) == 3

    b2 = units[0]
    assert b2["floor_plan_name"] == "B2"
    assert b2["bedrooms"] == "2"
    assert b2["bathrooms"] == "2"
    assert b2["sqft"] == "926"
    assert b2["market_rent_low"] == 1339
    assert b2["market_rent_high"] == 1339
    assert b2["available_units"] == "1"
    assert b2["availability_status"] == "AVAILABLE"
    assert b2["extraction_tier"] == "TIER_3_DOM_RENTVISION"

    a3 = units[1]
    assert a3["bedrooms"] == "1"
    assert a3["market_rent_low"] == 1006
    assert a3["available_units"] == ""  # "Available", no vacancy count

    studio = units[2]
    assert studio["bedrooms"] == "0"
    assert studio["bed_label"]
    assert studio["market_rent_low"] == 1075
    assert studio["market_rent_high"] == 1100  # price RANGE shape


def test_parse_rentvision_skips_empty_rows() -> None:
    assert parse_rentvision_cards([{}, {"name": "", "bedsAttr": "", "beds": ""}], "u") == []


@pytest.mark.asyncio
async def test_rentvision_adapter_extract() -> None:
    result = await RentVisionAdapter().extract(_FakePage(_RV_CARDS), _ctx())  # type: ignore[arg-type]
    assert result.tier_used == "TIER_3_DOM_RENTVISION"
    assert len(result.units) == 3
    assert result.confidence > 0.0
    assert result.winning_url == "https://www.westgateirving.com/floorplans"


@pytest.mark.asyncio
async def test_rentvision_adapter_no_floorplans_blocks() -> None:
    """No .floorplanItem (evaluate returns []) → clean zero-confidence fail."""
    result = await RentVisionAdapter().extract(_FakePage([]), _ctx())  # type: ignore[arg-type]
    assert result.units == []
    assert result.confidence == 0.0
    assert result.errors


@pytest.mark.asyncio
async def test_rentvision_adapter_pageless_stub() -> None:
    class _Bare:
        url = "https://x.com/"

    result = await RentVisionAdapter().extract(_Bare(), _ctx())  # type: ignore[arg-type]
    assert result.units == []
    assert result.confidence == 0.0


def test_detector_routes_rentvision_marker() -> None:
    html = (
        "<html><body><footer>Website created by RentVision · "
        '<a href="https://rentvision.com">RentVision</a></footer></body></html>'
    )
    det = detect_pms("https://www.westgateirving.com/", page_html=html)
    assert det.pms == "rentvision"
    assert det.recommended_strategy == "dom_first"


def test_rentvision_adapter_registered() -> None:
    adapter = get_adapter("rentvision")
    assert isinstance(adapter, RentVisionAdapter)
    assert adapter.pms_name == "rentvision"
