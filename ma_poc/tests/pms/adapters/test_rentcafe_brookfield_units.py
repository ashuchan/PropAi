from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from ma_poc.pms.adapters import _rentcafe_brookfield_units as brookfield
from ma_poc.pms.adapters.base import AdapterContext, AdapterResult
from ma_poc.pms.adapters.rentcafe import (
    _try_brookfield_unit_handoff,
    parse_rentcafe_floorplans,
)
from ma_poc.pms.detector import detect_pms


def _property_object(
    *,
    property_id: str = "1782224",
    name: str = "Briggs & Union",
    address: str = "12000 Knox Way",
    city: str = "Mt Laurel Township",
    state: str = "NJ",
) -> str:
    return (
        f'{{"propertyId":"{property_id}","parentId":"{property_id}",'
        f'"propertyName":"{name}","address":"{address}",'
        f'"city":"{city}","state":"{state}","zipCode":"8054"}}'
    )


def _ctx(
    *,
    body: str | None = None,
    base_url: str = "https://rent.brookfieldproperties.com/property/briggs-and-union/",
    name: str = "Briggs & Union",
    address: str = "12000 Knox Way",
) -> AdapterContext:
    ctx = AdapterContext(
        base_url=base_url,
        detected=detect_pms(base_url),
        profile=None,
        expected_total_units=None,
        property_id="254360",
        fetch_result=SimpleNamespace(
            body=(body or _property_object()).encode(),
            final_url=base_url,
        ),
        property_name=name,
        address=address,
        city="Mt Laurel Township",
        state="NJ",
    )
    ctx._api_responses = []
    return ctx


def _unit(
    apartment_id: str,
    apartment_name: str,
    *,
    property_id: str = "1782224",
    property_name: str = "Briggs & Union",
    rent: int = 2035,
) -> dict[str, Any]:
    return {
        "propertyId": property_id,
        "parentId": property_id,
        "floorplanId": "5251218",
        "floorplanName": "1A w/ Balcony",
        "apartmentId": apartment_id,
        "apartmentName": apartment_name,
        "buildingNumber": None,
        "beds": "1",
        "baths": "1",
        "sqft": "688",
        "minimumRent": rent,
        "maximumRent": rent + 200,
        "availableDate": "2026-08-27",
        "propertyName": property_name,
    }


@pytest.mark.parametrize(
    ("property_id", "name", "address"),
    (
        ("1782224", "Briggs & Union", "12000 Knox Way"),
        ("1782232", "Village on Memorial", "15200 Memorial Dr"),
        ("1807807", "St. James Crossing", "5620 Tranquility Oaks Dr"),
    ),
)
def test_finds_exact_live_brookfield_property_bindings(
    property_id: str,
    name: str,
    address: str,
) -> None:
    binding = brookfield.find_brookfield_binding(
        _ctx(
            body=_property_object(
                property_id=property_id,
                name=name,
                address=address,
            ),
            name=name,
            address=address,
        )
    )
    assert binding is not None
    assert binding.property_id == property_id
    assert binding.property_name == name
    assert binding.address == address


@pytest.mark.parametrize(
    "ctx",
    (
        _ctx(address="999 Unrelated Ave"),
        _ctx(base_url="https://example.com/property/briggs-and-union/"),
        _ctx(name="Different Property"),
        _ctx(name=""),
        _ctx(address=""),
    ),
    ids=("address-miss", "wrong-host", "name-miss", "no-name", "no-address"),
)
def test_binding_fails_closed_outside_exact_canonical_scope(ctx: AdapterContext) -> None:
    assert brookfield.find_brookfield_binding(ctx) is None


def test_strict_rows_emit_real_apartment_identity_and_positive_rent() -> None:
    binding = brookfield.BrookfieldBinding(
        property_id="1782224",
        property_name="Briggs & Union",
        address="12000 Knox Way",
        city="Mt Laurel Township",
        state="NJ",
    )
    rows = brookfield._strict_unit_rows(
        [_unit("45428233", "11303"), _unit("45428234", "12207", rent=2250)],
        binding,
        "https://rent.brookfieldproperties.com/wp-json/middleware/v1/getUnits",
    )

    assert [row["unit_number"] for row in rows] == ["11303", "12207"]
    assert [row["market_rent_low"] for row in rows] == [2035, 2250]
    assert rows[0]["source_ids"] == {
        "securecafe_apartment_id": "45428233",
        "rentcafe_floorplan_id": "5251218",
    }
    assert rows[0]["availability_status"] == "AVAILABLE"
    assert rows[0]["extraction_tier"] == "TIER_1_API_RENTCAFE_BROOKFIELD_UNITS"


@pytest.mark.parametrize(
    "payload",
    (
        # getFloorplans aggregates have a plan id and count, but no apartment.
        [
            {
                "propertyId": "1782224",
                "parentId": "1782224",
                "floorplanId": "5251218",
                "floorplanName": "1A w/ Balcony",
                "availableUnitsCount": "26",
                "minimumRent": "2035.00",
                "maximumRent": "3793.00",
                "propertyName": "Briggs & Union",
            }
        ],
        [_unit("45428233", "11303"), _unit("45428233", "12207")],
        [_unit("45428233", "11303"), _unit("45428234", "11303")],
        [_unit("45428233", "11303", property_id="9999999")],
        [_unit("45428233", "11303", property_name="Other Property")],
        [_unit("45428233", "11303", rent=0)],
    ),
    ids=(
        "plan-only",
        "duplicate-apartment-id",
        "duplicate-apartment-name",
        "wrong-property-id",
        "wrong-property-name",
        "zero-rent",
    ),
)
def test_strict_rows_reject_plan_or_ambiguous_payloads(payload: object) -> None:
    binding = brookfield.BrookfieldBinding(
        property_id="1782224",
        property_name="Briggs & Union",
        address="12000 Knox Way",
        city="Mt Laurel Township",
        state="NJ",
    )
    assert brookfield._strict_unit_rows(payload, binding, "https://example.test") == []


@pytest.mark.asyncio
async def test_bounded_fetch_accepts_only_same_endpoint_redirects() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if len(seen) == 1:
            return httpx.Response(302, headers={"location": "/wp-json/middleware/v1/getUnits"})
        return httpx.Response(200, json=[_unit("45428233", "11303")])

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=False,
        trust_env=False,
    ) as client:
        status, payload, final_url = await brookfield._fetch_public_json(
            client,
            "https://rent.brookfieldproperties.com/wp-json/middleware/v1/getUnits?x=1",
        )

    assert status == 200
    assert isinstance(payload, list)
    assert final_url == "https://rent.brookfieldproperties.com/wp-json/middleware/v1/getUnits"
    assert len(seen) == 2


@pytest.mark.asyncio
async def test_bounded_fetch_rejects_cross_origin_redirect() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302,
            headers={"location": "https://example.com/wp-json/middleware/v1/getUnits"},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=False,
        trust_env=False,
    ) as client:
        status, payload, final_url = await brookfield._fetch_public_json(
            client,
            "https://rent.brookfieldproperties.com/wp-json/middleware/v1/getUnits",
        )

    assert status == 302
    assert payload is None
    assert final_url == ""


@pytest.mark.asyncio
async def test_handoff_preserves_plan_catalogue_and_promotes_strict_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = "https://rent.brookfieldproperties.com/wp-json/middleware/v1/getUnits"
    binding = brookfield.BrookfieldBinding(
        property_id="1782224",
        property_name="Briggs & Union",
        address="12000 Knox Way",
        city="Mt Laurel Township",
        state="NJ",
    )
    rows = brookfield._strict_unit_rows([_unit("45428233", "11303")], binding, source)

    async def fake_recover(_ctx: AdapterContext) -> tuple[list[dict[str, Any]], str]:
        return rows, source

    monkeypatch.setattr(brookfield, "recover_brookfield_units", fake_recover)
    plans = parse_rentcafe_floorplans(
        [
            {
                "propertyId": "1782224",
                "floorplanId": "5251218",
                "floorplanName": "1A w/ Balcony",
                "beds": "1",
                "baths": "1",
                "minimumSQFT": "688",
                "maximumSQFT": "688",
                "minimumRent": "2035",
                "maximumRent": "3793",
                "availableUnitsCount": "1",
            }
        ],
        "https://rent.brookfieldproperties.com/wp-json/middleware/v1/getFloorplans",
    )

    result = await _try_brookfield_unit_handoff(_ctx(), AdapterResult(), plan_summaries=plans)

    assert result is not None
    assert [row["unit_number"] for row in result.units] == ["11303"]
    assert result.plan_summaries == plans
    assert result.tier_used == "TIER_1_API_RENTCAFE_BROOKFIELD_UNITS"
    assert result.winning_url == source
