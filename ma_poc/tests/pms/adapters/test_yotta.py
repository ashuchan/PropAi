from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from ma_poc.discovery.contracts import CrawlTask, TaskReason
from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode
from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.adapters.yotta import (
    YottaAdapter,
    extract_yotta_dba_id,
    parse_yotta_units,
    yotta_property_identity_matches,
)
from ma_poc.pms.detector import DetectedPMS, detect_pms
from ma_poc.pms.scraper import scrape_jugnu
from ma_poc.scripts.runners.jugnu import _format_v2_unit


def _ctx(**overrides: object) -> AdapterContext:
    values: dict[str, object] = {
        "base_url": "https://adaraportal.yottareal.com/pages/HomePage.aspx?Id=55",
        "detected": DetectedPMS(
            pms="yotta",
            confidence=0.95,
            evidence=["test"],
            recommended_strategy="api_first",
        ),
        "profile": None,
        "expected_total_units": None,
        "property_id": "34785",
        "fetch_result": SimpleNamespace(
            body=b"<html></html>",
            final_url=(
                "https://adaraportal.yottareal.com/pages/HomePage.aspx?Id=55"
            ),
        ),
        "property_name": "Pepper Tree",
        "address": "2701 Longmire Dr",
        "city": "College Station",
        "state": "TX",
        "zip_code": "77845",
    }
    values.update(overrides)
    return AdapterContext(**values)  # type: ignore[arg-type]


def _details(**overrides: object) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "dbaId": 55,
        "dbaName": "Pepper Tree Apartments",
        "address1": "2701",
        "address2": "Longmire Drive",
        "city": "College Station",
        "stateCode": "TX",
        "zip": "77845",
    }
    payload.update(overrides)
    return payload


def _units_payload() -> dict[str, Any]:
    return {
        "hotSheetUnitsModel": [
            {
                "unitId": 9795,
                "unitNumber": "0215",
                "rent": 905.0,
                "MoveInDateAvailable": "2026-10-03T00:00:00",
                "dbaUnitType": "One Bed / One Bath",
                "dbaUnitTypeId": 390,
                "dbaUnitTypeCode": "11MA",
                "bedRooms": "1",
                "bathRooms": "1",
                "squareFeet": 567.0,
                "floorLevel": "Second Floor",
            },
            {
                "unitId": 9892,
                "unitNumber": "1016",
                "rent": 983,
                "MoveInDateAvailable": "2026-08-08T00:00:00",
                "dateAvailable": "Today",
                "dbaUnitType": "One Bed / One Bath",
                "dbaUnitTypeId": 391,
                "dbaUnitTypeCode": "11MB",
                "bedRooms": "1",
                "bathRooms": "1",
                "squareFeet": 653,
                "floorLevel": "First Floor",
            },
        ]
    }


@dataclass
class _Response:
    payload: dict[str, Any]
    status_code: int = 200

    def json(self) -> dict[str, Any]:
        return self.payload


def test_extract_yotta_dba_id_from_supported_exact_routes() -> None:
    assert (
        extract_yotta_dba_id(
            "https://adaraportal.yottareal.com/pages/HomePage.aspx?Id=55"
        )
        == "55"
    )
    assert (
        extract_yotta_dba_id(
            "https://adaraportal.yottareal.com/dba/floorplans?dbaid=58"
        )
        == "58"
    )
    assert (
        extract_yotta_dba_id(
            "https://residentapis.yottareal.com/api/DBA/GetFloorPlans/57/1"
        )
        == "57"
    )


def test_extract_yotta_dba_id_fails_closed_for_foreign_or_ambiguous_routes() -> None:
    assert not extract_yotta_dba_id("https://example.com/?Id=55")
    assert not extract_yotta_dba_id(
        "https://adaraportal.yottareal.com/?Id=55",
        "https://adaraportal.yottareal.com/?Id=58",
    )


def test_yotta_property_identity_requires_full_exact_property_boundary() -> None:
    assert yotta_property_identity_matches(_details(), _ctx(), "55")
    assert not yotta_property_identity_matches(
        _details(dbaName="Hearthstone"), _ctx(), "55"
    )
    assert not yotta_property_identity_matches(
        _details(address1="8801", address2="Huebner Road"), _ctx(), "55"
    )
    assert not yotta_property_identity_matches(_details(), _ctx(), "58")


def test_parse_yotta_units_preserves_native_ids_rent_dates_and_property() -> None:
    source_url = (
        "https://residentapis.yottareal.com/api/DBA/GetFloorPlans/55/1"
    )
    units = parse_yotta_units(_units_payload(), dba_id="55", source_url=source_url)
    assert len(units) == 2
    assert units[0]["unit_number"] == "0215"
    assert units[0]["source_ids"] == {
        "yotta_dba_id": "55",
        "yotta_floor_plan_code": "11MA",
        "yotta_floor_plan_id": "390",
        "yotta_unit_id": "9795",
    }
    assert units[0]["source_property_id"] == "55"
    assert units[0]["floor_plan_name"] == "11MA"
    assert units[0]["floor_plan_description"] == "One Bed / One Bath"
    assert units[0]["_canonical_floor_plan_id"]
    assert units[0]["market_rent_low"] == 905
    assert units[0]["availability_date"] == "2026-10-03"
    assert units[1]["availability_date"] == "Today"
    assert units[0]["_canonical_floor_plan_id"] != units[1]["_canonical_floor_plan_id"]
    assert units[0]["source_api_url"] == source_url


def test_yotta_formatter_preserves_provider_plan_floor_and_today_semantics() -> None:
    source_url = "https://residentapis.yottareal.com/api/DBA/GetFloorPlans/55/1"
    rows = parse_yotta_units(_units_payload(), dba_id="55", source_url=source_url)
    capture_ts = datetime(2026, 8, 2, 15, 30, tzinfo=UTC)
    formatted = [_format_v2_unit(row, capture_ts, "34785") for row in rows]

    assert [row["floor_plan_name"] for row in formatted] == ["11MA", "11MB"]
    assert len({row["floor_plan_id"] for row in formatted}) == 2
    assert [row["floor_plan_description"] for row in formatted] == [
        "One Bed / One Bath",
        "One Bed / One Bath",
    ]
    assert [row["floor"] for row in formatted] == [2, 1]
    assert [row["floor_raw"] for row in formatted] == [
        "Second Floor",
        "First Floor",
    ]
    assert formatted[0]["available_date"] == "2026-10-03"
    assert formatted[0]["availability_date_provenance"] == "explicit_future"
    assert formatted[1]["available_date"] == "2026-08-02"
    assert formatted[1]["available_date_raw"] == "Today"
    assert formatted[1]["availability_date_provenance"] == "available_now"


def test_parse_yotta_units_rejects_missing_native_id_or_positive_rent() -> None:
    payload = _units_payload()
    payload["hotSheetUnitsModel"].extend(
        [
            {"unitId": "", "unitNumber": "200", "rent": 1000},
            {"unitId": 3, "unitNumber": "201", "rent": 0},
            {"unitId": 4, "unitNumber": "", "rent": 1000},
        ]
    )
    assert len(parse_yotta_units(payload, dba_id="55", source_url="https://x")) == 2


def test_detector_and_registry_route_yottareal_to_yotta() -> None:
    result = detect_pms(
        "https://adaraportal.yottareal.com/pages/HomePage.aspx?Id=55"
    )
    assert result.pms == "yotta"
    assert result.confidence == 0.95
    assert result.recommended_strategy == "api_first"

    from ma_poc.pms.adapters.registry import get_adapter

    assert get_adapter("yotta").pms_name == "yotta"


@pytest.mark.asyncio
async def test_yotta_adapter_full_property_scoped_api_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_probe_get(url: str, **_kwargs: object) -> _Response:
        calls.append(url)
        if "GetDBADetails" in url:
            return _Response(_details())
        if "GetFloorPlans" in url:
            return _Response(_units_payload())
        raise AssertionError(url)

    monkeypatch.setattr("ma_poc.pms.adapters.yotta.probe_get", fake_probe_get)
    result = await YottaAdapter().extract(None, _ctx())  # type: ignore[arg-type]

    assert result.tier_used == "TIER_1_API_YOTTA"
    assert len(result.units) == 2
    assert result.winning_url and result.winning_url.endswith("GetFloorPlans/55/1")
    assert result.units[0]["source_ids"]["yotta_unit_id"] == "9795"
    assert result.units[0]["source_ids"]["yotta_dba_id"] == "55"
    assert result.units[0]["source_ids"]["yotta_floor_plan_id"] == "390"
    assert result.units[0]["source_property_id"] == "55"
    assert len(result.unit_source_provenance) == 1
    provenance = result.unit_source_provenance[0]
    assert provenance["provider"] == "yotta"
    assert provenance["response_kind"] == "available_unit_roster"
    assert provenance["source_url"].endswith("GetFloorPlans/55/1")
    assert provenance["unit_count"] == 2
    assert provenance["identity"]["status"] == "MATCH"
    assert provenance["identity"]["source_count"] == 2
    assert provenance["identity"]["admitted_count"] == 2
    assert len(provenance["response_sha256"]) == 64
    assert calls == [
        "https://residentapis.yottareal.com/api/DBA/GetDBADetails/55",
        "https://residentapis.yottareal.com/api/DBA/GetFloorPlans/55/1",
    ]


@pytest.mark.asyncio
async def test_yotta_adapter_rejects_sibling_before_units_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_probe_get(url: str, **_kwargs: object) -> _Response:
        calls.append(url)
        return _Response(_details(dbaName="Hearthstone", dbaId=57))

    monkeypatch.setattr("ma_poc.pms.adapters.yotta.probe_get", fake_probe_get)
    result = await YottaAdapter().extract(None, _ctx())  # type: ignore[arg-type]

    assert not result.units
    assert result.tier_used.endswith("PROPERTY_IDENTITY_REJECTED")
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_yotta_configured_route_full_scraper_e2e(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_probe_get(url: str, **_kwargs: object) -> _Response:
        if "GetDBADetails" in url:
            return _Response(_details())
        if "GetFloorPlans" in url:
            return _Response(_units_payload())
        raise AssertionError(url)

    monkeypatch.setattr("ma_poc.pms.adapters.yotta.probe_get", fake_probe_get)
    configured_url = "http://adaraportal.yottareal.com/pages/HomePage.aspx?Id=55"
    normalized_url = configured_url.replace("http://", "https://", 1)
    fetch_result = FetchResult(
        url=configured_url,
        outcome=FetchOutcome.OK,
        status=200,
        body=b"<html><title>Luxury Apartment Homes</title></html>",
        headers={},
        render_mode=RenderMode.GET,
        final_url=normalized_url,
        attempts=1,
        elapsed_ms=0,
    )
    task = CrawlTask(
        url=configured_url,
        property_id="34785",
        priority=0,
        budget_ms=30_000,
        reason=TaskReason.MANUAL,
        render_mode=RenderMode.GET,
    )
    result = await scrape_jugnu(
        task,
        fetch_result,
        page=None,
        profile=None,
        csv_row={
            "apartmentid": "34785",
            "name": "Pepper Tree",
            "address": "2701 Longmire Dr",
            "city": "College Station",
            "state": "TX",
            "zip": "77845",
            "website": configured_url,
        },
    )

    assert result["_adapter_used"] == "yotta"
    assert result["extraction_tier_used"] == "TIER_1_API_YOTTA"
    assert len(result["units"]) == 2
    assert {unit["source_property_id"] for unit in result["units"]} == {"55"}
    assert len(result["_unit_source_provenance"]) == 1
