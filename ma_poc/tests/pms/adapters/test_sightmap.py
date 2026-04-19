"""Phase 3 — SightMap adapter tests."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ma_poc.pms.adapters.base import AdapterContext, AdapterResult
from ma_poc.pms.adapters.sightmap import (
    SightMapAdapter,
    _is_sightmap_response,
    parse_sightmap_payload,
)
from ma_poc.pms.detector import detect_pms

FIXTURES = Path(__file__).parent / "fixtures" / "sightmap"


def _load_fixture(name: str) -> list[dict]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _make_ctx(api_responses: list[dict]) -> AdapterContext:
    ctx = AdapterContext(
        base_url="https://tour.sightmap.com/embed/12345",
        detected=detect_pms("https://tour.sightmap.com/embed/12345"),
        profile=None,
        expected_total_units=None,
        property_id="TEST",
    )
    ctx._api_responses = api_responses  # type: ignore[attr-defined]
    return ctx


class _DummyPage:
    pass


@pytest.mark.asyncio
async def test_sightmap_extract_happy_path() -> None:
    """Synthetic SightMap payload with units produces correct output."""
    responses = _load_fixture("synthetic_units.json")
    adapter = SightMapAdapter()
    ctx = _make_ctx(responses)
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert isinstance(result, AdapterResult)
    assert len(result.units) == 4
    # Check floor plan join worked
    unit_101 = [u for u in result.units if u["unit_number"] == "101"][0]
    assert unit_101["floor_plan_name"] == "A1"
    assert unit_101["bedrooms"] == "1"
    assert "$1,500" in unit_101["rent_range"]
    assert unit_101["availability_status"] == "AVAILABLE"


@pytest.mark.asyncio
async def test_sightmap_extract_from_stored_fixture() -> None:
    """All stored fixtures load without error."""
    for fixture_path in FIXTURES.glob("*.json"):
        responses = json.loads(fixture_path.read_text(encoding="utf-8"))
        adapter = SightMapAdapter()
        ctx = _make_ctx(responses)
        result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
        assert isinstance(result, AdapterResult)


@pytest.mark.asyncio
async def test_sightmap_extract_real_fixture_268836() -> None:
    """Real SightMap payload (268836 Hawthorne) produces units."""
    responses = _load_fixture("268836_amenities_only.json")
    adapter = SightMapAdapter()
    ctx = _make_ctx(responses)
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert len(result.units) > 0
    assert result.confidence > 0


@pytest.mark.asyncio
async def test_sightmap_extract_returns_empty_on_no_data() -> None:
    """Response with no units key returns empty."""
    responses = [{"url": "https://sightmap.com/app/api/v1/x/sightmaps/1",
                  "body": {"data": {"amenities": []}}}]
    adapter = SightMapAdapter()
    ctx = _make_ctx(responses)
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert result.units == []
    assert result.confidence == 0.0


def test_parse_sightmap_handles_null_units() -> None:
    """Null units list returns empty."""
    body = {"data": {"units": None, "floor_plans": []}}
    assert parse_sightmap_payload(body, "test") == []


def test_parse_sightmap_handles_empty_units() -> None:
    body = {"data": {"units": [], "floor_plans": []}}
    assert parse_sightmap_payload(body, "test") == []


def test_parse_sightmap_studio_detection() -> None:
    """Studio floor plans (bedroom_count=0) get correct bed_label."""
    responses = _load_fixture("synthetic_units.json")
    body = responses[0]["body"]
    units = parse_sightmap_payload(body, "test")
    studio = [u for u in units if u["unit_number"] == "301"][0]
    assert studio["bed_label"] == "Studio"


def test_static_fingerprints_nonempty() -> None:
    assert SightMapAdapter().static_fingerprints()


def test_tier_used_label_is_pms_specific() -> None:
    responses = _load_fixture("synthetic_units.json")
    body = responses[0]["body"]
    units = parse_sightmap_payload(body, "test")
    assert all("SIGHTMAP" in u["extraction_tier"] for u in units)


def test_rent_within_sanity_range() -> None:
    responses = _load_fixture("synthetic_units.json")
    body = responses[0]["body"]
    units = parse_sightmap_payload(body, "test")
    import re
    for u in units:
        if u["rent_range"]:
            nums = re.findall(r"\d[\d,]*", u["rent_range"])
            for n in nums:
                val = int(n.replace(",", ""))
                assert 200 <= val <= 50000


def test_parse_sightmap_display_price_fallback() -> None:
    """When price is null, falls back to display_price."""
    body = {
        "data": {
            "units": [{"id": "1", "floor_plan_id": "1", "unit_number": "X1",
                        "price": None, "display_price": "$1,300", "area": 600}],
            "floor_plans": [{"id": "1", "name": "Test", "bedroom_count": 1, "bathroom_count": 1}],
        }
    }
    units = parse_sightmap_payload(body, "test")
    assert len(units) == 1
    assert "$1,300" in units[0]["rent_range"]


# --- 2026-04-19 fix tests (SM_T01 – SM_T08) ---------------------------------


def _sm_valid_body() -> dict:
    return {
        "data": {
            "units": [{
                "floor_plan_id": 1, "price": 1800, "unit_number": "101",
                "area": 720, "available_on": "2026-05-01",
            }],
            "floor_plans": [{
                "id": 1, "name": "1BR", "bedroom_count": 1, "bathroom_count": 1,
            }],
        }
    }


@pytest.mark.asyncio
async def test_sm_t01_sightmap_url_still_works() -> None:
    """SM_T01: sightmap.com URL + valid body still extracts units."""
    body = _sm_valid_body()
    assert _is_sightmap_response(body) is True
    responses = [{
        "url": "https://sightmap.com/app/api/v1/abc/sightmaps/123",
        "body": body,
    }]
    adapter = SightMapAdapter()
    ctx = _make_ctx(responses)
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert len(result.units) == 1
    u = result.units[0]
    assert u["bedrooms"] == "1"
    assert u["rent_range"]
    assert u["unit_number"] == "101"


@pytest.mark.asyncio
async def test_sm_t02_proxied_url_is_matched() -> None:
    """SM_T02: proxied URL (no sightmap.com) + valid body is matched."""
    body = _sm_valid_body()
    assert _is_sightmap_response(body) is True
    responses = [{
        "url": "https://lasvegasliving.com/api/properties/123/availability",
        "body": body,
    }]
    adapter = SightMapAdapter()
    ctx = _make_ctx(responses)
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert len(result.units) == 1


@pytest.mark.asyncio
async def test_sm_t03_amenities_only_error() -> None:
    """SM_T03: amenities-only response produces SIGHTMAP_AMENITIES_ONLY error."""
    responses = [{
        "url": "https://sightmap.com/app/api/v1/abc/sightmaps/456",
        "body": {"data": {"amenities": [{"id": 1, "name": "Pool"}],
                          "floor_plans": [], "units": []}},
    }]
    adapter = SightMapAdapter()
    ctx = _make_ctx(responses)
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert result.units == []
    assert any(e.startswith("SIGHTMAP_AMENITIES_ONLY") for e in result.errors)


@pytest.mark.asyncio
async def test_sm_t04_no_sightmap_response_error() -> None:
    """SM_T04: no SightMap-shaped response produces SIGHTMAP_NO_RESPONSE."""
    responses = [{"url": "https://example.com/api/other", "body": {"foo": "bar"}}]
    adapter = SightMapAdapter()
    ctx = _make_ctx(responses)
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert result.units == []
    assert any(e.startswith("SIGHTMAP_NO_RESPONSE") for e in result.errors)


@pytest.mark.asyncio
async def test_sm_t05_units_but_empty_fps_parse_failed() -> None:
    """SM_T05: units[] present but floor_plans[] empty → SIGHTMAP_PARSE_FAILED."""
    responses = [{
        "url": "https://sightmap.com/app/api/v1/x/sightmaps/1",
        "body": {"data": {"units": [{"floor_plan_id": 99, "price": 1500}],
                          "floor_plans": []}},
    }]
    adapter = SightMapAdapter()
    ctx = _make_ctx(responses)
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert result.units == []
    assert any(e.startswith("SIGHTMAP_PARSE_FAILED") for e in result.errors)


def test_sm_t06_is_sightmap_response_rejects_non_sightmap_body() -> None:
    """SM_T06: _is_sightmap_response rejects non-SightMap body."""
    assert _is_sightmap_response({"floorplanName": "1BR", "minimumRent": "1800"}) is False


def test_sm_t07_is_sightmap_response_matches_sightmap_id_alone() -> None:
    """SM_T07: sightmap_id alone in data is sufficient to match."""
    assert _is_sightmap_response({"data": {"sightmap_id": 80671, "other_stuff": []}}) is True


@pytest.mark.asyncio
async def test_sm_t08_unmatched_floor_plan_join_fails() -> None:
    """SM_T08: unit without matching floor plan → 0 units + SIGHTMAP_PARSE_FAILED."""
    responses = [{
        "url": "https://sightmap.com/app/api/v1/x/sightmaps/9",
        "body": {"data": {
            "units": [{"floor_plan_id": 999, "price": 2000,
                        "unit_number": "A1", "area": 800}],
            "floor_plans": [],
        }},
    }]
    adapter = SightMapAdapter()
    ctx = _make_ctx(responses)
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert result.units == []
    assert any(e.startswith("SIGHTMAP_PARSE_FAILED") for e in result.errors)
