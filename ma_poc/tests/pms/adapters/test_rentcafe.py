"""Phase 3 — RentCafe adapter tests."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ma_poc.pms.adapters.base import AdapterContext, AdapterResult
from ma_poc.pms.adapters.rentcafe import (
    RentCafeAdapter,
    _is_rentcafe_response,
    _unwrap_rentcafe_list,
    parse_rentcafe_floorplans,
)
from ma_poc.pms.detector import detect_pms

FIXTURES = Path(__file__).parent / "fixtures" / "rentcafe"


def _load_fixture(name: str) -> list[dict]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _make_ctx(api_responses: list[dict]) -> AdapterContext:
    ctx = AdapterContext(
        base_url="https://www.rentcafe.com/apartments/test/",
        detected=detect_pms("https://www.rentcafe.com/apartments/test/"),
        profile=None,
        expected_total_units=None,
        property_id="35593",
    )
    ctx._api_responses = api_responses  # type: ignore[attr-defined]
    return ctx


class _DummyPage:
    pass


@pytest.mark.asyncio
async def test_rentcafe_extract_happy_path() -> None:
    """Real RentCafe payload produces units with correct fields."""
    responses = _load_fixture("35593.json")
    adapter = RentCafeAdapter()
    ctx = _make_ctx(responses)
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert isinstance(result, AdapterResult)
    assert len(result.units) >= 5
    first = result.units[0]
    assert first["floor_plan_name"]
    assert first["rent_range"]
    assert "RENTCAFE" in first["extraction_tier"]


@pytest.mark.asyncio
async def test_rentcafe_extract_from_stored_fixture() -> None:
    for fixture_path in FIXTURES.glob("*.json"):
        responses = json.loads(fixture_path.read_text(encoding="utf-8"))
        adapter = RentCafeAdapter()
        ctx = _make_ctx(responses)
        result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
        assert isinstance(result, AdapterResult)
        assert len(result.units) > 0


@pytest.mark.asyncio
async def test_rentcafe_extract_returns_empty_on_no_data() -> None:
    responses = [{"url": "https://example.com/api", "body": {"some": "data"}}]
    adapter = RentCafeAdapter()
    ctx = _make_ctx(responses)
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert result.units == []
    assert result.confidence == 0.0


def test_is_rentcafe_response_positive() -> None:
    body = [{"floorplanName": "A1", "api": "rentcafe", "minimumRent": "1000.00"}]
    assert _is_rentcafe_response(body)


def test_is_rentcafe_response_negative() -> None:
    assert not _is_rentcafe_response({"some": "dict"})
    assert not _is_rentcafe_response([])
    assert not _is_rentcafe_response(None)
    assert not _is_rentcafe_response([{"random": "keys"}])


@pytest.mark.asyncio
async def test_rentcafe_extracts_from_dict_wrapped_payload() -> None:
    """Yardi-style ``{"data": [...]}`` wrappers must extract the same units.

    Regression: 12 of 13 RentCafe NO_DATA properties in the 2026-04-19 run
    (Windsor Communities, Brookfield, etc.) shipped the floorplan list
    inside a ``data``/``Result`` wrapper. The original matcher only saw
    root-level lists and silently rejected them.
    """
    item = {
        "floorplanName": "A1",
        "api": "rentcafe",
        "beds": "1",
        "baths": "1",
        "floorplanId": "42",
        "minimumRent": "1000.00",
        "maximumRent": "1200.00",
        "availableUnitsCount": "2",
    }
    for wrapper in ("data", "results", "floorplans", "Result"):
        responses = [{
            "url": "https://example.rentcafe.com/api/floorplans",
            "body": {wrapper: [item]},
        }]
        adapter = RentCafeAdapter()
        ctx = _make_ctx(responses)
        result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
        assert len(result.units) == 1, f"wrapper={wrapper!r} produced no units"
        assert result.units[0]["floor_plan_name"] == "A1"


def test_parse_rentcafe_min_max_price() -> None:
    """Prefer numeric min_price/max_price over string minimumRent/maximumRent."""
    items = [{"floorplanName": "X", "beds": "1", "baths": "1", "minimumSQFT": "700",
              "maximumSQFT": "700", "minimumRent": "1349.00", "maximumRent": "2211.00",
              "min_price": 1349, "max_price": 1349, "floorplanId": "123",
              "availableUnitsCount": "1", "availableDate": "2026-05-01"}]
    units = parse_rentcafe_floorplans(items, "test")
    assert len(units) == 1
    assert units[0]["rent_range"] == "$1,349"  # min_price == max_price
    assert units[0]["availability_status"] == "AVAILABLE"


def test_static_fingerprints_nonempty() -> None:
    assert RentCafeAdapter().static_fingerprints()


def test_tier_used_label_is_pms_specific() -> None:
    items = [{"floorplanName": "A", "beds": "1", "baths": "1", "minimumRent": "1000.00",
              "maximumRent": "1000.00", "floorplanId": "1", "availableUnitsCount": "1"}]
    units = parse_rentcafe_floorplans(items, "test")
    assert "RENTCAFE" in units[0]["extraction_tier"]


def test_rent_within_sanity_range() -> None:
    responses = _load_fixture("35593.json")
    import re
    for resp in responses:
        body = resp.get("body")
        if isinstance(body, list):
            units = parse_rentcafe_floorplans(body, "test")
            for u in units:
                if u["rent_range"]:
                    nums = re.findall(r"\d[\d,]*", u["rent_range"])
                    for n in nums:
                        val = int(n.replace(",", ""))
                        assert 200 <= val <= 50000


# --- 2026-04-19 fix tests (RC_T01 – RC_T10) ---------------------------------


def test_rc_t01_lowercase_root_list_regression_guard() -> None:
    """RC_T01: existing lowercase / research-log payload still works."""
    body = [{
        "floorplanName": "Studio",
        "floorplanId": "F1",
        "api": "rentcafe",
        "beds": "0",
        "baths": "1",
        "minimumRent": "1200.00",
        "maximumRent": "1400.00",
        "availableUnitsCount": "1",
    }]
    assert _is_rentcafe_response(body) is True
    units = parse_rentcafe_floorplans(body, "test")
    assert len(units) >= 1
    assert units[0]["floor_plan_name"] == "Studio"
    assert units[0]["rent_range"]


def test_rc_t02_pascalcase_root_list_is_fingerprinted() -> None:
    """RC_T02: PascalCase root-level list is fingerprinted correctly."""
    body = [{
        "FloorplanName": "1BR",
        "FloorplanId": "FP1",
        "MinimumRent": "2100.00",
        "MaximumRent": "2400.00",
        "AvailableUnitsCount": 2,
        "Beds": 1,
        "Baths": 1,
        "MinimumSQFT": "700",
        "MaximumSQFT": "750",
    }]
    assert _is_rentcafe_response(body) is True


def test_rc_t03_pascalcase_items_parse_to_correct_fields() -> None:
    """RC_T03: PascalCase items parse to correct field values."""
    import re

    body = [{
        "FloorplanName": "1BR",
        "FloorplanId": "FP1",
        "MinimumRent": "2100.00",
        "MaximumRent": "2400.00",
        "AvailableUnitsCount": 2,
        "Beds": 1,
        "Baths": 1,
        "MinimumSQFT": "700",
        "MaximumSQFT": "750",
    }]
    units = parse_rentcafe_floorplans(body, "test")
    assert len(units) == 1
    u = units[0]
    assert u["floor_plan_name"] == "1BR"
    assert u["bedrooms"] == "1"
    assert u["unit_number"] == "FP1"
    nums = [int(n.replace(",", "")) for n in re.findall(r"\d[\d,]*", u["rent_range"])]
    assert nums and nums[0] > 0


@pytest.mark.asyncio
async def test_rc_t04_data_wrapper_pascalcase_items() -> None:
    """RC_T04: {"data": [PascalCase items]} wrapper is unwrapped and parsed."""
    body = {"data": [{
        "FloorplanName": "Aspen",
        "FloorplanId": "A1",
        "Beds": 1,
        "MinimumRent": "2195.00",
        "MaximumRent": "2395.00",
        "AvailableUnitsCount": 3,
        "Baths": 1,
        "MinimumSQFT": "685",
        "MaximumSQFT": "695",
    }]}
    assert _is_rentcafe_response(body) is True
    responses = [{"url": "https://windsor.example.com/api", "body": body}]
    adapter = RentCafeAdapter()
    ctx = _make_ctx(responses)
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert len(result.units) == 1
    assert result.units[0]["floor_plan_name"] == "Aspen"


@pytest.mark.asyncio
async def test_rc_t05_result_wrapper_lowercase_items() -> None:
    """RC_T05: {"Result": [lowercase items]} wrapper unwraps (existing wrapper)."""
    body = {"Result": [{
        "floorplanName": "Studio",
        "floorplanId": "S1",
        "minimumRent": "1500.00",
        "availableUnitsCount": 1,
        "availabilityURL": "https://securecafe.com/...",
    }]}
    assert _is_rentcafe_response(body) is True
    responses = [{"url": "https://example.com/api", "body": body}]
    adapter = RentCafeAdapter()
    ctx = _make_ctx(responses)
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert len(result.units) == 1


def test_rc_t06_floorplans_new_wrapper_key() -> None:
    """RC_T06: {"Floorplans": [...]} new wrapper key is handled."""
    body = {"Floorplans": [{
        "floorplanName": "2BR",
        "api": "rentcafe",
        "minimumRent": "2500.00",
        "availableUnitsCount": 1,
    }]}
    assert _is_rentcafe_response(body) is True


def test_rc_t07_two_level_response_result_unwrap() -> None:
    """RC_T07: two-level {"response": {"result": [...]}} is unwrapped."""
    body = {"response": {"result": [{
        "floorplanName": "1BR",
        "api": "rentcafe",
        "minimumRent": "1800.00",
        "availableUnitsCount": 2,
    }]}}
    items = _unwrap_rentcafe_list(body)
    assert items is not None
    assert len(items) == 1
    assert _is_rentcafe_response(body) is True


def test_rc_t08_all_unavailable_floorplans_still_extracted() -> None:
    """RC_T08: availableUnitsCount==0 floorplans extract as UNAVAILABLE."""
    body = [{
        "floorplanName": "3BR",
        "floorplanId": "FP3",
        "minimumRent": "3000.00",
        "maximumRent": "3200.00",
        "availableUnitsCount": 0,
        "beds": 3,
        "baths": 2,
    }]
    units = parse_rentcafe_floorplans(body, "test")
    assert len(units) == 1
    assert units[0]["availability_status"] == "UNAVAILABLE"
    assert units[0]["rent_range"]


def test_rc_t09_non_rentcafe_body_rejected() -> None:
    """RC_T09: non-RentCafe JSON body is rejected."""
    body = {"amenities": [{"id": 1, "name": "Pool"}], "events": []}
    assert _is_rentcafe_response(body) is False


def test_rc_t10_empty_list_body_rejected() -> None:
    """RC_T10: empty list body is rejected."""
    assert _is_rentcafe_response([]) is False
    assert _unwrap_rentcafe_list([]) is None
