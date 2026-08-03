from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import ma_poc.pms.adapters.rentmanager as rentmanager
from ma_poc.pms.adapters.rentmanager import (
    RentManagerAdapter,
    _iloveleasing_widget_matches_context,
    parse_iloveleasing_availability,
    parse_iloveleasing_settings,
)


_EMBED = """
<script>
window.luv_settings = [
  'PUBLIC-GUID', 'atlantico-property', 'public-source', ''
];
</script>
<script src="https://www.iloveleasing.com/pub/widget/js/luv.js"></script>
"""

_PROPERTY = {
    "data": {
        "location": {
            "name": "Atlantico at Alton Apartments",
            "address": {
                "street1": "13805 Emerson Street",
                "city": "Palm Beach Gardens",
                "state": "FL ",
                "zip": "33418",
            },
        }
    },
    "modules": ["availability", "schedule"],
}

_AVAILABILITY = {
    "valid": "true",
    "units": [
        {
            "unitid": "native-1",
            "unitname": "9-109",
            "planname": "Brewster",
            "dateavailable": "5/6/2026",
            "sqft": "802",
            "beds": "1",
            "baths": "1.0",
            "termprices": [
                {"term": 7, "price": "2891.00"},
                {"term": 12, "price": "2819.00"},
            ],
        },
        {
            "unitid": "native-no-price",
            "unitname": "2-200",
            "planname": "No Public Price",
            "termprices": [],
        },
    ],
}


def _ctx(**overrides: str) -> SimpleNamespace:
    values = {
        "property_id": "77794",
        "property_name": "Atlantico at Alton",
        "address": "13805 Emerson St",
        "city": "Palm Beach Gardens",
        "state": "FL",
        "zip_code": "33418",
        "base_url": "https://www.palmbeachgardensapartments.com/",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class _Response:
    def __init__(self, payload: dict) -> None:
        self.status_code = 200
        self.text = json.dumps(payload)


def test_parse_public_settings_accepts_multiline_single_quotes() -> None:
    assert parse_iloveleasing_settings(_EMBED) == (
        "PUBLIC-GUID",
        "atlantico-property",
        "public-source",
        "",
    )
    assert parse_iloveleasing_settings("<html>none</html>") is None
    assert parse_iloveleasing_settings("window.luv_settings=['only-one']") is None


def test_widget_boundary_requires_exact_name_and_address() -> None:
    assert _iloveleasing_widget_matches_context(_PROPERTY, _ctx())
    assert not _iloveleasing_widget_matches_context(
        {
            **_PROPERTY,
            "data": {
                "location": {
                    **_PROPERTY["data"]["location"],
                    "name": "Atlantico at Another Property Apartments",
                }
            },
        },
        _ctx(),
    )
    assert not _iloveleasing_widget_matches_context(
        _PROPERTY,
        _ctx(address="13806 Emerson St"),
    )


def test_parse_public_availability_keeps_native_priced_rows_only() -> None:
    units = parse_iloveleasing_availability(
        _AVAILABILITY,
        "https://www.iloveleasing.com/pub/wapi/api/availability/",
        _PROPERTY,
    )
    assert len(units) == 1
    unit = units[0]
    assert unit["unit_number"] == "9-109"
    assert unit["source_ids"] == {"iloveleasing_unit_id": "native-1"}
    assert unit["market_rent_low"] == 2819
    assert unit["market_rent_high"] == 2891
    assert unit["available_date"] == "2026-05-06"
    assert unit["address"] == (
        "13805 Emerson Street, Palm Beach Gardens, FL 33418"
    )
    assert unit["source_property_name"] == "Atlantico at Alton Apartments"


def test_adapter_resolves_exact_public_widget(monkeypatch) -> None:
    async def page_html(_page, _ctx):
        return _EMBED

    calls: list[str] = []

    def post(url: str, **_kwargs):
        calls.append(url)
        if url.endswith("/init/"):
            return _Response(
                {
                    "valid": "true",
                    "user_token": "ephemeral-token",
                    "advertiser": {"id": None},
                }
            )
        if url.endswith("/widget/"):
            return _Response({"valid": "true", "property": _PROPERTY})
        if url.endswith("/availability/"):
            return _Response(_AVAILABILITY)
        raise AssertionError(url)

    monkeypatch.setattr(rentmanager, "_get_page_html", page_html)
    monkeypatch.setattr(rentmanager, "probe_post", post)
    result = asyncio.run(RentManagerAdapter().extract(None, _ctx()))

    assert result.tier_used == "TIER_1_API_RENTMANAGER_ILOVELEASING_PUBLIC"
    assert len(result.units) == 1
    assert result.units[0]["unit_number"] == "9-109"
    assert [url.rsplit("/", 2)[-2] for url in calls] == [
        "init",
        "widget",
        "availability",
    ]
    assert len(result.api_responses) == 3


def test_adapter_rejects_portfolio_sibling_before_inventory_call(monkeypatch) -> None:
    async def page_html(_page, _ctx):
        return _EMBED

    sibling = {
        **_PROPERTY,
        "data": {
            "location": {
                **_PROPERTY["data"]["location"],
                "name": "A Different Community Apartments",
            }
        },
    }
    calls: list[str] = []

    def post(url: str, **_kwargs):
        calls.append(url)
        if url.endswith("/init/"):
            return _Response(
                {
                    "valid": "true",
                    "user_token": "ephemeral-token",
                    "advertiser": {"id": None},
                }
            )
        if url.endswith("/widget/"):
            return _Response({"valid": "true", "property": sibling})
        raise AssertionError("availability must not be queried for sibling widget")

    monkeypatch.setattr(rentmanager, "_get_page_html", page_html)
    monkeypatch.setattr(rentmanager, "probe_post", post)
    result = asyncio.run(RentManagerAdapter().extract(None, _ctx()))

    assert result.units == []
    assert len(calls) == 2
    assert any("property_boundary_mismatch" in error for error in result.errors)
