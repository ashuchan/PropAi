"""Aspen Square Management adapter (2026-05-19, greenfield).

Card+unit data captured live from
aspensquare.com/apartments/massachusetts/westfield/southwood-acres
(community) and .../floor-plans/the-woodhaven (unit drill).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from ma_poc.pms.adapters import get_adapter
from ma_poc.pms.adapters.aspensquare import (
    AspenSquareAdapter,
    parse_aspensquare_cards,
    parse_aspensquare_next_surface,
    reconcile_aspensquare_knock_units,
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


def _next_html() -> str:
    """Minimal current app-router frame with exact plan and unit contracts."""
    floorplans = {
        "id": 16,
        "styles": [
            {
                "id": 361,
                "name": "The Essex",
                "assetId": "2115",
                "bathrooms": 2,
                "bedrooms": 2,
                "squareFeet": 925,
                "price": 1614,
                "availableUnits": [
                    {
                        "id": "126-2115",
                        "address": {
                            "unitID": "126",
                            "unitNumber": "G301",
                            "buildingNumber": "G",
                        },
                        "assetId": "2115",
                        "floorPlan": {
                            "floorPlanID": "4",
                            "floorPlanName": "2x2 MKT",
                        },
                        "xRefUnitId": "126",
                        "availability": {
                            "vacantBit": True,
                            "vacantDate": "2026-05-09",
                            "madeReadyDate": "2026-05-22",
                        },
                        "floorPlanName": "The Essex",
                        "baseRentAmount": 1638,
                    },
                    {
                        "id": "66-2115",
                        "address": {
                            "unitID": "66",
                            "unitNumber": "C106",
                            "buildingNumber": "C",
                        },
                        "assetId": "2115",
                        "floorPlan": {
                            "floorPlanID": "4",
                            "floorPlanName": "2x2 MKT",
                        },
                        "xRefUnitId": "66",
                        "availability": {
                            "vacantBit": True,
                            "vacantDate": "2026-07-21",
                            "madeReadyDate": "2026-08-21",
                        },
                        "floorPlanName": "The Essex",
                        "baseRentAmount": 1623,
                    },
                ],
                "xRefFloorPlanID": "4",
                "showAvailability": True,
                "showAvailableUnitPricing": True,
                "showFloorPlanPricing": True,
            },
            {
                "id": 360,
                "name": "The Duke",
                "assetId": "2115",
                "bathrooms": 1,
                "bedrooms": 1,
                "squareFeet": 753,
                # Internal revenue-management value; the empty public card
                # renders Call For Pricing and must not publish this number.
                "price": 1328,
                "availableUnits": [],
                "xRefFloorPlanID": "2",
                "showAvailability": True,
                "showAvailableUnitPricing": True,
                "showFloorPlanPricing": True,
            },
        ],
    }
    payload = f'6:[["$","section",null,{{"floorPlans":{json.dumps(floorplans)}}}]]'
    return f"<script>self.__next_f.push([1,{json.dumps(payload)}])</script>"


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


def test_current_next_surface_preserves_exact_catalogue_and_dates() -> None:
    surface = parse_aspensquare_next_surface(
        _next_html(),
        "https://www.aspensquare.com/apartments/nebraska/papillion/adley-72nd",
        capture_date="2026-08-02",
    )
    assert surface is not None
    assert [plan["name"] for plan in surface.plans] == ["The Essex", "The Duke"]
    assert len(surface.units) == 2

    current, future = surface.units
    assert current["unit_number"] == "G301"
    assert current["building"] == "G"
    assert current["availability_date"] == "Available Now"
    assert current["source_ids"] == {
        "aspensquare_asset_id": "2115",
        "aspensquare_unit_id": "126",
        "aspensquare_floor_plan_id": "4",
    }
    assert future["availability_date"] == "2026-08-21"

    duke = surface.plans[1]["row"]
    assert duke["availability_status"] == "UNAVAILABLE"
    assert duke["market_rent_low"] is None
    assert duke["source_ids"]["aspensquare_floor_plan_id"] == "2"


def test_current_next_surface_rejects_unrelated_or_malformed_frames() -> None:
    assert parse_aspensquare_next_surface("<html>no RSC</html>", "u") is None
    malformed = '<script>self.__next_f.push([1,"not-json])</script>'
    assert parse_aspensquare_next_surface(malformed, "u") is None


def test_reconciliation_keeps_uuid_and_public_identity_without_changing_rent() -> None:
    surface = parse_aspensquare_next_surface(
        _next_html(), "https://www.aspensquare.com/adley", capture_date="2026-08-02"
    )
    assert surface is not None
    knock = [
        {
            "unit_id": "knock_unit_id-uuid-g301",
            "unit_number": "G301",
            "unit_name": "G301",
            "building": "G",
            "floor_plan_name": "2x2 MKT",
            "bedrooms": "2",
            "bathrooms": "2",
            "sqft": "925",
            "market_rent_low": 1593,
            "market_rent_high": 1593,
            "availability_status": "AVAILABLE",
            "availability_date": "2026-05-22",
            "available_date": "2026-05-22",
            "source_ids": {"knock_unit_id": "uuid-g301"},
        },
        # Not in Aspen's capped display window, but deterministically belongs
        # to a non-empty exact plan by internal layout/dimensions.
        {
            "unit_id": "knock_unit_id-uuid-g303",
            "unit_number": "G303",
            "unit_name": "G303",
            "building": "G",
            "floor_plan_name": "2x2 MKT",
            "bedrooms": "2",
            "bathrooms": "2",
            "sqft": "925",
            "market_rent_low": 1777,
            "market_rent_high": 1777,
            "availability_status": "AVAILABLE",
            "availability_date": "2026-09-01",
            "available_date": "2026-09-01",
            "source_ids": {"knock_unit_id": "uuid-g303"},
        },
    ]

    reconciled, conflicts = reconcile_aspensquare_knock_units(knock, surface)
    assert conflicts == []
    assert len(reconciled) == 2
    exact = reconciled[0]
    assert exact["unit_id"] == "knock_unit_id-uuid-g301"
    assert exact["unit_name"] == "G301"
    assert exact["building"] == "G"
    assert exact["floor_plan_name"] == "The Essex"
    assert exact["market_rent_low"] == 1593
    assert exact["availability_date"] == "Available Now"
    assert exact["source_ids"] == {
        "knock_unit_id": "uuid-g301",
        "aspensquare_asset_id": "2115",
        "aspensquare_floor_plan_id": "4",
        "aspensquare_unit_id": "126",
    }
    assert reconciled[1]["availability_date"] == "2026-09-01"
    assert "ASPENSQUARE_KNOCK_FALLBACK_NOT_IN_PUBLIC_WINDOW" in reconciled[1][
        "data_quality_flag"
    ]


def test_reconciliation_withholds_knock_row_for_explicit_empty_plan() -> None:
    surface = parse_aspensquare_next_surface(
        _next_html(), "https://www.aspensquare.com/adley", capture_date="2026-08-02"
    )
    assert surface is not None
    knock = [
        {
            "unit_id": "knock_unit_id-stale-duke",
            "unit_number": "A101",
            "unit_name": "A101",
            "building": "A",
            "floor_plan_name": "1x1 MKT",
            "bedrooms": "1",
            "bathrooms": "1",
            "sqft": "753",
            "market_rent_low": 1428,
            "availability_status": "AVAILABLE",
            "source_ids": {"knock_unit_id": "stale-duke"},
        }
    ]

    reconciled, conflicts = reconcile_aspensquare_knock_units(knock, surface)
    assert reconciled == []
    assert conflicts == ["marketing_empty_withheld:The Duke:stale-duke"]


def test_aspensquare_visible_now_and_future_survive_production_formatter() -> None:
    from ma_poc.scripts.runners.jugnu import _format_v2_unit

    surface = parse_aspensquare_next_surface(
        _next_html(), "https://www.aspensquare.com/adley", capture_date="2026-08-02"
    )
    assert surface is not None
    base = {
        "unit_id": "knock_unit_id-control",
        "unit_number": "G301",
        "unit_name": "G301",
        "building": "G",
        "floor_plan_name": "2x2 MKT",
        "bedrooms": "2",
        "bathrooms": "2",
        "sqft": "925",
        "market_rent_low": 1593,
        "market_rent_high": 1593,
        "availability_status": "AVAILABLE",
        "source_ids": {"knock_unit_id": "control"},
    }
    current_source, future_source = surface.units
    current_rows, _ = reconcile_aspensquare_knock_units([base], surface)
    assert current_source["unit_number"] == "G301"
    current = _format_v2_unit(
        current_rows[0], datetime(2026, 8, 2, 12, tzinfo=UTC), "47710"
    )
    assert current["available_date"] == "2026-08-02"
    assert current["availability_date_provenance"] == "available_now"

    future_input = {
        **base,
        "unit_id": "knock_unit_id-future",
        "unit_number": "C106",
        "unit_name": "C106",
        "building": "C",
        "source_ids": {"knock_unit_id": "future"},
    }
    future_rows, _ = reconcile_aspensquare_knock_units([future_input], surface)
    assert future_source["unit_number"] == "C106"
    future = _format_v2_unit(
        future_rows[0], datetime(2026, 8, 2, 12, tzinfo=UTC), "47710"
    )
    assert future["available_date"] == "2026-08-21"
    assert future["availability_date_provenance"] == "explicit_future"
