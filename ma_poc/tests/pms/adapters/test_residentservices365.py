"""365 ResidentServices adapter (2026-05-19, greenfield).

Tile data captured live from rusticwoodsapts.com and waterfordpoint.us
/Marketing/FloorPlans — the same SSR .floorplan-tile shape both emit.
"""

from __future__ import annotations

import pytest

from ma_poc.pms.adapters import get_adapter
from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.adapters.residentservices365 import (
    Residentservices365Adapter,
    parse_residentservices365_tiles,
)
from ma_poc.pms.detector import detect_pms

# Live rusticwoodsapts.com — units-available + waitlist + range variants
_RUSTICWOODS_TILES = [
    {"title": "Sedona 1 Bed 1 Bath 675 sqft", "specs": "1 Bed 1 Bath 675 sqft",
     "pricing": "$759 per month", "availability": "3 Units Available"},
    {"title": "Bordeaux 1 Bed 1 Bath 750 sqft", "specs": "1 Bed 1 Bath 750 sqft",
     "pricing": "$849 - $899 per month", "availability": "Join Waitlist"},
]
# Live waterfordpoint.us — Studio + "Special" suffix + multi-unit + range
_WATERFORDPOINT_TILES = [
    {"title": "Stafford Studio 1 Bath 392 sqft Special", "specs": "Studio 1 Bath 392 sqft",
     "pricing": "$1,675 - $1,860 per month", "availability": "5 Units Available"},
    {"title": "Oxford 1 Bed 1 Bath 720 sqft", "specs": "1 Bed 1 Bath 720 sqft",
     "pricing": "$2,195 per month", "availability": "1 Unit Available"},
]


class _FakePage:
    def __init__(self, tiles: object, url: str = "https://www.rusticwoodsapts.com/Marketing/FloorPlans") -> None:
        self._tiles = tiles
        self.url = url

    async def evaluate(self, _js: str, *_a: object) -> object:
        return self._tiles


def _ctx(base_url: str = "https://www.rusticwoodsapts.com/") -> AdapterContext:
    return AdapterContext(
        base_url=base_url,
        detected=detect_pms(base_url),
        profile=None,
        expected_total_units=None,
        property_id="P_TEST",
    )


def test_parse_rusticwoods_basic() -> None:
    units = parse_residentservices365_tiles(_RUSTICWOODS_TILES, "u")
    assert len(units) == 2
    s = units[0]
    assert s["floor_plan_name"] == "Sedona"
    assert s["bedrooms"] == "1"
    assert s["bathrooms"] == "1"
    assert s["sqft"] == "675"
    assert s["market_rent_low"] == 759
    assert s["market_rent_high"] == 759
    assert s["available_units"] == "3"
    assert s["availability_status"] == "AVAILABLE"
    assert s["extraction_tier"] == "TIER_1_DOM_365RESIDENTSERVICES"

    b = units[1]
    assert b["floor_plan_name"] == "Bordeaux"
    assert b["market_rent_low"] == 849
    assert b["market_rent_high"] == 899
    assert b["availability_status"] == "UNAVAILABLE"  # Waitlist


def test_parse_waterfordpoint_studio_and_special_suffix() -> None:
    units = parse_residentservices365_tiles(_WATERFORDPOINT_TILES, "u")
    assert len(units) == 2
    stafford = units[0]
    assert stafford["floor_plan_name"] == "Stafford"  # "Special" suffix stripped
    assert stafford["bedrooms"] == "0"  # Studio
    assert stafford["sqft"] == "392"
    assert stafford["market_rent_low"] == 1675
    assert stafford["market_rent_high"] == 1860
    assert stafford["available_units"] == "5"

    oxford = units[1]
    assert oxford["market_rent_low"] == 2195
    assert oxford["available_units"] == "1"


def test_parse_skips_empty() -> None:
    assert parse_residentservices365_tiles([{}, {"title": "", "specs": ""}], "u") == []


@pytest.mark.asyncio
async def test_adapter_extract_rusticwoods(monkeypatch) -> None:
    """2026-05-25 update: the adapter now drills into per-plan
    /Marketing/FloorPlans/Units/{guid} pages before falling back to
    plan-level. Mock both self-fetch helpers to return empty so the
    test stays hermetic (no real network calls). The drill returns 0,
    the existing plan-level path still fires, and the test validates
    the original plan-level emit shape (2 plan summaries)."""
    async def _mock_empty(*args, **kwargs):
        return ""
    monkeypatch.setattr(
        Residentservices365Adapter,
        "_fetch_floorplans_html",
        staticmethod(_mock_empty),
    )
    monkeypatch.setattr(
        Residentservices365Adapter,
        "_fetch_detail_html",
        staticmethod(_mock_empty),
    )
    result = await Residentservices365Adapter().extract(_FakePage(_RUSTICWOODS_TILES), _ctx())  # type: ignore[arg-type]
    assert result.tier_used == "TIER_1_DOM_365RESIDENTSERVICES"
    assert len(result.units) == 2
    assert result.confidence > 0.0


@pytest.mark.asyncio
async def test_adapter_no_tiles(monkeypatch) -> None:
    async def _mock_empty(*args, **kwargs):
        return ""
    monkeypatch.setattr(
        Residentservices365Adapter,
        "_fetch_floorplans_html",
        staticmethod(_mock_empty),
    )
    monkeypatch.setattr(
        Residentservices365Adapter,
        "_fetch_detail_html",
        staticmethod(_mock_empty),
    )
    result = await Residentservices365Adapter().extract(_FakePage([]), _ctx())  # type: ignore[arg-type]
    assert result.units == []
    assert result.confidence == 0.0
    assert result.errors


@pytest.mark.asyncio
async def test_adapter_pageless_stub(monkeypatch) -> None:
    async def _mock_empty(*args, **kwargs):
        return ""
    monkeypatch.setattr(
        Residentservices365Adapter,
        "_fetch_floorplans_html",
        staticmethod(_mock_empty),
    )
    monkeypatch.setattr(
        Residentservices365Adapter,
        "_fetch_detail_html",
        staticmethod(_mock_empty),
    )

    class _Bare:
        url = "https://x.com/"

    result = await Residentservices365Adapter().extract(_Bare(), _ctx())  # type: ignore[arg-type]
    assert result.units == []
    assert result.confidence == 0.0


def test_detector_routes_365rs() -> None:
    html = (
        '<html><body><img src="https://cdn.365residentservices.com/themes/x.png">'
        '<a href="/Marketing/FloorPlans">Floor Plans</a></body></html>'
    )
    det = detect_pms("https://www.rusticwoodsapts.com/", page_html=html)
    assert det.pms == "residentservices365"
    assert det.recommended_strategy == "dom_first"


def test_adapter_registered() -> None:
    adapter = get_adapter("residentservices365")
    assert isinstance(adapter, Residentservices365Adapter)
    assert adapter.pms_name == "residentservices365"


# ─────────────────────────────────────────────────────────────────────
# 2026-05-25 user-flagged via Village Square Wheaton (pid 16196) —
# unit-detail drill from /Marketing/FloorPlans/Units/{guid}.
#
# Pre-fix: adapter was plan-level only (its own docstring said so).
# Result on Village Square: 3 plan-summary rows with NULL rent /
# NULL unit_number / NULL date.
#
# Post-fix: adapter follows each plan tile's /Marketing/FloorPlans/
# Units/{guid} anchor, fetches the SSR detail page, parses the
# .unit-details blocks for per-unit data (data-unit-id, data-unit-code,
# data-availabledate, data-rent-min, data-rent-max). Result: 4 real
# units with full data for plans that have availability.
# ─────────────────────────────────────────────────────────────────────


def test_find_unit_detail_urls_basic() -> None:
    """Plan-grid page anchors point to /Marketing/FloorPlans/Units/{guid}.
    Extract them as absolute URLs ready for fetch."""
    from ma_poc.pms.adapters.residentservices365 import find_unit_detail_urls

    html = (
        '<html><body>'
        '<div class="floorplan-tile">'
        '  <a href="/Marketing/FloorPlans/Units/95d137e7-6c24-4843-9dd8-0bd645b15195">View Units</a>'
        '</div>'
        '<div class="floorplan-tile">'
        '  <a href="/Marketing/FloorPlans/Units/838833ae-2035-4cfb-8320-258f08e39142">View Units</a>'
        '</div>'
        '</body></html>'
    )
    urls = find_unit_detail_urls(html, "https://example.com/Marketing/FloorPlans")
    assert urls == [
        "https://example.com/Marketing/FloorPlans/Units/95d137e7-6c24-4843-9dd8-0bd645b15195",
        "https://example.com/Marketing/FloorPlans/Units/838833ae-2035-4cfb-8320-258f08e39142",
    ]


def test_find_unit_detail_urls_dedupe() -> None:
    """Same GUID linked twice (e.g. anchor + button) → one URL out."""
    from ma_poc.pms.adapters.residentservices365 import find_unit_detail_urls

    html = (
        '<a href="/Marketing/FloorPlans/Units/95d137e7-6c24-4843-9dd8-0bd645b15195">A</a>'
        '<a href="/Marketing/FloorPlans/Units/95d137e7-6c24-4843-9dd8-0bd645b15195">B</a>'
    )
    urls = find_unit_detail_urls(html, "https://example.com/Marketing/FloorPlans")
    assert len(urls) == 1


def test_find_unit_detail_urls_empty_on_no_match() -> None:
    """No anchors → empty list (caller falls back to plan-level)."""
    from ma_poc.pms.adapters.residentservices365 import find_unit_detail_urls

    urls = find_unit_detail_urls("<html>nothing</html>", "https://example.com")
    assert urls == []


def test_parse_rs365_unit_blocks_village_square_first_plan() -> None:
    """Real Village Square Wheaton first-plan signature (live-probed
    2026-05-25): 3 unit-details blocks, all with full attrs."""
    from ma_poc.pms.adapters.residentservices365 import parse_rs365_unit_blocks

    html = (
        '<div class="unit-details" data-unit-id="fb985cf9-16d5-46ea-a469-52ff580ec84b" '
        'data-unit-code="022009-202" data-availabledate="1778025600000">'
        '  <div class="unit-header">'
        '    <h3 class="standard">Apartment 022009-202</h3>'
        '    <ul class="list-divider">'
        '      <li>1 Bed</li><li>1 Bath</li>'
        '      <li>785 <span>Square Feet</span></li>'
        '    </ul>'
        '  </div>'
        '  <ul><li class="unitPricing">'
        '    <span data-rent-min="1818.00" data-rent-max="1818.00">$1,818</span>'
        '  </li></ul>'
        '</div>'
        '<div class="unit-details" data-unit-id="9e7d0a97-06bb-4db2-a020-88c0ffd707e2" '
        'data-unit-code="021927-302" data-availabledate="1783776000000">'
        '  <div class="unit-header">'
        '    <h3 class="standard">Apartment 021927-302</h3>'
        '    <ul class="list-divider">'
        '      <li>1 Bed</li><li>1 Bath</li>'
        '      <li>880 <span>Square Feet</span></li>'
        '    </ul>'
        '  </div>'
        '  <ul><li class="unitPricing">'
        '    <span data-rent-min="1862.00" data-rent-max="1862.00">$1,862</span>'
        '  </li></ul>'
        '</div>'
    )
    units = parse_rs365_unit_blocks(html, "https://example.com/Marketing/FloorPlans/Units/95d1")
    assert len(units) == 2
    u1 = units[0]
    assert u1["unit_number"] == "022009-202"
    assert u1["bedrooms"] == "1"
    assert u1["bathrooms"] == "1"
    assert u1["sqft"] == "785"
    assert "1,818" in u1["rent_range"]
    assert u1["availability_date"]  # non-empty ISO date
    assert u1["source_ids"]["rs365_unit_guid"] == "fb985cf9-16d5-46ea-a469-52ff580ec84b"
    assert u1["extraction_tier"] == "TIER_1_DOM_365RESIDENTSERVICES_UNIT_LEVEL"
    u2 = units[1]
    assert u2["unit_number"] == "021927-302"
    assert u2["sqft"] == "880"


def test_parse_rs365_unit_blocks_skips_plan_summary_placeholder() -> None:
    """When a /Units/{guid} page has NO available units, Apollo renders
    ONE .unit-details block as a plan-summary placeholder (no
    data-unit-* attrs; h3 = plan name like '3 Bedrooms + 2 Baths').
    The parser MUST skip these — surfacing them would create a duplicate
    of the plan-level row + a synthetic unit_number that is really
    just the plan name."""
    from ma_poc.pms.adapters.residentservices365 import parse_rs365_unit_blocks

    html = (
        '<div class="unit-details">'  # no data-unit-* attrs
        '  <div class="unit-header">'
        '    <h3 class="standard">3 Bedrooms + 2 Baths</h3>'  # plan name, not unit
        '    <ul class="list-divider">'
        '      <li>3 Beds</li><li>2 Baths</li>'
        '      <li>1165 <span>Square Feet</span></li>'
        '    </ul>'
        '  </div>'
        '</div>'
    )
    units = parse_rs365_unit_blocks(html, "https://example.com/x")
    assert units == [], (
        f"plan-summary placeholder block must be skipped; got {units}"
    )


def test_parse_rs365_unit_blocks_data_attr_only_no_h3() -> None:
    """When the wrapper carries ``data-unit-code`` but the h3 is
    missing or non-conforming, the attr is the source of truth."""
    from ma_poc.pms.adapters.residentservices365 import parse_rs365_unit_blocks

    html = (
        '<div class="unit-details" data-unit-code="101A" '
        'data-availabledate="1778025600000">'
        '  <div class="unit-header">'
        '    <ul class="list-divider">'
        '      <li>2 Beds</li><li>2 Baths</li><li>950 sqft</li>'
        '    </ul>'
        '  </div>'
        '  <span data-rent-min="2000" data-rent-max="2100">$2,000-$2,100</span>'
        '</div>'
    )
    units = parse_rs365_unit_blocks(html, "https://x.test/y")
    assert len(units) == 1
    assert units[0]["unit_number"] == "101A"
    assert units[0]["bedrooms"] == "2"
    assert units[0]["sqft"] == "950"


def test_parse_rs365_unit_blocks_h3_fallback_when_no_data_attr() -> None:
    """H3 fallback path: extract unit_number from 'Apartment X' h3 when
    data-unit-code attr is missing."""
    from ma_poc.pms.adapters.residentservices365 import parse_rs365_unit_blocks

    html = (
        '<div class="unit-details" data-unit-id="abc" data-availabledate="1778025600000">'
        '  <div class="unit-header">'
        '    <h3 class="standard">Apartment 305B</h3>'
        '    <ul class="list-divider">'
        '      <li>1 Bed</li><li>1 Bath</li><li>700 sqft</li>'
        '    </ul>'
        '  </div>'
        '  <span data-rent-min="1500" data-rent-max="1500">$1,500</span>'
        '</div>'
    )
    units = parse_rs365_unit_blocks(html, "https://x.test/y")
    assert len(units) == 1
    assert units[0]["unit_number"] == "305B"


def test_parse_rs365_unit_blocks_rent_data_attr_wins_over_text() -> None:
    """The data-rent-min/-max attrs are authoritative — the rendered
    text $X is a display value that the Apollo JS sometimes swaps for
    a different lease-term. Always prefer the attrs."""
    from ma_poc.pms.adapters.residentservices365 import parse_rs365_unit_blocks

    html = (
        '<div class="unit-details" data-unit-code="201"'
        ' data-availabledate="1778025600000">'
        '  <h3 class="standard">Apartment 201</h3>'
        '  <ul class="list-divider"><li>1 Bed</li><li>1 Bath</li><li>650 sqft</li></ul>'
        '  <span data-rent-min="1800" data-rent-max="1800">$9,999</span>'  # bogus text
        '</div>'
    )
    units = parse_rs365_unit_blocks(html, "https://x.test/y")
    assert len(units) == 1
    assert "1,800" in units[0]["rent_range"]
    assert "9,999" not in units[0]["rent_range"]


def test_parse_rs365_unit_blocks_empty_html() -> None:
    """No unit-details markup at all → empty list."""
    from ma_poc.pms.adapters.residentservices365 import parse_rs365_unit_blocks

    assert parse_rs365_unit_blocks("<html>no units here</html>", "x") == []
    assert parse_rs365_unit_blocks("", "x") == []


def test_parse_rs365_unit_blocks_invalid_epoch_handled() -> None:
    """Bogus epoch ms (negative / future-2100 / non-numeric) →
    availability_date stays empty, unit still emitted."""
    from ma_poc.pms.adapters.residentservices365 import parse_rs365_unit_blocks

    html = (
        '<div class="unit-details" data-unit-code="X1" data-availabledate="9999999999999999">'
        '  <h3 class="standard">Apartment X1</h3>'
        '  <ul class="list-divider"><li>1 Bed</li><li>1 Bath</li><li>700 sqft</li></ul>'
        '  <span data-rent-min="1500" data-rent-max="1500">$1,500</span>'
        '</div>'
    )
    units = parse_rs365_unit_blocks(html, "https://x.test/y")
    assert len(units) == 1
    assert units[0]["availability_date"] == ""  # rejected, not crashed


@pytest.mark.asyncio
async def test_adapter_drill_end_to_end(monkeypatch) -> None:
    """End-to-end: plan tile → mocked /Marketing/FloorPlans returns HTML
    with 1 detail anchor → mocked /Units/{guid} fetch returns a
    unit-details block → adapter emits 1 TIER_1_DOM_365RESIDENTSERVICES
    _UNIT_LEVEL unit with the right shape."""
    fp_html = (
        '<html><body>'
        '<div class="floorplan-tile">'
        '  <a href="/Marketing/FloorPlans/Units/'
        '95d137e7-6c24-4843-9dd8-0bd645b15195">View Units</a>'
        '</div>'
        '</body></html>'
    )
    unit_html = (
        '<div class="unit-details" data-unit-id="fb985cf9-16d5-46ea-a469-52ff580ec84b" '
        'data-unit-code="022009-202" data-availabledate="1778025600000">'
        '  <div class="unit-header">'
        '    <h3 class="standard">Apartment 022009-202</h3>'
        '    <ul class="list-divider">'
        '      <li>1 Bed</li><li>1 Bath</li>'
        '      <li>785 <span>Square Feet</span></li>'
        '    </ul>'
        '  </div>'
        '  <ul><li class="unitPricing">'
        '    <span data-rent-min="1818.00" data-rent-max="1818.00">$1,818</span>'
        '  </li></ul>'
        '</div>'
    )

    async def _mock_fp(*args, **kwargs):
        return fp_html

    async def _mock_detail(*args, **kwargs):
        return unit_html

    monkeypatch.setattr(
        Residentservices365Adapter,
        "_fetch_floorplans_html",
        staticmethod(_mock_fp),
    )
    monkeypatch.setattr(
        Residentservices365Adapter,
        "_fetch_detail_html",
        staticmethod(_mock_detail),
    )

    result = await Residentservices365Adapter().extract(
        _FakePage(_RUSTICWOODS_TILES), _ctx()  # type: ignore[arg-type]
    )
    # Drill succeeded → unit-level tier wins over plan-level.
    assert result.tier_used == "TIER_1_DOM_365RESIDENTSERVICES_UNIT_LEVEL"
    assert len(result.units) == 1
    u = result.units[0]
    assert u["unit_number"] == "022009-202"
    assert u["bedrooms"] == "1"
    assert u["sqft"] == "785"
    assert "1,818" in u["rent_range"]
    assert u["availability_date"]  # non-empty


@pytest.mark.asyncio
async def test_adapter_drill_falls_back_to_plan_level_on_empty(monkeypatch) -> None:
    """If the drill fetches detail pages but all return 0 units (every
    plan has 'no availability'), fall through to the plan-level emit
    so the property at least surfaces with 'we know this property
    exists, just no units right now'."""
    fp_html = (
        '<a href="/Marketing/FloorPlans/Units/95d137e7-6c24-4843-9dd8-0bd645b15195">'
        'View</a>'
    )
    # Detail page has the no-availability placeholder (no data-unit-*)
    detail_html = (
        '<div class="unit-details">'
        '  <h3 class="standard">3 Bedrooms + 2 Baths</h3>'
        '  <ul class="list-divider">'
        '    <li>3 Beds</li><li>2 Baths</li><li>1165 sqft</li>'
        '  </ul>'
        '</div>'
    )

    async def _mock_fp(*args, **kwargs):
        return fp_html

    async def _mock_detail(*args, **kwargs):
        return detail_html

    monkeypatch.setattr(
        Residentservices365Adapter,
        "_fetch_floorplans_html",
        staticmethod(_mock_fp),
    )
    monkeypatch.setattr(
        Residentservices365Adapter,
        "_fetch_detail_html",
        staticmethod(_mock_detail),
    )

    result = await Residentservices365Adapter().extract(
        _FakePage(_RUSTICWOODS_TILES), _ctx()  # type: ignore[arg-type]
    )
    # Drill returned 0 units → plan-level wins.
    assert result.tier_used == "TIER_1_DOM_365RESIDENTSERVICES"
    assert len(result.units) == 2  # plan-level Rusticwoods tiles
