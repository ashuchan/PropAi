"""Phase 3 — SightMap adapter tests."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from pms.adapters.base import AdapterContext, AdapterResult
from pms.adapters.sightmap import SightMapAdapter, parse_sightmap_payload
from pms.detector import detect_pms

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
