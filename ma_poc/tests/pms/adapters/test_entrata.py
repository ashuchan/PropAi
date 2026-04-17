"""Phase 3 — Entrata adapter tests."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from pms.adapters.base import AdapterContext, AdapterResult
from pms.adapters.entrata import (
    EntrataAdapter,
    parse_entrata_floorplans,
    parse_entrata_widget_envelope,
)
from pms.detector import detect_pms

FIXTURES = Path(__file__).parent / "fixtures" / "entrata"


def _load_fixture(name: str) -> list[dict]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _make_ctx(api_responses: list[dict]) -> AdapterContext:
    ctx = AdapterContext(
        base_url="https://www.hackneyhouseapartments.com/",
        detected=detect_pms("https://www.hackneyhouseapartments.com/"),
        profile=None,
        expected_total_units=None,
        property_id="257356",
    )
    ctx._api_responses = api_responses  # type: ignore[attr-defined]
    return ctx


class _DummyPage:
    """Minimal mock for Playwright Page."""
    pass


@pytest.mark.asyncio
async def test_entrata_extract_happy_path() -> None:
    """Real Entrata payload (257356) produces units with rent and floor plan name."""
    responses = _load_fixture("257356.json")
    adapter = EntrataAdapter()
    ctx = _make_ctx(responses)
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert isinstance(result, AdapterResult)
    assert len(result.units) >= 10
    first = result.units[0]
    assert first["floor_plan_name"]
    assert first["rent_range"]
    assert "ENTRATA" in first["extraction_tier"]


@pytest.mark.asyncio
async def test_entrata_extract_from_stored_fixture() -> None:
    """All stored fixtures load and produce units."""
    for fixture_path in FIXTURES.glob("*.json"):
        responses = json.loads(fixture_path.read_text(encoding="utf-8"))
        adapter = EntrataAdapter()
        ctx = _make_ctx(responses)
        result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
        assert isinstance(result, AdapterResult)
        # 252511 has no floorplan data (only availability widget + ppConfig)
        if "257356" in fixture_path.name:
            assert len(result.units) > 0


@pytest.mark.asyncio
async def test_entrata_extract_returns_empty_list_on_no_data() -> None:
    """Noise-only responses produce empty units, not an exception."""
    responses = [{"url": "https://example.com/Apartments/module/widgets/", "body": {"widget_name": "directions"}}]
    adapter = EntrataAdapter()
    ctx = _make_ctx(responses)
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert result.units == []
    assert result.confidence == 0.0


def test_parse_entrata_floorplans_basic() -> None:
    """Parse a minimal Entrata floorplan list."""
    items = [
        {"id": 100, "floorplan-name": "A1", "no_of_bedroom": 1, "no_of_bathroom": 1,
         "square_footage": 750, "min_rent": "$1,500", "max_rent": "$1,800"},
    ]
    units = parse_entrata_floorplans(items, "https://test.com/widgets/")
    assert len(units) == 1
    assert units[0]["floor_plan_name"] == "A1"
    assert units[0]["bedrooms"] == "1"
    assert "$1,500" in units[0]["rent_range"]


def test_parse_entrata_widget_envelope() -> None:
    """Parse from widget_data.content.floor_plans envelope."""
    body = {
        "widget_name": "floor_plans",
        "widget_data": {
            "content": {
                "floor_plans": {
                    "floor_plans": [
                        {"id": 1, "floorplan-name": "B2", "no_of_bedroom": 2,
                         "no_of_bathroom": 2, "square_footage": 1000,
                         "min_rent": "$2,000", "max_rent": "$2,500"},
                    ]
                }
            }
        }
    }
    units = parse_entrata_widget_envelope(body, "https://test.com/widgets/")
    assert len(units) == 1
    assert units[0]["floor_plan_name"] == "B2"


def test_static_fingerprints_nonempty() -> None:
    adapter = EntrataAdapter()
    fps = adapter.static_fingerprints()
    assert len(fps) >= 1
    assert "entrata.com" in fps


def test_tier_used_label_is_pms_specific() -> None:
    items = [{"id": 1, "floorplan-name": "X", "no_of_bedroom": 1, "no_of_bathroom": 1,
              "square_footage": 500, "min_rent": "$1,000", "max_rent": "$1,000"}]
    units = parse_entrata_floorplans(items, "test")
    assert "ENTRATA" in units[0]["extraction_tier"]


def test_rent_within_sanity_range() -> None:
    """All emitted rents from real fixture are in sanity range."""
    responses = _load_fixture("257356.json")
    for resp in responses:
        body = resp.get("body")
        if isinstance(body, list) and body and isinstance(body[0], dict):
            units = parse_entrata_floorplans(body, "test")
            for u in units:
                if u["rent_range"]:
                    # Extract numeric rent from range
                    import re
                    nums = re.findall(r"\d[\d,]*", u["rent_range"])
                    for n in nums:
                        val = int(n.replace(",", ""))
                        assert 200 <= val <= 50000, f"Rent {val} out of range"
