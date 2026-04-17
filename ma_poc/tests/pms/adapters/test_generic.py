"""Phase 3 — Generic adapter tests."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from pms.adapters.base import AdapterContext, AdapterResult
from pms.adapters.generic import GenericAdapter, parse_generic_api, _find_unit_list
from pms.detector import detect_pms

FIXTURES = Path(__file__).parent / "fixtures" / "generic"


def _load_fixture(name: str) -> list[dict]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _make_ctx(api_responses: list[dict], pms: str = "unknown") -> AdapterContext:
    ctx = AdapterContext(
        base_url="https://example.com/",
        detected=detect_pms("https://example.com/"),
        profile=None,
        expected_total_units=None,
        property_id="TEST",
    )
    ctx._api_responses = api_responses  # type: ignore[attr-defined]
    return ctx


class _DummyPage:
    pass


@pytest.mark.asyncio
async def test_generic_extract_happy_path() -> None:
    responses = _load_fixture("synthetic_units.json")
    adapter = GenericAdapter()
    ctx = _make_ctx(responses)
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert isinstance(result, AdapterResult)
    assert len(result.units) == 2
    assert result.units[0]["floor_plan_name"] == "1BR"
    assert result.units[0]["rent_range"]


@pytest.mark.asyncio
async def test_generic_extract_from_stored_fixture() -> None:
    for fixture_path in FIXTURES.glob("*.json"):
        responses = json.loads(fixture_path.read_text(encoding="utf-8"))
        adapter = GenericAdapter()
        ctx = _make_ctx(responses)
        result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
        assert isinstance(result, AdapterResult)
        assert len(result.units) >= 1


@pytest.mark.asyncio
async def test_generic_extract_returns_empty_on_no_data() -> None:
    responses = [{"url": "https://example.com/api", "body": {"config": True}}]
    adapter = GenericAdapter()
    ctx = _make_ctx(responses)
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert result.units == []
    assert result.confidence == 0.0


@pytest.mark.asyncio
async def test_generic_skips_llm_for_detected_pms() -> None:
    """When pms != 'unknown', error message mentions LLM skipped."""
    responses = [{"url": "https://example.com/api", "body": {"config": True}}]
    ctx = AdapterContext(
        base_url="https://www.rentcafe.com/test/",
        detected=detect_pms("https://www.rentcafe.com/test/"),
        profile=None,
        expected_total_units=None,
        property_id="TEST",
    )
    ctx._api_responses = responses  # type: ignore[attr-defined]
    adapter = GenericAdapter()
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert any("LLM/Vision skipped" in e for e in result.errors)


def test_find_unit_list_direct_list() -> None:
    body = [{"name": "A", "minRent": 1000}]
    assert _find_unit_list(body) == body


def test_find_unit_list_nested() -> None:
    body = {"data": {"results": [{"name": "A", "minRent": 1000}]}}
    items = _find_unit_list(body)
    assert len(items) == 1


def test_find_unit_list_empty() -> None:
    assert _find_unit_list({"config": True}) == []
    assert _find_unit_list(None) == []
    assert _find_unit_list("string") == []


def test_parse_generic_api_dedup() -> None:
    """Duplicate items are deduplicated by unit_number."""
    items = [
        {"unitNumber": "101", "name": "A1", "bedrooms": 1, "minRent": 1500},
        {"unitNumber": "101", "name": "A1", "bedrooms": 1, "minRent": 1500},  # dupe
    ]
    units = parse_generic_api(items, "test")
    assert len(units) == 1


def test_parse_generic_api_rent_sanity() -> None:
    """Rents outside $200-$50,000 are filtered."""
    items = [
        {"name": "Valid", "bedrooms": 1, "minRent": 1500},
        {"name": "TooLow", "bedrooms": 1, "minRent": 14},  # rent=14 is garbage
    ]
    units = parse_generic_api(items, "test")
    assert len(units) == 1
    assert units[0]["floor_plan_name"] == "Valid"


def test_static_fingerprints_empty() -> None:
    """Generic adapter has no fingerprints (it's the catch-all)."""
    assert GenericAdapter().static_fingerprints() == []


def test_parse_generic_nested_envelope() -> None:
    responses = _load_fixture("nested_envelope.json")
    body = responses[0]["body"]
    items = _find_unit_list(body)
    assert len(items) == 2
    units = parse_generic_api(items, "test")
    assert len(units) == 2
    studio = [u for u in units if u["floor_plan_name"] == "Studio"][0]
    assert studio["bed_label"] == "Studio"
