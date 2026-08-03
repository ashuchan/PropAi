"""Strict OneSite residual-lane coverage for PIDs 39995, 43520 and 14295."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ma_poc.pms.adapters.onesite import (
    _extract_rpfp_config,
    _parse_strict_rpfp_units,
    _probe_same_origin_rpfp_cws,
    parse_onesite_workflowstartup,
)


def _park_ctx() -> SimpleNamespace:
    return SimpleNamespace(
        property_id="43520",
        property_name="Park at Blanding",
        address="222 Blairmore Blvd E",
        city="Orange Park",
        state="FL",
        zip_code="32073",
        base_url="http://www.parkatblanding.com/",
        fetch_result=SimpleNamespace(
            body=b'<a href="/Floor-Plans.aspx">Floor Plans</a>',
            final_url="https://theparkatblanding.com/",
        ),
    )


def _park_floorplans() -> dict:
    return {
        "status": 200,
        "response": {
            "propertyKey": "6O9425995018",
            "floorplans": [
                {
                    "id": "11500521",
                    "name": "2x1 Large",
                    "bedRooms": "2",
                    "bathRooms": "1",
                    "minimumSquareFeet": "970",
                }
            ],
        },
    }


def _park_units() -> dict:
    valid = {
        "id": 18447495,
        "propertyId": 9259508,
        "partnerPropertyId": "5586626",
        "floorplanId": 11500521,
        "unitNumber": "026",
        "leaseStatus": "AVAILABLE_READY",
        "active": True,
        "rent": 1300,
        "squareFeet": 970,
        "internalAvailableDate": "2026-08-01 00:00 -0500",
    }
    return {
        "status": 200,
        "response": {
            "units": [
                valid,
                {**valid, "id": 2, "unitNumber": "leased", "leaseStatus": "LEASED"},
                {**valid, "id": 3, "unitNumber": "inactive", "active": False},
                {**valid, "id": 4, "unitNumber": "free", "rent": 0},
                {**valid, "id": 5, "unitNumber": "foreign-property", "propertyId": 1},
                {**valid, "id": 6, "unitNumber": "foreign-partner", "partnerPropertyId": "1"},
                {**valid, "id": 7, "unitNumber": "foreign-plan", "floorplanId": 999},
                {**valid, "id": "", "unitNumber": "missing-native-id"},
                {**valid, "id": 9, "unitNumber": ""},
            ]
        },
    }


def test_ambiguous_rpfp_property_ids_fail_closed() -> None:
    html = """
        <script>var propertyId='9259508'; var propertyId='1111111';
        var propertyKey='6O9425995018';
        var RPFP_config={apiUrl:'https://c-leasestar-api.realpage.com',
        apiKey:'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
        onlineLeasingUrls:[{'PartnerPropertyId':'5586626'}]};</script>
    """

    assert _extract_rpfp_config(html) is None


def test_pid43520_strict_parser_rejects_nonready_and_foreign_rows() -> None:
    rows = _parse_strict_rpfp_units(
        _park_units(),
        _park_floorplans(),
        property_id="9259508",
        property_key="6O9425995018",
        partner_property_id="5586626",
        source_url="https://api.ws.realpage.com/v2/property/9259508/units",
        source_page_url="https://theparkatblanding.com/Floor-Plans.aspx",
    )

    assert [row["unit_number"] for row in rows] == ["026"]
    assert rows[0]["source_native_unit_id"] == "18447495"
    assert rows[0]["market_rent_low"] == 1300
    assert rows[0]["source_property_id"] == "9259508"
    assert rows[0]["source_partner_property_id"] == "5586626"


@pytest.mark.asyncio
async def test_pid43520_follows_one_published_same_origin_page_and_binds_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ma_poc.pms.adapters import _probe, onesite

    page_calls: list[tuple[str, dict]] = []
    api_calls: list[str] = []
    floor_page = """
        <script>
        var propertyId = '9259508'; var propertyKey = '6O9425995018';
        var RPFP_config = {
          apiUrl: 'https://c-leasestar-api.realpage.com',
          apiKey: 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
          onlineLeasingUrls: [{'PartnerPropertyId':'5586626'}]
        };
        </script>
    """

    def _page_get(url: str, **kwargs: object) -> SimpleNamespace:
        page_calls.append((url, kwargs))
        return SimpleNamespace(status_code=200, text=floor_page, url=url)

    async def _api_get(url: str, _key: str, _origin: str) -> dict:
        api_calls.append(url)
        if url.endswith("/PropertyDetails"):
            return {
                "status": 200,
                "response": {
                    "active": True,
                    "id": "9259508",
                    "name": "The Park at Blanding",
                    "propertyKey": "6O9425995018",
                    "address": {
                        "address1": "222 Blairmore Boulevard East",
                        "cityName": "Orange Park",
                        "stateCode": "FL",
                        "postalCode": "32073",
                    },
                },
            }
        if url.endswith("/floorplans"):
            return _park_floorplans()
        return _park_units()

    monkeypatch.setattr(_probe, "probe_get", _page_get)
    monkeypatch.setattr(onesite, "_fetch_rpfp_json", _api_get)

    rows = await _probe_same_origin_rpfp_cws(_park_ctx())

    assert [row["unit_number"] for row in rows] == ["026"]
    assert page_calls == [
        (
            "https://theparkatblanding.com/Floor-Plans.aspx",
            {"timeout": 15, "unlocker": False, "retries": 1},
        )
    ]
    assert {url.rsplit("/", 1)[-1] for url in api_calls} == {
        "PropertyDetails",
        "floorplans",
        "units",
    }


@pytest.mark.asyncio
async def test_pid43520_property_details_mismatch_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ma_poc.pms.adapters import _probe, onesite

    floor_page = """
        <script>var propertyId='9259508'; var propertyKey='6O9425995018';
        var RPFP_config={apiUrl:'https://c-leasestar-api.realpage.com',
        apiKey:'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
        onlineLeasingUrls:[{'PartnerPropertyId':'5586626'}]};</script>
    """
    monkeypatch.setattr(
        _probe,
        "probe_get",
        lambda url, **_kwargs: SimpleNamespace(status_code=200, text=floor_page, url=url),
    )

    async def _api_get(url: str, _key: str, _origin: str) -> dict:
        if url.endswith("/PropertyDetails"):
            return {
                "status": 200,
                "response": {
                    "active": True,
                    "id": "9259508",
                    "name": "Sibling Community",
                    "propertyKey": "6O9425995018",
                    "address": {
                        "address1": "999 Other Road",
                        "cityName": "Orange Park",
                        "stateCode": "FL",
                        "postalCode": "32073",
                    },
                },
            }
        return _park_floorplans() if url.endswith("/floorplans") else _park_units()

    monkeypatch.setattr(onesite, "_fetch_rpfp_json", _api_get)
    assert await _probe_same_origin_rpfp_cws(_park_ctx()) == []


def test_pid39995_workflow_is_siteid_bound_native_positive_inventory() -> None:
    body = {
        "Workflow": {
            "SiteId": "5272798",
            "ActivityGroups": [
                {
                    "GroupActivities": [
                        {
                            "Floorplans": [
                                {
                                    "Id": "B1",
                                    "Name": "B1",
                                    "Bedrooms": 2,
                                    "Bathrooms": 1,
                                    "Squarefeet": 900,
                                    "MinPriceRange": 1263,
                                    "MaxPriceRange": 1263,
                                    "AvailableUnits": 1,
                                    "UnitIds": ["52"],
                                }
                            ]
                        }
                    ]
                }
            ],
        }
    }
    url = "https://leasing.realpage.com/x/workflowstartup/v1/5272798/English?x=1"

    rows = parse_onesite_workflowstartup(body, url)

    assert [(row["unit_number"], row["market_rent_low"]) for row in rows] == [("52", 1263)]
    assert {row["source_property_id"] for row in rows} == {"5272798"}


def test_pid14295_priced_plans_without_native_unitids_do_not_become_units() -> None:
    body = {
        "Workflow": {
            "SiteId": "1101338",
            "ActivityGroups": [
                {
                    "GroupActivities": [
                        {
                            "Floorplans": [
                                {
                                    "Id": "A1",
                                    "Name": "Alpine",
                                    "Bedrooms": 1,
                                    "Bathrooms": 1,
                                    "Squarefeet": 700,
                                    "MinPriceRange": 1500,
                                    "MaxPriceRange": 1500,
                                    "AvailableUnits": 2,
                                    "UnitIds": [],
                                }
                            ]
                        }
                    ]
                }
            ],
        }
    }
    url = "https://leasing.realpage.com/x/workflowstartup/v1/1101338/English?x=1"

    rows = parse_onesite_workflowstartup(body, url)

    assert len(rows) == 1
    assert rows[0]["unit_number"] == ""
    assert not any(row.get("unit_number") and row.get("market_rent_low", 0) > 0 for row in rows)
