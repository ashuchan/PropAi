"""Phase 3 — RentCafe adapter tests."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from pms.adapters.base import AdapterContext, AdapterResult
from pms.adapters.rentcafe import (
    RentCafeAdapter,
    _is_rentcafe_response,
    parse_rentcafe_floorplans,
)
from pms.detector import detect_pms

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
