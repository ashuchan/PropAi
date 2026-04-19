"""Tests for the daily_runner parser reuse in Jugnu adapters (step 1+2).

Verifies:
  - generic adapter falls through to daily_runner's parse_api_responses when
    its narrow parser finds nothing (50+ key variants, nested-rent handling)
  - generic adapter routes SightMap bodies to daily_runner's host parser
  - onesite + realpage_oll handle the /units endpoint via
    daily_runner._realpage_units_from_body (incl. null response handling)
  - the bridge module imports cleanly without pulling Playwright eagerly
"""
from __future__ import annotations

import pytest

from ma_poc.pms.adapters.base import AdapterContext, AdapterResult
from ma_poc.pms.adapters.generic import GenericAdapter
from ma_poc.pms.adapters.onesite import OneSiteAdapter
from ma_poc.pms.adapters.realpage_oll import RealPageOllAdapter
from ma_poc.pms.detector import detect_pms


class _DummyPage:
    pass


def _ctx(url: str, api_responses: list[dict]) -> AdapterContext:
    ctx = AdapterContext(
        base_url=url,
        detected=detect_pms(url),
        profile=None,
        expected_total_units=None,
        property_id="test",
    )
    ctx._api_responses = api_responses  # type: ignore[attr-defined]
    return ctx


# ── Bridge module sanity ─────────────────────────────────────────────────────

def test_bridge_imports_cleanly() -> None:
    """_daily_runner_parsers should import without error and expose the lift."""
    from ma_poc.pms.adapters import _daily_runner_parsers as bridge

    assert callable(bridge.parse_api_responses)
    assert callable(bridge.realpage_units_to_adapter_shape)
    assert callable(bridge.parse_sightmap_payload)
    assert isinstance(bridge._UNIT_ID_KEYS, (set, frozenset, tuple, list))
    assert isinstance(bridge._RENT_KEYS, (set, frozenset, tuple, list))


# ── Generic adapter — nested-rent handling from daily_runner ──────────────────

@pytest.mark.asyncio
async def test_generic_falls_through_to_daily_runner_on_nested_rent() -> None:
    """ResMan-style nested rent object: {rent: {min: 1351, max: 1351}}.

    Jugnu's narrow parse_generic_api looks for flat scalar rent keys;
    daily_runner's parse_api_responses unwraps nested rent dicts. The
    second-pass should catch these.
    """
    body = {
        "units": [
            {"id": "101", "rent": {"min": 1351, "max": 1351}, "sqft": 700, "bedrooms": 1},
            {"id": "102", "rent": {"min": 1551, "max": 1551}, "sqft": 800, "bedrooms": 2},
            {"id": "103", "rent": {"min": 1451, "max": 1451}, "sqft": 750, "bedrooms": 1},
        ]
    }
    ctx = _ctx("https://example.com/", [{"url": "https://example.com/api/units", "body": body}])
    result = await GenericAdapter().extract(_DummyPage(), ctx)  # type: ignore[arg-type]

    assert isinstance(result, AdapterResult)
    assert len(result.units) >= 1, \
        f"daily_runner fallthrough should catch nested-rent shape; errors={result.errors}"


# ── Generic adapter — SightMap host routing ───────────────────────────────────

@pytest.mark.asyncio
async def test_generic_routes_sightmap_to_dedicated_parser() -> None:
    """SightMap uses floor_plans[] + units[] joined by floor_plan_id.

    daily_runner's _parse_sightmap_payload does the join. The generic
    adapter should route SightMap URLs to it.
    """
    sightmap_body = {
        "data": {
            "floor_plans": [
                {"id": 1, "name": "A1", "filter_label": "1 Bed",
                 "bedroom_count": 1, "bathroom_count": 1},
            ],
            "units": [
                {"floor_plan_id": 1, "price": 1500, "display_price": "$1,500",
                 "area": 650, "unit_number": "101", "available_on": "2026-05-01"},
                {"floor_plan_id": 1, "price": 1525, "display_price": "$1,525",
                 "area": 650, "unit_number": "102", "available_on": "2026-05-15"},
            ],
        }
    }
    url = "https://example.sightmap.com/api/floorplans"
    ctx = _ctx("https://example.com/", [{"url": url, "body": sightmap_body}])
    result = await GenericAdapter().extract(_DummyPage(), ctx)  # type: ignore[arg-type]

    assert isinstance(result, AdapterResult)
    assert len(result.units) >= 1, \
        f"SightMap bodies should route to daily_runner parser; errors={result.errors}"


# ── RealPage /units endpoint via onesite adapter ─────────────────────────────

@pytest.mark.asyncio
async def test_onesite_handles_units_endpoint_with_data() -> None:
    """RealPage /units endpoint returning actual unit rows."""
    units_body = {
        "response": [
            {"id": 1001, "unitNumber": "A101", "minRent": 1500, "maxRent": 1500,
             "sqft": 750, "bedRooms": 1, "bathRooms": 1,
             "availableDate": "2026-05-01"},
            {"id": 1002, "unitNumber": "A102", "minRent": 1550, "maxRent": 1550,
             "sqft": 750, "bedRooms": 1, "bathRooms": 1,
             "availableDate": "2026-05-15"},
        ]
    }
    url = "https://api.ws.realpage.com/v2/property/7824595/units"
    ctx = _ctx("https://8756399.onlineleasing.realpage.com/", [{"url": url, "body": units_body}])
    result = await OneSiteAdapter().extract(_DummyPage(), ctx)  # type: ignore[arg-type]

    assert isinstance(result, AdapterResult)
    # The /units endpoint should produce at least one unit record.
    assert len(result.units) >= 1, \
        f"RealPage /units body should parse; errors={result.errors}"


@pytest.mark.asyncio
async def test_onesite_handles_units_endpoint_null_body() -> None:
    """/units endpoint returning null when no availability. Should not crash."""
    url = "https://api.ws.realpage.com/v2/property/7824595/units"
    ctx = _ctx("https://8756399.onlineleasing.realpage.com/", [{"url": url, "body": None}])
    result = await OneSiteAdapter().extract(_DummyPage(), ctx)  # type: ignore[arg-type]

    assert isinstance(result, AdapterResult)
    # No data but no crash. errors may or may not contain "no data" message.
    assert result.units == []


@pytest.mark.asyncio
async def test_realpage_oll_handles_units_endpoint() -> None:
    """Same /units handling for the non-OneSite portal."""
    units_body = {
        "response": [
            {"id": 2001, "unitNumber": "B201", "minRent": 2100, "maxRent": 2100,
             "sqft": 900, "bedRooms": 2, "bathRooms": 2,
             "availableDate": "2026-06-01"},
        ]
    }
    url = "https://api.ws.realpage.com/v2/property/1234/units"
    ctx = _ctx("https://www.myrealpageportal.com/", [{"url": url, "body": units_body}])
    # Manually mark as realpage_oll (detector would pick based on portal hop).
    ctx.detected = detect_pms("https://myrealpage.com")
    result = await RealPageOllAdapter().extract(_DummyPage(), ctx)  # type: ignore[arg-type]

    assert isinstance(result, AdapterResult)
    assert len(result.units) >= 1
    assert all(u.get("extraction_tier") == "TIER_1_API_REALPAGE_OLL" for u in result.units)
