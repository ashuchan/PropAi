"""Current AMLI query-identity, field, and source-to-adapter regressions."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from ma_poc.pms.adapters.amli import (
    AmliAdapter,
    _floorplan_arrays,
    parse_amli_floor_plans,
)
from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.detector import detect_pms


def _floorplan(
    *,
    slug: str = "amli-target",
    amli_property_id: int = 111,
    prismic_property_id: str = "prismic-target",
    unit_id: int = 9001,
    unit_number: str = "101",
) -> dict:
    return {
        "floorplanName": "S1",
        "floorplanId": 501,
        "propertyId": amli_property_id,
        "entrataPropertyId": 701,
        "bedroomMax": 0,
        "bedroomMin": 0,
        "bathroomMax": 1,
        "sqftMin": 600,
        "cms": {
            "id": "plan-doc",
            "data": {
                "properties": [
                    {
                        "property": {
                            "id": prismic_property_id,
                            "uid": slug,
                        }
                    }
                ]
            },
        },
        "units": [
            {
                "unitId": unit_id,
                "engrainUnitId": f"engrain-{unit_id}",
                "entrataUnitId": 801,
                "unitNumber": unit_number,
                "buildingNumber": "Building A",
                "floor": "1",
                "squareFeet": 625,
                "rent": 1900,
                "rpAvailableDate": "2026-09-15",
            }
        ],
    }


def _query(amli_id: int, prismic_id: str, floorplans: list[dict]) -> dict:
    return {
        "queryKey": [
            ["amli", "floorplans"],
            {
                "input": {
                    "amliPropertyId": amli_id,
                    "propertyId": prismic_id,
                },
                "type": "query",
            },
        ],
        "state": {"data": floorplans},
    }


def _next_data(queries: list[dict]) -> dict:
    return {
        "buildId": "build-1",
        "props": {
            "pageProps": {
                "trpcState": {"json": {"queries": queries}}
            }
        },
    }


def _context(next_data: dict) -> AdapterContext:
    html = (
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(next_data)
        + "</script>"
    )
    url = "https://www.amli.com/apartments/texas/austin/amli-target"
    return AdapterContext(
        base_url=url,
        detected=detect_pms(url),
        profile=None,
        expected_total_units=None,
        property_id="property-1",
        property_name="AMLI Target",
        fetch_result=SimpleNamespace(body=html.encode(), final_url=url),
    )


def test_floorplan_arrays_reads_current_props_pageprops_root() -> None:
    payload = _next_data([_query(111, "prismic-target", [_floorplan()])])

    arrays = _floorplan_arrays(payload)

    assert len(arrays) == 1
    assert arrays[0][0]["floorplanName"] == "S1"


def test_query_bound_parser_preserves_native_identity_and_unit_fields() -> None:
    [row] = parse_amli_floor_plans(
        [_floorplan()],
        "amli-target",
        "https://www.amli.com/target",
        query_identity={
            "amli_property_id": "111",
            "prismic_property_id": "prismic-target",
        },
    )

    assert row["bedrooms"] == "0"
    assert row["bathrooms"] == "1"
    assert row["sqft"] == "625"
    assert row["unit_number"] == "101"
    assert row["unit_name"] == "101"
    assert row["building"] == "Building A"
    assert row["availability_date"] == "2026-09-15"
    assert row["source_ids"] == {
        "amli_unit_id": "9001",
        "amli_engrain_unit_id": "engrain-9001",
        "amli_entrata_unit_id": "801",
        "amli_floor_plan_id": "501",
        "amli_property_id": "111",
        "amli_prismic_property_id": "prismic-target",
        "amli_entrata_property_id": "701",
    }
    assert row["unit_id"] == "9001"


def test_query_bound_parser_rejects_contradictory_floorplan_property_id() -> None:
    assert parse_amli_floor_plans(
        [_floorplan(amli_property_id=222)],
        "amli-target",
        "https://www.amli.com/target",
        query_identity={
            "amli_property_id": "111",
            "prismic_property_id": "prismic-target",
        },
    ) == []


@pytest.mark.asyncio
async def test_adapter_uses_exact_floorplans_query_not_floorplan_highlights() -> None:
    target = _floorplan()
    highlight = _floorplan(unit_id=9999, unit_number="HIGHLIGHT")
    highlight_query = {
        "queryKey": [["amli", "floorPlanHighlights"], {"input": {"id": 111}}],
        "state": {"data": [highlight]},
    }
    context = _context(
        _next_data([highlight_query, _query(111, "prismic-target", [target])])
    )

    result = await AmliAdapter().extract(SimpleNamespace(context=None), context)

    assert len(result.units) == 1
    assert result.units[0]["source_ids"]["amli_unit_id"] == "9001"
    assert result.units[0]["unit_number"] == "101"
    assert result.unit_source_provenance[0]["identity"]["status"] == "MATCH"
    assert result.unit_source_provenance[0]["identity"]["amli_property_id"] == "111"


@pytest.mark.asyncio
async def test_adapter_fails_closed_on_two_contradictory_target_queries() -> None:
    first = _query(111, "prismic-target", [_floorplan()])
    second = _query(
        222,
        "prismic-sibling",
        [
            _floorplan(
                amli_property_id=222,
                prismic_property_id="prismic-sibling",
                unit_id=9999,
            )
        ],
    )
    # Both arrays claim the configured slug, so slug alone cannot decide.
    context = _context(_next_data([first, second]))

    result = await AmliAdapter().extract(SimpleNamespace(context=None), context)

    assert result.units == []
    assert "AMLI: contradictory property-bound floorplan queries" in result.errors
