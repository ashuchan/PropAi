"""Irvine rank-response identity and request-binding regressions."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from ma_poc.pms.adapters.irvine import (
    _RANK_URL,
    _active_fetch_irvine,
    parse_irvine_units,
)
from ma_poc.scripts.runners.jugnu import (
    _emit_v2_units_for_property,
    _format_v2_unit,
)

COMMUNITY_ID = "7d86836c-feda-4653-9a89-c6f4113ec655"


def _unit(
    property_id: int,
    floorplan_id: int,
    unit_id: str,
    building: str,
) -> dict[str, object]:
    return {
        "objectID": f"{property_id}_{floorplan_id}_{unit_id}",
        "propertyID": property_id,
        "unitID": unit_id,
        "buildingNumber": building,
        "floorplanID": str(floorplan_id),
        "floorplanUniqueID": f"{property_id}_{floorplan_id}",
        "communityIDAEM": COMMUNITY_ID,
        "communityMarketingName": "Crescent Village",
        "propertyAddress": "100 Test Street",
        "floorplanName": "Plan A",
        "floorplanBed": 1,
        "floorplanBath": 1,
        "unitSqFt": 720,
        "unitFloor": 2,
        "unitLeasePrice": [
            {"term": 12, "price": 2525, "date": "20260915"},
            {"term": 13, "price": 2475, "date": "20260915"},
        ],
        "unitEarliestAvailable": {"date": "20260915", "price": 2475},
    }


def test_property_unit_composite_prevents_master_community_collision() -> None:
    # Exact collision shape from Crescent Village: bare unit 24 belongs to
    # two separate source properties/buildings in one page-bound community.
    source = [
        {
            "units": [
                _unit(2915722, 4, "24", "04"),
                _unit(2637658, 9, "24", "01"),
            ]
        }
    ]
    request_payload = {
        "communityId": COMMUNITY_ID,
        "unitsPerFloor": 100,
        "env": "prod",
    }
    parsed = parse_irvine_units(source, _RANK_URL, request_payload)
    final = _emit_v2_units_for_property(
        [_format_v2_unit(row, datetime(2026, 8, 2, 12, 0), "231107") for row in parsed]
    )

    assert [row["unit_number"] for row in parsed] == ["24", "24"]
    assert {row["unit_name"] for row in parsed} == {"24"}
    assert {row["building"] for row in parsed} == {"04", "01"}
    assert {row["unit_id"] for row in final} == {"2915722:24", "2637658:24"}
    assert len(final) == 2
    assert {row["rent_low"] for row in final} == {2475.0}
    assert {row["rent_high"] for row in final} == {2525.0}
    assert {row["available_date"] for row in final} == {"2026-09-15"}

    by_property = {row["source_property_id"]: row for row in parsed}
    assert by_property["2915722"]["source_ids"] == {
        "irvine_unit_id": "24",
        "irvine_object_id": "2915722_4_24",
        "irvine_floorplan_id": "4",
        "irvine_floorplan_unique_id": "2915722_4",
        "irvine_property_id": "2915722",
        "irvine_community_id": COMMUNITY_ID,
    }
    assert all(row["source_property_name"] == "Crescent Village" for row in parsed)
    assert all(row["source_property_address"] == "100 Test Street" for row in parsed)
    assert all(row["source_request_payload"] == request_payload for row in parsed)


@pytest.mark.asyncio
async def test_active_fetch_records_the_exact_page_bound_request(monkeypatch: pytest.MonkeyPatch) -> None:
    source = [{"units": [_unit(2915722, 4, "24", "04")]}]
    expected_payload = {
        "communityId": COMMUNITY_ID,
        "unitsPerFloor": 100,
        "env": "prod",
    }
    seen: list[tuple[str, dict[str, object]]] = []

    class Response:
        status_code = 200

        @staticmethod
        def json() -> list[dict[str, object]]:
            return source

    def fake_post(url: str, *, json: dict[str, object], **_kwargs: object) -> Response:
        seen.append((url, json))
        return Response()

    monkeypatch.setattr("ma_poc.pms.adapters._probe.probe_post", fake_post)
    ctx = SimpleNamespace(
        base_url="https://www.irvinecompanyapartments.com/communities/crescent-village",
        fetch_result=SimpleNamespace(
            body=(f'<input id="contact_aemCommunityId" value="{COMMUNITY_ID}">').encode()
        ),
    )

    sources = await _active_fetch_irvine(None, ctx)

    assert seen == [(_RANK_URL, expected_payload)]
    assert sources == [
        {
            "url": _RANK_URL,
            "body": source,
            "request_payload": expected_payload,
        }
    ]
