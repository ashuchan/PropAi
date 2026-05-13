"""Spherexx adapter tests.

Fixture captured live from henryonthepark.com/interactive-site-map/ via
Playwright on 2026-05-13. Documents the real Spherexx /api/unit and
/api/floorplan response shapes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ma_poc.pms.adapters.base import AdapterContext, AdapterResult
from ma_poc.pms.adapters.spherexx import (
    SpherexxAdapter,
    _is_spherexx_floorplan_response,
    _is_spherexx_unit_response,
    parse_spherexx_units,
)
from ma_poc.pms.detector import detect_pms

FIXTURES = Path(__file__).parent / "fixtures" / "spherexx"


def _load_fixture() -> list[dict]:
    return json.loads((FIXTURES / "henryonthepark.json").read_text(encoding="utf-8"))


def _make_ctx(api_responses: list[dict]) -> AdapterContext:
    ctx = AdapterContext(
        base_url="https://www.henryonthepark.com/interactive-site-map/",
        detected=detect_pms("https://www.henryonthepark.com/"),
        profile=None,
        expected_total_units=None,
        property_id="TEST",
    )
    ctx._api_responses = api_responses  # type: ignore[attr-defined]
    return ctx


class _DummyPage:
    pass


# ── Body-shape detection ──────────────────────────────────────────────


def test_is_spherexx_unit_response_matches_real_shape() -> None:
    """Live-captured /api/unit body should pass the shape check."""
    fix = _load_fixture()
    unit_resp = next(r for r in fix if r["url"].endswith("/api/unit"))
    assert _is_spherexx_unit_response(unit_resp["body"])


def test_is_spherexx_unit_response_rejects_floorplan_shape() -> None:
    """/api/floorplan has Bed/Bath but uses MinSqFt/MaxSqFt instead of Sqft —
    must be rejected by the /api/unit shape check.
    """
    fix = _load_fixture()
    fp_resp = next(r for r in fix if r["url"].endswith("/api/floorplan"))
    assert not _is_spherexx_unit_response(fp_resp["body"])


def test_is_spherexx_unit_response_rejects_empty_list() -> None:
    assert not _is_spherexx_unit_response([])
    assert not _is_spherexx_unit_response(None)
    assert not _is_spherexx_unit_response({"data": []})


def test_is_spherexx_unit_response_rejects_unrelated_json() -> None:
    """SightMap/RentCafe/AppFolio response shapes must not falsely match."""
    sightmap_like = {"data": {"units": [], "floor_plans": []}}
    rentcafe_like = [{"propertyId": 1, "floorplanId": 2}]
    appfolio_like = [{"unit_id": "x", "rent": 1500, "available_date": "2026-01"}]
    assert not _is_spherexx_unit_response(sightmap_like)
    assert not _is_spherexx_unit_response(rentcafe_like)
    assert not _is_spherexx_unit_response(appfolio_like)


def test_is_spherexx_floorplan_response_matches_real_shape() -> None:
    fix = _load_fixture()
    fp_resp = next(r for r in fix if r["url"].endswith("/api/floorplan"))
    assert _is_spherexx_floorplan_response(fp_resp["body"])


# ── Unit parsing ──────────────────────────────────────────────


def test_parse_spherexx_units_real_fixture() -> None:
    """Parse the live henryonthepark /api/unit fixture into our schema."""
    fix = _load_fixture()
    unit_resp = next(r for r in fix if r["url"].endswith("/api/unit"))
    units = parse_spherexx_units(unit_resp["body"], unit_resp["url"])
    assert units, "expected ≥1 unit parsed from live fixture"
    u0 = units[0]
    # Schema spot-check
    assert u0["unit_number"]  # e.g. "B102"
    assert u0["bedrooms"]  # numeric string
    assert u0["bathrooms"]
    assert u0["sqft"]
    assert u0["rent_range"].startswith("$")
    assert u0["market_rent_low"]
    assert u0["floor_plan_name"]
    assert u0["availability_status"] == "AVAILABLE"
    assert u0["extraction_tier"] == "TIER_1_API_SPHEREXX"


def test_parse_spherexx_units_skips_empty_placeholders() -> None:
    """Units with no Price AND no Sqft (unbuilt buildings) are skipped."""
    body = [
        {"ID": 1, "Name": "X", "Sqft": 0, "Bed": 0, "Bath": 0,
         "Price": 0, "PriceMin": 0, "PriceMax": 0, "FloorplanID": 99,
         "FloorplanName": "TBD", "AvailableDate": None},
    ]
    assert parse_spherexx_units(body, "test") == []


def test_parse_spherexx_units_handles_price_range() -> None:
    """PriceMin/PriceMax both present + different → rent_range shows both."""
    body = [{
        "ID": 1, "Name": "A101", "Sqft": 750, "Bed": 1.0, "Bath": 1.0,
        "Price": 1500.0, "PriceMin": 1500.0, "PriceMax": 1800.0,
        "FloorplanID": 1, "FloorplanName": "Olive",
        "AvailableDate": "2026-06-29T00:00:00", "Building": "A", "Floor": "1",
    }]
    units = parse_spherexx_units(body, "test")
    assert len(units) == 1
    u = units[0]
    assert u["market_rent_low"] == 1500
    assert u["market_rent_high"] == 1800
    assert "$1,500" in u["rent_range"]
    assert "$1,800" in u["rent_range"]
    assert u["availability_date"] == "2026-06-29"
    assert u["bedrooms"] == "1"
    assert u["bathrooms"] == "1"


def test_parse_spherexx_units_handles_half_bath() -> None:
    """Bath=2.5 emits "2.5", not "2.50"."""
    body = [{
        "ID": 1, "Name": "X", "Sqft": 1000, "Bed": 2, "Bath": 2.5,
        "Price": 2000, "FloorplanID": 1, "FloorplanName": "Y",
    }]
    units = parse_spherexx_units(body, "test")
    assert units[0]["bathrooms"] == "2.5"


# ── Adapter extract ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_spherexx_adapter_extracts_from_fixture() -> None:
    fix = _load_fixture()
    adapter = SpherexxAdapter()
    ctx = _make_ctx(fix)
    result = await adapter.extract(_DummyPage(), ctx)
    assert isinstance(result, AdapterResult)
    assert result.units, "expected units from fixture"
    assert result.tier_used == "TIER_1_API_SPHEREXX"
    assert result.confidence >= 0.7


@pytest.mark.asyncio
async def test_spherexx_adapter_no_response_path() -> None:
    """No spherexx responses in api_responses → NO_RESPONSE tier."""
    adapter = SpherexxAdapter()
    ctx = _make_ctx([
        {"url": "https://example.com/api/units", "body": [{"foo": "bar"}]},
    ])
    result = await adapter.extract(_DummyPage(), ctx)
    assert result.units == []
    assert result.tier_used == "TIER_1_API_SPHEREXX_NO_RESPONSE"


@pytest.mark.asyncio
async def test_spherexx_adapter_shape_rejected_path() -> None:
    """Has spherexx responses but none match /api/unit shape."""
    adapter = SpherexxAdapter()
    ctx = _make_ctx([
        {"url": "https://presentation.spherexx.app/api/community",
         "body": [{"ID": 5671, "Name": "Henry on the Park"}]},
        {"url": "https://presentation.spherexx.app/api/configuration",
         "body": [{"TileUrl": "", "ElevationUrl": ""}]},
    ])
    result = await adapter.extract(_DummyPage(), ctx)
    assert result.units == []
    assert result.tier_used == "TIER_1_API_SPHEREXX_SHAPE_REJECTED"
    # Diagnostic must list which endpoints WERE seen
    assert any("community" in e for e in result.errors)


# ── Detector integration ──────────────────────────────────────────────


def test_detector_recognizes_spherexx_marker_in_html() -> None:
    detection = detect_pms(
        "https://www.henryonthepark.com/interactive-site-map/",
        page_html='<html><script>window.sspcfg={"key":"x"}</script></html>',
    )
    assert detection.pms == "spherexx"
    assert detection.confidence >= 0.85
    assert detection.recommended_strategy == "api_first"


def test_detector_recognizes_spherexx_via_ssploader_script() -> None:
    detection = detect_pms(
        "https://example.com/",
        page_html=(
            '<html><script src="https://presentation.spherexx.app/js/ssploader.js" '
            'defer></script></html>'
        ),
    )
    assert detection.pms == "spherexx"


def test_detector_recognizes_spherexx_via_iframe_src() -> None:
    detection = detect_pms(
        "https://example.com/",
        page_html=(
            '<html><iframe src="https://presentation.spherexx.app/"></iframe>'
            '</html>'
        ),
    )
    assert detection.pms == "spherexx"
