"""G5 adapter — merged (2026-05-19).

Path A: captured inventory.g5marketingcloud.com/graphql response (Patch #11
parser, Apartment-preferred / Floorplan-fallback).
Path B: Apollo cache fallback — unit-level (Apartment↔Prices↔Floorplan join)
preferred, plan-level Floorplan fallback. Apollo shapes captured live from
livemarleymanor.com.
"""

from __future__ import annotations

import pytest

from ma_poc.pms.adapters import get_adapter
from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.adapters.g5 import (
    G5Adapter,
    is_g5_graphql_body,
    is_g5_graphql_url,
    parse_g5_apollo_floorplans,
    parse_g5_apollo_units,
    parse_g5_response,
)
from ma_poc.pms.detector import detect_pms

# ── Path A fixtures: captured GraphQL response bodies ───────────────────────
_G5_APT_BODY = {
    "data": {
        "apartments": [
            {
                "name": "103",
                "building": "B1",
                "availabilityDate": "2026-06-09",
                "sqftDisplay": "1,038",
                "prices": [{"min": 1866, "max": 1866, "leaseTermMonths": 12}],
                "floorplan": {"name": "2 Bed MMI Standard", "beds": 2, "baths": "2.0"},
            }
        ]
    }
}
_G5_FP_BODY = {
    "data": {
        "floorplans": [
            {
                "name": "3 Bed MMI Upgraded",
                "beds": 3,
                "baths": "2.0",
                "sqft": 1246,
                "startingRate": 1799,
                "endingRate": 2076,
                "totalAvailableUnits": 3,
            }
        ]
    }
}

# ── Path B fixtures: Apollo cache (real livemarleymanor shapes) ─────────────
_APOLLO_FPS = [
    {"name": "2 Bed MMI Standard", "beds": 2, "baths": "2.0", "sqft": 1038,
     "startingRate": 1866, "endingRate": 1866, "available": 3, "hasSpecials": False},
    {"name": "3 Bed MMI Upgraded", "beds": 3, "baths": "2.0", "sqft": 1246,
     "startingRate": 1799, "endingRate": 2076, "available": 3, "hasSpecials": True},
]
_APOLLO_UNITS = [
    {"unit": "103", "avail": "2026-06-09", "rentLow": 1866, "rentHigh": 1866,
     "fpName": "2 Bed MMI Standard", "beds": 2, "baths": "2.0", "sqft": 1038},
    {"unit": "302", "avail": "2026-06-13", "rentLow": 1799, "rentHigh": 2076,
     "fpName": "3 Bed MMI Upgraded", "beds": 3, "baths": "2.0", "sqft": 1246},
]


class _FakePage:
    def __init__(self, cache: object, url: str = "https://www.livemarleymanor.com/apartments/md/salisbury/floor-plans") -> None:
        self._cache = cache
        self.url = url

    async def evaluate(self, _js: str, *_a: object) -> object:
        return self._cache


def _ctx(api: list[dict] | None = None) -> AdapterContext:
    ctx = AdapterContext(
        base_url="https://www.livemarleymanor.com/",
        detected=detect_pms("https://www.livemarleymanor.com/"),
        profile=None,
        expected_total_units=None,
        property_id="P_TEST",
    )
    ctx._api_responses = api or []  # type: ignore[attr-defined]
    return ctx


# ── Path A: captured-response parser ────────────────────────────────────────

def test_is_g5_graphql_url() -> None:
    assert is_g5_graphql_url("https://inventory.g5marketingcloud.com/graphql")
    assert not is_g5_graphql_url("https://example.com/graphql")


def test_parse_g5_response_apartment_preferred() -> None:
    units = parse_g5_response(_G5_APT_BODY, "https://inventory.g5marketingcloud.com/graphql")
    assert len(units) == 1
    u = units[0]
    assert u["unit_number"] == "103"
    assert u["floor_plan_name"] == "2 Bed MMI Standard"
    assert u["bedrooms"] == "2"
    assert u["market_rent_low"] == 1866
    assert u["availability_date"] == "2026-06-09"
    assert u["extraction_tier"] == "TIER_1_API_G5_GRAPHQL"


def test_parse_g5_response_floorplan_fallback() -> None:
    units = parse_g5_response(_G5_FP_BODY, "u")
    assert len(units) == 1
    assert units[0]["floor_plan_name"] == "3 Bed MMI Upgraded"
    assert units[0]["market_rent_low"] == 1799
    assert units[0]["market_rent_high"] == 2076
    assert units[0]["unit_number"] == ""  # plan-level


def test_is_g5_graphql_body_rejects_noise() -> None:
    assert not is_g5_graphql_body({"data": {"foo": [{"bar": 1}]}})
    assert not is_g5_graphql_body({"errors": [{"message": "x"}]})


@pytest.mark.asyncio
async def test_adapter_path_a_captured_response_wins() -> None:
    """A captured G5 GraphQL response → Tier-1, Apollo not consulted."""
    ctx = _ctx(api=[{"url": "https://inventory.g5marketingcloud.com/graphql", "body": _G5_APT_BODY}])
    result = await G5Adapter().extract(_FakePage({"floorplans": _APOLLO_FPS, "units": []}), ctx)  # type: ignore[arg-type]
    assert result.tier_used == "TIER_1_API_G5_GRAPHQL"
    assert len(result.units) == 1
    assert result.units[0]["unit_number"] == "103"


# ── Path B: Apollo cache ────────────────────────────────────────────────────

def test_parse_g5_apollo_units_join() -> None:
    units = parse_g5_apollo_units(_APOLLO_UNITS, "u")
    assert len(units) == 2
    a = units[0]
    assert a["unit_number"] == "103"
    assert a["floor_plan_name"] == "2 Bed MMI Standard"
    assert a["bedrooms"] == "2"
    assert a["sqft"] == "1038"
    assert a["market_rent_low"] == 1866
    assert a["availability_date"] == "2026-06-09"
    assert a["extraction_tier"] == "TIER_2_API_G5_APOLLO"
    assert units[1]["market_rent_low"] == 1799
    assert units[1]["market_rent_high"] == 2076


def test_parse_g5_apollo_floorplans_planlevel() -> None:
    units = parse_g5_apollo_floorplans(_APOLLO_FPS, "u")
    assert len(units) == 2
    assert units[0]["market_rent_low"] == 1866
    assert units[1]["concession"] == "SPECIAL"
    assert units[0]["extraction_tier"] == "TIER_2_API_G5_APOLLO"


@pytest.mark.asyncio
async def test_adapter_path_b_prefers_unit_level() -> None:
    """No captured response → Apollo cache; unit-level beats plan-level."""
    page = _FakePage({"floorplans": _APOLLO_FPS, "units": _APOLLO_UNITS})
    result = await G5Adapter().extract(page, _ctx())  # type: ignore[arg-type]
    assert result.tier_used == "TIER_2_API_G5_APOLLO"
    assert len(result.units) == 2
    assert result.units[0]["unit_number"] == "103"  # unit-level, not plan


@pytest.mark.asyncio
async def test_adapter_path_b_planlevel_when_no_units() -> None:
    page = _FakePage({"floorplans": _APOLLO_FPS, "units": []})
    result = await G5Adapter().extract(page, _ctx())  # type: ignore[arg-type]
    assert result.tier_used == "TIER_2_API_G5_APOLLO"
    assert len(result.units) == 2
    assert all(u["unit_number"] == "" for u in result.units)  # plan-level


@pytest.mark.asyncio
async def test_adapter_empty_everywhere() -> None:
    page = _FakePage({"floorplans": [], "units": []})
    result = await G5Adapter().extract(page, _ctx())  # type: ignore[arg-type]
    assert result.units == []
    assert result.confidence == 0.0
    assert result.errors


@pytest.mark.asyncio
async def test_adapter_pageless_and_no_capture() -> None:
    class _Bare:
        url = "https://x.com/"

    result = await G5Adapter().extract(_Bare(), _ctx())  # type: ignore[arg-type]
    assert result.units == []
    assert result.confidence == 0.0


def test_detector_routes_g5_marker() -> None:
    html = (
        '<html><head><script src="https://themes.g5dxm.com/x.js"></script>'
        '</head><body data-client="g5-c-62sb7nzcg-rinnier-management-llc">'
        "</body></html>"
    )
    det = detect_pms("https://www.livemarleymanor.com/", page_html=html)
    assert det.pms == "g5"
    assert det.recommended_strategy == "dom_first"


def test_g5_adapter_registered() -> None:
    adapter = get_adapter("g5")
    assert isinstance(adapter, G5Adapter)
    assert adapter.pms_name == "g5"
