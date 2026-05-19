"""LeaseLeads embed recovery (2026-05-19, greenfield).

Plan data captured live from api.leaseleads.co/api/v2/property/{uuid}
/floor-plans for liveatlumina.com (UUID 9e5e0a14-…). 30 plans returned;
the fixtures below are 3 representative shapes: Available-Now,
Move-In-date, and Waitlist.
"""

from __future__ import annotations

import json
import pytest

from ma_poc.pms.adapters._leaseleads_embed import (
    parse_leaseleads_floorplans,
    recover_leaseleads_embed,
)
from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.detector import detect_pms

# Live shapes (sanitized excerpts):
_LL_PLANS = [
    {  # Available Now with rent range
        "id": "b0e0f039-86e9-4fd9-8a22-c7f07ccd8b22",
        "name": "Rigel Luxury",
        "status": "Available Now",
        "bedrooms": 1,
        "bathrooms": 1,
        "price": "$2,310",
        "price_min": 2310,
        "price_max": 3707,
        "size_min": 737,
        "size_max": 737,
        "marketing_label": None,
    },
    {  # Move-In date variant
        "id": "30f59723-969a-4549-8b80-f9ac739a81bf",
        "name": "Sirius",
        "status": "Move In May 26th, 2026",
        "bedrooms": 2,
        "bathrooms": 2,
        "price_min": 2900,
        "price_max": 2900,
        "size_min": 1050,
        "marketing_label": "One month free on 13-month lease",
    },
    {  # Waitlist — no rent
        "id": "30f59723-969a-4549-8b80-f9ac739a81bf",
        "name": "Pegasus",
        "status": "Waitlist",
        "bedrooms": 1,
        "bathrooms": 1,
        "price": "Call for pricing",
        "price_min": 0,
        "price_max": 0,
        "size_min": 912,
    },
]


class _FakePage:
    """Stub Page whose evaluate() dispatches on JS shape (scan vs API)."""

    def __init__(self, scan_result: object, api_text: str, url: str = "https://liveatlumina.com/all-floor-plans") -> None:
        self._scan = scan_result
        self._api = api_text
        self.url = url
        self.evals: list[str] = []

    async def evaluate(self, js: str, *_a: object) -> object:
        self.evals.append(js[:60])
        # The fetch JS contains the literal API URL "api.leaseleads.co/api/v2".
        # The scan JS uses a regex-escaped "embed\\.leaseleads\\.co" string.
        if "api.leaseleads.co/api/v2" in js:
            return self._api
        return self._scan


def _ctx() -> AdapterContext:
    return AdapterContext(
        base_url="https://liveatlumina.com/",
        detected=detect_pms("https://liveatlumina.com/"),
        profile=None,
        expected_total_units=None,
        property_id="P_TEST",
    )


def test_parse_available_with_range() -> None:
    units = parse_leaseleads_floorplans([_LL_PLANS[0]], "u")
    assert len(units) == 1
    u = units[0]
    assert u["floor_plan_name"] == "Rigel Luxury"
    assert u["bedrooms"] == "1"
    assert u["sqft"] == "737"
    assert u["market_rent_low"] == 2310
    assert u["market_rent_high"] == 3707
    assert u["availability_status"] == "AVAILABLE"
    assert u["availability_date"] == ""  # "Available Now" → blank date
    assert u["extraction_tier"] == "TIER_1_API_LEASELEADS"


def test_parse_move_in_status() -> None:
    units = parse_leaseleads_floorplans([_LL_PLANS[1]], "u")
    u = units[0]
    assert u["floor_plan_name"] == "Sirius"
    assert u["market_rent_low"] == 2900
    assert u["availability_status"] == "AVAILABLE"
    assert u["availability_date"] == "May 26th, 2026"
    assert u["concession"] == "One month free on 13-month lease"


def test_parse_waitlist() -> None:
    units = parse_leaseleads_floorplans([_LL_PLANS[2]], "u")
    u = units[0]
    assert u["floor_plan_name"] == "Pegasus"
    assert u["market_rent_low"] is None  # price_min=0 → no rent
    assert u["availability_status"] == "UNAVAILABLE"


def test_parse_skips_empty_rows() -> None:
    assert parse_leaseleads_floorplans([{}, {"name": "", "bedrooms": None, "price_min": 0}], "u") == []


@pytest.mark.asyncio
async def test_recover_via_live_iframe() -> None:
    scan = {"hits": ["https://embed.leaseleads.co/9e5e0a14-d118-40db-89df-b02a6176e804/floor-plans"], "source": "live"}
    page = _FakePage(scan, json.dumps(_LL_PLANS))
    units = await recover_leaseleads_embed(page, _ctx())  # type: ignore[arg-type]
    assert len(units) == 3
    assert units[0]["floor_plan_name"] == "Rigel Luxury"
    assert units[2]["availability_status"] == "UNAVAILABLE"


@pytest.mark.asyncio
async def test_recover_no_iframe_returns_empty() -> None:
    scan = {"hits": [], "source": "none"}
    page = _FakePage(scan, "")
    units = await recover_leaseleads_embed(page, _ctx())  # type: ignore[arg-type]
    assert units == []


@pytest.mark.asyncio
async def test_recover_api_returns_empty_body() -> None:
    scan = {"hits": ["https://embed.leaseleads.co/9e5e0a14-d118-40db-89df-b02a6176e804/floor-plans"], "source": "live"}
    page = _FakePage(scan, None)  # API fails / returns null
    units = await recover_leaseleads_embed(page, _ctx())  # type: ignore[arg-type]
    assert units == []


@pytest.mark.asyncio
async def test_recover_pageless_stub() -> None:
    class _Bare:
        url = "https://x.com/"

    units = await recover_leaseleads_embed(_Bare(), _ctx())  # type: ignore[arg-type]
    assert units == []
