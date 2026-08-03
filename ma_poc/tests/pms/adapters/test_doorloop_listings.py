from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from ma_poc.pms.adapters._doorloop_listings import (
    build_doorloop_feed_url,
    extract_published_doorloop_listing_urls,
    parse_doorloop_mits,
    recover_doorloop_listings,
)
from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.adapters.wix_nopms import WixNoPmsAdapter
from ma_poc.pms.detector import detect_pms

LISTING_URL = (
    "https://parkplace.app.doorloop.com/tenant-portal/"
    "rental-applications/listing?"
    "companyId=65f8a0d0390bcd90dba4e93f&source=CompanyLink"
)
FEED_URL = (
    "https://parkplace.app.doorloop.com/api/units/listings/mits/json/"
    "65f8a0d0390bcd90dba4e93f/1?partnerKey=doorLoopListingSites&"
    "subdomain=parkplace&filter_rentalAppListed=true&"
    "filter_showPropertyList=false"
)


def _ctx(body: str = "") -> AdapterContext:
    ctx = AdapterContext(
        base_url="https://www.parkplacegreenville.com/",
        detected=detect_pms(
            "https://www.parkplacegreenville.com/",
            page_html='<script src="https://static.parastorage.com/x.js"></script>',
        ),
        profile=None,
        expected_total_units=None,
        property_id="254556",
        fetch_result=SimpleNamespace(
            body=body.encode(),
            final_url="https://www.parkplacegreenville.com/",
        ),
        property_name="Park Place",
        address="305 W Jack Finney Blvd",
        city="Greenville",
        state="TX",
        zip_code="75402",
    )
    return ctx


def _property(
    *,
    listing_id: str,
    unit: str,
    rent: int | None,
    street: str = "305 W Jack Finney Blvd.",
    city: str = "Greenville",
    state: str = "TX",
    zip_code: str = "75402",
) -> dict:
    return {
        "_IDValue": listing_id,
        "_ListingID": listing_id,
        "_OrganizationName": "Park Place Luxury Apartments",
        "PropertyID": {
            "propertyId": "65fb3287fac79949cd8a411b",
            "MarketingName": f"{street} - {unit}",
            "unitName": unit,
            "Address": [
                {
                    "AddressLine1": street,
                    "AddressLine2": unit,
                    "City": city,
                    "StateCode": state,
                    "PostalCode": zip_code,
                }
            ],
        },
        "ILS_Unit": {
            "Units": {
                "Unit": [
                    {
                        "UnitBedrooms": 2,
                        "UnitBathrooms": 2,
                        "MinSquareFeet": 1114,
                        "UnitRent": rent,
                        "MarketRent": rent,
                    }
                ]
            },
            "Availability": {
                "VacateDate": {"_Year": 2026, "_Month": 8, "_Day": 1},
                "MadeReadyDate": {"_Year": 2026, "_Month": 9, "_Day": 15},
            },
        },
    }


def _payload() -> dict:
    return {
        "companyName": "Park Place Luxury Apartments",
        "PhysicalProperty": {
            "Management": [
                {
                    "_IDValue": "65f8a0d0390bcd90dba4e93f",
                    "_OrganizationName": "Park Place Luxury Apartments",
                }
            ],
            "Property": [
                _property(
                    listing_id="65fb3288fac79949cd8a4212",
                    unit="A2",
                    rent=1525,
                ),
                _property(
                    listing_id="65fb328afac79949cd8a454a",
                    unit="C6",
                    rent=1475,
                ),
                # A different property in the same management feed.
                _property(
                    listing_id="65fb328afac79949cd8a455b",
                    unit="B1",
                    rent=1800,
                    street="900 Other Street",
                    city="Dallas",
                    zip_code="75201",
                ),
                # DoorLoop live feeds can publish $1 placeholder rows.
                _property(
                    listing_id="65fb328afac79949cd8a456c",
                    unit="WAIT1",
                    rent=1,
                ),
            ],
        },
    }


def test_extracts_only_exact_published_company_listing_url() -> None:
    body = f"""
    <a href="{LISTING_URL.replace('&', '&amp;')}">Apply</a>
    <a href="https://evil.example.com/?next={LISTING_URL}">not DoorLoop</a>
    <a href="https://parkplace.app.doorloop.com/tenant-portal/rental-applications/listing?propertyId=abc">no company id</a>
    """
    assert extract_published_doorloop_listing_urls(body) == [LISTING_URL]


def test_builds_public_feed_from_published_host_and_company_id() -> None:
    assert build_doorloop_feed_url(LISTING_URL) == FEED_URL
    assert build_doorloop_feed_url(
        "https://doorloop.example.com/tenant-portal/rental-applications/listing?"
        "companyId=65f8a0d0390bcd90dba4e93f"
    ) == ""


def test_parser_filters_other_property_and_placeholder_rent() -> None:
    rows = parse_doorloop_mits(_payload(), _ctx(), FEED_URL, published_url=LISTING_URL)
    assert [row["unit_number"] for row in rows] == ["A2", "C6"]
    assert [row["market_rent_low"] for row in rows] == [1525, 1475]
    assert all(row["availability_date"] == "2026-09-15" for row in rows)
    assert all(row["source_portal_url"] == LISTING_URL for row in rows)
    assert all(
        row["source_ids"]["doorloop_listing_id"].startswith("65fb") for row in rows
    )
    assert len({row["source_ids"]["doorloop_listing_id"] for row in rows}) == 2


def test_parser_fails_closed_without_configured_property_address() -> None:
    ctx = _ctx()
    ctx.address = ""
    assert parse_doorloop_mits(_payload(), ctx, FEED_URL) == []


@pytest.mark.asyncio
async def test_recovery_uses_captured_feed_without_network() -> None:
    ctx = _ctx()
    ctx._api_responses = [{"url": FEED_URL, "body": _payload()}]  # type: ignore[attr-defined]
    with patch(
        "ma_poc.pms.adapters._probe.probe_fetch_status",
        AsyncMock(side_effect=AssertionError("network should not run")),
    ):
        rows = await recover_doorloop_listings(ctx)
    assert [row["unit_number"] for row in rows] == ["A2", "C6"]


@pytest.mark.asyncio
async def test_wix_adapter_full_e2e_from_published_link() -> None:
    ctx = _ctx(f'<html><body><a href="{LISTING_URL}">Apply now</a></body></html>')
    with patch(
        "ma_poc.pms.adapters._probe.probe_fetch_status",
        AsyncMock(return_value=(200, json.dumps(_payload()))),
    ) as fetch:
        result = await WixNoPmsAdapter().extract(None, ctx)  # type: ignore[arg-type]

    fetch.assert_awaited_once_with(FEED_URL)
    assert result.tier_used == "TIER_1_API_DOORLOOP_MITS"
    assert [row["unit_number"] for row in result.units] == ["A2", "C6"]
    assert all(row["market_rent_low"] > 0 for row in result.units)
    assert not result.plan_summaries
