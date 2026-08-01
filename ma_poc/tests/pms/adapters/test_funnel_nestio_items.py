from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.adapters.funnel import (
    FunnelAdapter,
    _is_funnel_current_items_response,
    _published_nestio_listings_url,
    parse_funnel_listings,
)
from ma_poc.pms.detector import detect_pms

KEY = "7536d35593414ef29a6696a9dc35b6fc"
URL = (
    "https://nestiolistings.com/api/v2/listings/all/"
    f"?key={KEY}&property=3152"
)


def _payload(*, community_id: int = 3152, duplicate: bool = False) -> dict:
    def item(listing_id: int, unit_number: str, price: str, date: str) -> dict:
        return {
            "id": listing_id,
            "unit_number": unit_number,
            "price": price,
            "date_available": date,
            "layout": "Alcove Studio",
            "bedrooms": 0,
            "bathrooms": 1.0,
            "square_footage": 516,
            "floor": "10",
            "status": "Available",
            "building": {
                "id": 1442828,
                "name": "220 East 72nd Street",
                "street_address": "220 East 72nd Street",
                "community": {
                    "id": community_id,
                    "name": "220 East 72nd Street",
                    "street_address": "220 East 72nd Street",
                    "city": "New York",
                    "state": "NY",
                    "postal_code": "10021",
                    "website_url": (
                        "https://www.dermotcompany.com/"
                        "building/220-east-72nd-street"
                    ),
                },
            },
        }

    return {
        "page": 1,
        "total_items": 2,
        "total_pages": 1,
        "items": [
            item(5825898, "10E1-0", "5495.00", "2026-10-22"),
            item(5825700, "10E1-0" if duplicate else "8E2-0", "5495.00", "2026-09-04"),
        ],
    }


def _html(*urls: str) -> str:
    return "<html><body><h1>220 East 72nd Street</h1>" + "".join(
        f"<script>// let urlApiApartment = new URL('{url}');</script>"
        for url in urls
    ) + "</body></html>"


def _ctx(html: str) -> AdapterContext:
    base = "https://www.dermotcompany.com/building/220-east-72nd-street"
    return AdapterContext(
        base_url=base,
        detected=detect_pms(base, page_html=html),
        profile=None,
        expected_total_units=None,
        property_id="262799",
        fetch_result=SimpleNamespace(body=html.encode(), final_url=base),
        property_name="220 East 72nd",
        address="220 E 72nd St",
        city="New York",
        state="NY",
        zip_code="10021",
    )


def test_current_items_shape_and_parser_preserve_native_fields() -> None:
    payload = _payload()
    assert _is_funnel_current_items_response(payload)
    rows = parse_funnel_listings(payload, URL)
    assert len(rows) == 2
    first = rows[0]
    assert first["unit_number"] == "10E1-0"
    assert first["floor_plan_name"] == "Alcove Studio"
    assert first["bedrooms"] == "0"
    assert first["bathrooms"] == "1"
    assert first["sqft"] == "516"
    assert first["market_rent_low"] == 5495
    assert first["market_rent_high"] == 5495
    assert first["availability_date"] == "2026-10-22"
    assert first["source_ids"]["funnel_listing_id"] == "5825898"
    assert first["source_ids"]["funnel_building_id"] == "1442828"
    assert first["source_property_id"] == "3152"
    assert first["source_property_name"] == "220 East 72nd Street"
    assert first["source_property_provenance"] == "published_nestio_community"


def test_generic_items_wrapper_is_not_funnel_inventory() -> None:
    assert not _is_funnel_current_items_response(
        {"items": [{"id": 1, "name": "Upper East Side", "city": "New York"}]}
    )


def test_published_url_requires_exactly_one_property_pair() -> None:
    assert _published_nestio_listings_url(_html(URL)) == (URL, "3152")
    sibling = URL.replace("property=3152", "property=3388")
    assert _published_nestio_listings_url(_html(URL, sibling)) is None


@pytest.mark.asyncio
async def test_adapter_recovers_exact_page_published_current_inventory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict]] = []

    def fake_probe(url: str, **kwargs: object) -> SimpleNamespace:
        calls.append((url, kwargs))
        return SimpleNamespace(status_code=200, text=json.dumps(_payload()))

    monkeypatch.setattr("ma_poc.pms.adapters._probe.probe_get", fake_probe)
    result = await FunnelAdapter().extract(None, _ctx(_html(URL)))

    assert len(result.units) == 2
    assert result.plan_summaries == []
    assert result.tier_used == "TIER_1_API_FUNNEL_PUBLISHED_LISTINGS"
    assert result.winning_url == URL
    assert calls == [
        (
            URL,
            {
                "timeout": 20,
                "unlocker": False,
                "proxies": {},
                "verify": True,
                "retries": 1,
            },
        )
    ]
    assert len({row["unit_number"] for row in result.units}) == 2
    assert all(row["market_rent_low"] > 0 for row in result.units)


@pytest.mark.asyncio
async def test_adapter_rejects_payload_from_sibling_community(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "ma_poc.pms.adapters._probe.probe_get",
        lambda *_args, **_kwargs: SimpleNamespace(
            status_code=200,
            text=json.dumps(_payload(community_id=3388)),
        ),
    )
    result = await FunnelAdapter().extract(None, _ctx(_html(URL)))
    assert result.units == []
    assert any("BOUNDARY_REJECTED" in error for error in result.errors)


@pytest.mark.asyncio
async def test_adapter_rejects_duplicate_physical_unit_codes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "ma_poc.pms.adapters._probe.probe_get",
        lambda *_args, **_kwargs: SimpleNamespace(
            status_code=200,
            text=json.dumps(_payload(duplicate=True)),
        ),
    )
    result = await FunnelAdapter().extract(None, _ctx(_html(URL)))
    assert result.units == []
    assert any("STRICT_REJECTED" in error for error in result.errors)
