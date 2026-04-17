"""Phase 3 — Tests for remaining adapters (AppFolio, AvalonBay, RealPage OLL,
Squarespace, Wix).

Grouped in one file since these adapters have limited real captured data.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from pms.adapters.appfolio import AppFolioAdapter, parse_appfolio_listings
from pms.adapters.avalonbay import AvalonBayAdapter, parse_avalonbay_units
from pms.adapters.base import AdapterContext, AdapterResult
from pms.adapters.realpage_oll import RealPageOllAdapter
from pms.adapters.squarespace_nopms import SquarespaceNoPmsAdapter
from pms.adapters.wix_nopms import WixNoPmsAdapter
from pms.detector import detect_pms

FIXTURES = Path(__file__).parent / "fixtures"


def _make_ctx(api_responses: list[dict], url: str = "https://example.com") -> AdapterContext:
    ctx = AdapterContext(
        base_url=url,
        detected=detect_pms(url),
        profile=None,
        expected_total_units=None,
        property_id="TEST",
    )
    ctx._api_responses = api_responses  # type: ignore[attr-defined]
    return ctx


class _DummyPage:
    pass


# ── AppFolio ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_appfolio_extract_happy_path() -> None:
    responses = json.loads((FIXTURES / "appfolio" / "synthetic_listings.json").read_text())
    adapter = AppFolioAdapter()
    ctx = _make_ctx(responses, "https://example.appfolio.com/listings")
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert isinstance(result, AdapterResult)
    assert len(result.units) == 2
    assert "APPFOLIO" in result.units[0]["extraction_tier"]


@pytest.mark.asyncio
async def test_appfolio_extracts_from_real_community_fixture() -> None:
    """Real community fixture (12617) includes floorplans/all endpoint with unit data."""
    responses = json.loads((FIXTURES / "appfolio" / "12617_community.json").read_text())
    adapter = AppFolioAdapter()
    ctx = _make_ctx(responses, "https://example.appfolio.com/")
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    # The fixture includes /floorplans/all/ with bed, bath, rent, sq_ft
    assert len(result.units) > 0
    assert result.units[0]["rent_range"]


def test_appfolio_static_fingerprints() -> None:
    assert "appfolio.com" in AppFolioAdapter().static_fingerprints()


def test_parse_appfolio_listings_basic() -> None:
    items = [{"id": "1", "name": "Suite A", "bedrooms": 1, "bathrooms": 1,
              "sqft": 650, "price": 1400, "status": "available"}]
    units = parse_appfolio_listings(items, "test")
    assert len(units) == 1
    assert "$1,400" in units[0]["rent_range"]


# ── AvalonBay ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_avalonbay_extract_happy_path() -> None:
    responses = json.loads((FIXTURES / "avalonbay" / "synthetic_units.json").read_text())
    adapter = AvalonBayAdapter()
    ctx = _make_ctx(responses, "https://www.avaloncommunities.com/property/")
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert isinstance(result, AdapterResult)
    assert len(result.units) == 2
    assert "AVALONBAY" in result.units[0]["extraction_tier"]


@pytest.mark.asyncio
async def test_avalonbay_returns_empty_on_empty_units() -> None:
    responses = json.loads((FIXTURES / "avalonbay" / "empty_response.json").read_text())
    adapter = AvalonBayAdapter()
    ctx = _make_ctx(responses, "https://www.avaloncommunities.com/property/")
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert result.units == []


def test_avalonbay_static_fingerprints() -> None:
    assert "avaloncommunities.com" in AvalonBayAdapter().static_fingerprints()


def test_parse_avalonbay_units_basic() -> None:
    """AvalonBay real API shape with bedroomNumber, unitName, squareFeet."""
    items = [{"unitName": "1043", "bedroomNumber": 1, "bathroomNumber": 1,
              "squareFeet": 711, "floorPlan": {"name": "AM12"},
              "floorNumber": "1", "availableDateUnfurnished": "2026-06-11T04:00:00+00:00",
              "promotions": [{"promotionTitle": "1 month free!"}]}]
    summary = {"totalPricesStartingAt": {"1": {"unfurnished": 2431}}}
    units = parse_avalonbay_units(items, "test", summary)
    assert len(units) == 1
    assert units[0]["unit_number"] == "1043"
    assert units[0]["bedrooms"] == "1"
    assert units[0]["sqft"] == "711"
    assert "$2,431" in units[0]["rent_range"]
    assert units[0]["availability_date"] == "2026-06-11"
    assert "1 month free" in units[0]["concession"]


# ── RealPage OLL ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_realpage_oll_extract_happy_path() -> None:
    responses = json.loads((FIXTURES / "realpage_oll" / "293707.json").read_text())
    adapter = RealPageOllAdapter()
    ctx = _make_ctx(responses, "https://api.ws.realpage.com/v2/property/test")
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert isinstance(result, AdapterResult)
    assert len(result.units) >= 1
    assert "REALPAGE_OLL" in result.units[0]["extraction_tier"]


@pytest.mark.asyncio
async def test_realpage_oll_returns_empty_on_empty() -> None:
    responses = json.loads((FIXTURES / "realpage_oll" / "empty.json").read_text())
    adapter = RealPageOllAdapter()
    ctx = _make_ctx(responses)
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert result.units == []


def test_realpage_oll_static_fingerprints() -> None:
    assert "realpage.com" in RealPageOllAdapter().static_fingerprints()


# ── Squarespace ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_squarespace_returns_empty() -> None:
    adapter = SquarespaceNoPmsAdapter()
    ctx = _make_ctx([], "https://83freight.com")
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert result.units == []
    assert result.confidence == 0.0
    assert any("syndication_only" in e for e in result.errors)


def test_squarespace_static_fingerprints() -> None:
    fps = SquarespaceNoPmsAdapter().static_fingerprints()
    assert "squarespace.com" in fps


# ── Wix ───────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_wix_returns_empty() -> None:
    adapter = WixNoPmsAdapter()
    ctx = _make_ctx([], "https://example-wix.com")
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert result.units == []
    assert result.confidence == 0.0
    assert any("syndication_only" in e for e in result.errors)


def test_wix_static_fingerprints() -> None:
    fps = WixNoPmsAdapter().static_fingerprints()
    assert "wix.com" in fps
