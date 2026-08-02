"""365 ResidentServices adapter (2026-05-19, greenfield).

Tile data captured live from rusticwoodsapts.com and waterfordpoint.us
/Marketing/FloorPlans — the same SSR .floorplan-tile shape both emit.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ma_poc.pms.adapters import get_adapter
from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.adapters.residentservices365 import (
    Residentservices365Adapter,
    RS365PlanTarget,
    parse_residentservices365_tiles,
    parse_rs365_plan_targets,
    parse_rs365_unit_blocks,
    rs365_plan_rows,
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


def _code_only_ctx(property_id: str, base_url: str) -> AdapterContext:
    ctx = _ctx(base_url)
    ctx.property_id = property_id
    ctx.fetch_result = SimpleNamespace(
        body=(
            b'<html><script src="https://cdn.365residentservices.com/'
            b'themes/apollo/site.js"></script></html>'
        ),
        final_url=base_url,
    )
    return ctx


def _floorplans_html(*detail_guids: str) -> str:
    anchors = "".join(
        '<a href="/Marketing/FloorPlans/Units/' + guid + '">View Units</a>'
        for guid in detail_guids
    )
    return (
        '<html><script src="https://cdn.365residentservices.com/x.js"></script>'
        f'<div class="floorplan-tile">{anchors}</div></html>'
    )


def _unit_detail_html(unit_number: str, rent: int | None) -> str:
    rent_attrs = (
        f'<span data-rent-min="{rent}" data-rent-max="{rent}">${rent}</span>'
        if rent is not None
        else ""
    )
    return (
        f'<div class="unit-details" data-unit-code="{unit_number}" '
        'data-availabledate="1778025600000">'
        f'<h3 class="standard">Apartment {unit_number}</h3>'
        '<ul class="list-divider"><li>1 Bed</li><li>1 Bath</li>'
        '<li>700 sqft</li></ul>'
        f"{rent_attrs}</div>"
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
    assert result.tier_used == "TIER_1_DOM_365RESIDENTSERVICES_PLAN_LEVEL"
    assert result.units == []
    assert len(result.plan_summaries) == 2


@pytest.mark.asyncio
async def test_code_only_plan_fallback_uses_exact_dedicated_catalogue(
    monkeypatch,
) -> None:
    guid = "6f852f38-fad8-41dc-a594-dda77320fc32"
    fp_html = (
        '<script src="https://cdn.365residentservices.com/site.js"></script>'
        '<div class="floorplan-tile" data-name="Greenwood" data-beds="1" '
        'data-baths="1" data-size="775" data-rent-min="1406" '
        'data-rent-max="1512"><div class="title-row">Greenwood 1 Bed 1 Bath '
        '775 sqft</div><ul class="list-divider"><li>1 Bed</li><li>1 Bath</li>'
        '<li>775 sqft</li></ul><div class="availability">Only 2 Left</div>'
        f'<a href="/floorplan/{guid}">View Units</a></div>'
    )

    async def _mock_fp(*_args, **_kwargs):
        return fp_html

    async def _mock_detail(*_args, **_kwargs):
        return ""

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
        None,  # type: ignore[arg-type]
        _code_only_ctx("60939", "https://greenarch.example/"),
    )

    assert result.units == []
    assert result.tier_used == "TIER_1_DOM_365RESIDENTSERVICES_PLAN_LEVEL"
    assert len(result.plan_summaries) == 1
    [plan] = result.plan_summaries
    assert plan["floor_plan_name"] == "Greenwood"
    assert plan["bedrooms"] == "1"
    assert plan["market_rent_low"] == 1406
    assert plan["available_units"] == "2"
    assert plan["availability_status"] == "AVAILABLE"
    assert plan["source_ids"] == {"rs365_floorplan_guid": guid}
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


def test_find_unit_detail_urls_supports_modern_floorplan_route() -> None:
    """Gemini/cosmic themes link the same unit roster at /floorplan/{guid}."""
    from ma_poc.pms.adapters.residentservices365 import find_unit_detail_urls

    html = (
        '<a href="/floorplan/6f852f38-fad8-41dc-a594-dda77320fc32">Greenwood</a>'
        '<a href="/floorplan/199bedb8-ba29-452b-957b-fd97c12ec6c5">Stradford</a>'
    )
    urls = find_unit_detail_urls(html, "https://example.com/Marketing/FloorPlans")
    assert urls == [
        "https://example.com/floorplan/6f852f38-fad8-41dc-a594-dda77320fc32",
        "https://example.com/floorplan/199bedb8-ba29-452b-957b-fd97c12ec6c5",
    ]


def test_find_unit_detail_urls_empty_on_no_match() -> None:
    """No anchors → empty list (caller falls back to plan-level)."""
    from ma_poc.pms.adapters.residentservices365 import find_unit_detail_urls

    urls = find_unit_detail_urls("<html>nothing</html>", "https://example.com")
    assert urls == []


def test_parse_rs365_plan_target_preserves_catalogue_semantics() -> None:
    guid = "6f852f38-fad8-41dc-a594-dda77320fc32"
    html = (
        '<div class="floorplan-tile" data-name="Greenwood" data-beds="1" '
        'data-baths="1" data-size="775" data-rent-min="1406" '
        'data-rent-max="1512">'
        '<div class="title-row">Greenwood 1 Bed 1 Bath 775 sqft</div>'
        '<ul class="list-divider"><li>1 Bed</li><li>1 Bath</li>'
        '<li>775 sqft</li></ul><div class="availability">Only 2 Left</div>'
        f'<a href="/floorplan/{guid}">View Units</a></div>'
    )

    [target] = parse_rs365_plan_targets(html, "https://greenarch.example/floorplans")
    assert target == RS365PlanTarget(
        url=f"https://greenarch.example/floorplan/{guid}",
        plan_id=guid,
        name="Greenwood",
        beds="1",
        baths="1",
        sqft="775",
        rent_low=1406,
        rent_high=1512,
        availability_status="AVAILABLE",
        available_units="2",
    )
    [row] = rs365_plan_rows([target])
    assert row["floor_plan_name"] == "Greenwood"
    assert row["source_ids"] == {"rs365_floorplan_guid": guid}
    assert row["available_units"] == "2"
    assert row["extraction_tier"].endswith("_PLAN_LEVEL")


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


def _semantic_unit_html(
    *,
    plan_name: str = "Greenwood",
    availability: str = "September 8, 2026 / Available",
) -> str:
    return (
        '<div class="unit-details" '
        'data-unit-id="fb985cf9-16d5-46ea-a469-52ff580ec84b" '
        'data-unit-code="S407" data-availabledate="1704067200000">'
        '<h3 class="standard">Apartment S407</h3>'
        '<ul class="list-divider"><li>1 Bed</li><li>1 Bath</li>'
        '<li>775 Square Feet</li></ul>'
        f'<ul><li><label>Floor Plan:</label> {plan_name}</li>'
        '<li><label>Floor:</label> 4</li></ul>'
        f'<p class="availability">{availability}</p>'
        '<span data-rent-min="1406" data-rent-max="1406" data-term="12">'
        '$1,406</span><button data-apply="true" data-building="South"></button>'
        '</div>'
    )


def _greenwood_target() -> RS365PlanTarget:
    return RS365PlanTarget(
        url="https://greenarch.example/floorplan/6f852f38-fad8-41dc-a594-dda77320fc32",
        plan_id="6f852f38-fad8-41dc-a594-dda77320fc32",
        name="Greenwood",
        beds="1",
        baths="1",
        sqft="775",
        rent_low=1406,
        rent_high=1406,
        availability_status="AVAILABLE",
        available_units="1",
    )


def test_rs365_unit_joins_parent_plan_floor_term_and_future_date() -> None:
    [row] = parse_rs365_unit_blocks(
        _semantic_unit_html(),
        _greenwood_target().url,
        _greenwood_target(),
    )
    assert row["floor_plan_name"] == "Greenwood"
    assert row["floor"] == "4"
    assert row["building"] == "South"
    assert row["lease_term"] == "12"
    assert row["availability_date"] == "2026-09-08"
    assert row["move_in_date"] == "2026-09-08"
    assert row["source_ids"] == {
        "rs365_unit_guid": "fb985cf9-16d5-46ea-a469-52ff580ec84b",
        "rs365_floorplan_guid": "6f852f38-fad8-41dc-a594-dda77320fc32",
    }


def test_rs365_visible_today_overrides_stale_epoch_in_both_v2_formatters() -> None:
    from datetime import UTC, datetime

    from ma_poc.core.schema_v2 import _format_v2_unit as core_formatter
    from ma_poc.scripts.runners.jugnu import _format_v2_unit as jugnu_formatter

    [row] = parse_rs365_unit_blocks(
        _semantic_unit_html(availability="Today / Available"),
        _greenwood_target().url,
        _greenwood_target(),
    )
    assert row["availability_date"] == "Available Now"
    capture = datetime(2026, 8, 2, 12, tzinfo=UTC)
    for formatter in (core_formatter, jugnu_formatter):
        output = formatter(row, capture, "60939")
        assert output["available_date"] == "2026-08-02"
        assert output["availability_date_provenance"] == "available_now"


def test_rs365_telfair_best_value_is_one_coherent_tuple() -> None:
    html = (
        '<div class="unit-details" data-unit-code="1114">'
        '<h3 class="standard">Apartment 1114</h3>'
        '<ul class="list-divider"><li>1 Bed</li><li>1 Bath</li>'
        '<li>700 sqft</li></ul>'
        '<ul><li><label>Floor Plan:</label> A1</li></ul>'
        '<span data-rent-min="1558" data-rent-max="1558" data-term="12">'
        '$1,558</span>'
        '<a class="better-pricing" title="Best Value" '
        'data-content="&lt;div&gt;Per Month: $1,406&lt;/div&gt;'
        '&lt;div&gt;Lease Term: 13&lt;/div&gt;'
        '&lt;div&gt;Move-In-Date: September 12, 2026&lt;/div&gt;'
        '&lt;button data-moveInDate=\'9/12/2026 12:00:00 AM\' '
        'data-term=\'13\'&gt;&lt;/button&gt;">Best Value</a></div>'
    )
    target = RS365PlanTarget(
        url="https://telfair.example/floorplan/e061f8cd-4e76-43bc-8112-c99aed9301c0",
        plan_id="e061f8cd-4e76-43bc-8112-c99aed9301c0",
        name="A1",
        beds="1",
        baths="1",
        sqft="700",
        rent_low=1406,
        rent_high=1558,
        availability_status="AVAILABLE",
        available_units="1",
    )

    [row] = parse_rs365_unit_blocks(html, target.url, target)
    assert row["market_rent_low"] == 1406
    assert row["market_rent_high"] == 1406
    assert row["lease_term"] == "13"
    assert row["availability_date"] == "2026-09-12"
    assert row["_rs365_pricing_selection"] == "best_value"


def test_rs365_best_value_move_in_does_not_erase_visible_now_semantics() -> None:
    from datetime import UTC, datetime

    from ma_poc.scripts.runners.jugnu import _format_v2_unit

    html = (
        '<div class="unit-details" data-unit-code="1114" '
        'data-availabledate="1704067200000">'
        '<h3 class="standard">Apartment 1114</h3>'
        '<ul class="list-divider"><li>1 Bed</li><li>1 Bath</li>'
        '<li>700 sqft</li></ul><ul><li><label>Floor Plan:</label> A1</li></ul>'
        '<p class="availability">Now / Available</p>'
        '<a class="better-pricing" title="Best Value" '
        'data-content="&lt;div&gt;Per Month: $1,406&lt;/div&gt;'
        '&lt;div&gt;Lease Term: 13&lt;/div&gt;'
        '&lt;div&gt;Move-In-Date: August 2, 2026&lt;/div&gt;'
        '&lt;button data-moveInDate=\'8/2/2026 12:00:00 AM\' '
        'data-term=\'13\'&gt;&lt;/button&gt;">Best Value</a></div>'
    )
    target = RS365PlanTarget(
        url="https://telfair.example/floorplan/e061f8cd-4e76-43bc-8112-c99aed9301c0",
        plan_id="e061f8cd-4e76-43bc-8112-c99aed9301c0",
        name="A1",
        beds="1",
        baths="1",
        sqft="700",
        rent_low=1406,
        rent_high=1406,
        availability_status="AVAILABLE",
        available_units="1",
    )

    [row] = parse_rs365_unit_blocks(html, target.url, target)
    assert row["availability_date"] == "Available Now"
    assert row["move_in_date"] == "2026-08-02"
    assert row["lease_term"] == "13"
    output = _format_v2_unit(
        row,
        datetime(2026, 8, 2, 12, tzinfo=UTC),
        "63462",
    )
    assert output["available_date"] == "2026-08-02"
    assert output["availability_date_provenance"] == "available_now"


def test_rs365_parent_and_visible_plan_mismatch_fails_closed() -> None:
    assert parse_rs365_unit_blocks(
        _semantic_unit_html(plan_name="Sibling Plan"),
        _greenwood_target().url,
        _greenwood_target(),
    ) == []


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
    assert result.tier_used == "TIER_1_DOM_365RESIDENTSERVICES_PLAN_LEVEL"
    assert result.units == []
    assert len(result.plan_summaries) == 2


@pytest.mark.parametrize(
    ("property_id", "base_url", "unit_number", "rent", "guid"),
    [
        (
            "16196",
            "http://www.villagesquarewheaton.com/Home/Index/36189",
            "021927-402",
            2008,
            "95d137e7-6c24-4843-9dd8-0bd645b15195",
        ),
        (
            "34909",
            "http://www.polodowns.com/",
            "901308",
            1345,
            "838833ae-2035-4cfb-8320-258f08e39142",
        ),
        (
            "63462",
            "https://apartmentssugarlandtexas.com/",
            "1114",
            1563,
            "e061f8cd-4e76-43bc-8112-c99aed9301c0",
        ),
    ],
)
@pytest.mark.asyncio
async def test_code_only_recovers_three_live_rs365_signatures(
    monkeypatch,
    property_id: str,
    base_url: str,
    unit_number: str,
    rent: int,
    guid: str,
) -> None:
    """The page=None lane covers the three live-positive cohort members."""

    async def _mock_floorplans(*_args, **_kwargs):
        return _floorplans_html(guid)

    async def _mock_detail(*_args, **_kwargs):
        return _unit_detail_html(unit_number, rent)

    monkeypatch.setattr(
        Residentservices365Adapter,
        "_fetch_floorplans_html",
        staticmethod(_mock_floorplans),
    )
    monkeypatch.setattr(
        Residentservices365Adapter,
        "_fetch_detail_html",
        staticmethod(_mock_detail),
    )

    result = await Residentservices365Adapter().extract(
        None,  # type: ignore[arg-type]
        _code_only_ctx(property_id, base_url),
    )

    assert result.tier_used == "TIER_1_DOM_365RESIDENTSERVICES_UNIT_LEVEL"
    assert len(result.units) == 1
    assert result.units[0]["unit_number"] == unit_number
    assert result.units[0]["market_rent_low"] == rent


@pytest.mark.asyncio
async def test_code_only_no_inventory_control_is_not_a_unit(monkeypatch) -> None:
    """Westshore (16377) has live detail pages but no available unit attrs."""
    guid = "95d137e7-6c24-4843-9dd8-0bd645b15195"

    async def _mock_floorplans(*_args, **_kwargs):
        return _floorplans_html(guid)

    async def _mock_detail(*_args, **_kwargs):
        return (
            '<div class="unit-details"><h3 class="standard">The Bayshore</h3>'
            '<ul class="list-divider"><li>2 Beds</li><li>2 Baths</li></ul></div>'
        )

    monkeypatch.setattr(
        Residentservices365Adapter,
        "_fetch_floorplans_html",
        staticmethod(_mock_floorplans),
    )
    monkeypatch.setattr(
        Residentservices365Adapter,
        "_fetch_detail_html",
        staticmethod(_mock_detail),
    )

    result = await Residentservices365Adapter().extract(
        None,  # type: ignore[arg-type]
        _code_only_ctx("16377", "http://www.westshoretampabay.com/"),
    )

    assert result.units == []
    assert result.confidence == 0.0
    assert "no canonical unit with rent" in result.errors[-1]


@pytest.mark.asyncio
async def test_code_only_migrated_route_control_is_not_a_unit(monkeypatch) -> None:
    """Prime Gardenside (39573) redirects to a grid with no unit-detail links."""

    async def _mock_floorplans(*_args, **_kwargs):
        return _floorplans_html()

    async def _unexpected_detail(*_args, **_kwargs):
        raise AssertionError("no detail request is allowed without an exact link")

    monkeypatch.setattr(
        Residentservices365Adapter,
        "_fetch_floorplans_html",
        staticmethod(_mock_floorplans),
    )
    monkeypatch.setattr(
        Residentservices365Adapter,
        "_fetch_detail_html",
        staticmethod(_unexpected_detail),
    )

    result = await Residentservices365Adapter().extract(
        None,  # type: ignore[arg-type]
        _code_only_ctx("39573", "https://www.liveprimegardenside.com/"),
    )

    assert result.units == []
    assert result.confidence == 0.0


@pytest.mark.asyncio
async def test_code_only_rejects_unit_identity_without_positive_rent(
    monkeypatch,
) -> None:
    guid = "95d137e7-6c24-4843-9dd8-0bd645b15195"

    async def _mock_floorplans(*_args, **_kwargs):
        return _floorplans_html(guid)

    async def _mock_detail(*_args, **_kwargs):
        return _unit_detail_html("A-101", None)

    monkeypatch.setattr(
        Residentservices365Adapter,
        "_fetch_floorplans_html",
        staticmethod(_mock_floorplans),
    )
    monkeypatch.setattr(
        Residentservices365Adapter,
        "_fetch_detail_html",
        staticmethod(_mock_detail),
    )

    result = await Residentservices365Adapter().extract(
        None,  # type: ignore[arg-type]
        _code_only_ctx("P", "https://example.com/"),
    )

    assert result.units == []


@pytest.mark.asyncio
async def test_code_only_caps_detail_requests_and_isolates_one_failure(
    monkeypatch,
) -> None:
    guids = [f"00000000-0000-0000-0000-{index:012d}" for index in range(18)]
    fetched: list[str] = []

    async def _mock_floorplans(*_args, **_kwargs):
        return _floorplans_html(*guids)

    async def _mock_detail(url: str):
        fetched.append(url)
        if url.endswith(guids[3]):
            raise RuntimeError("one detail page failed")
        return _unit_detail_html(url.rsplit("-", 1)[-1], 1500)

    monkeypatch.setattr(
        Residentservices365Adapter,
        "_fetch_floorplans_html",
        staticmethod(_mock_floorplans),
    )
    monkeypatch.setattr(
        Residentservices365Adapter,
        "_fetch_detail_html",
        staticmethod(_mock_detail),
    )

    result = await Residentservices365Adapter().extract(
        None,  # type: ignore[arg-type]
        _code_only_ctx("P", "https://example.com/"),
    )

    assert len(fetched) == 16
    assert len(result.units) == 15


def test_rs365_property_scope_allows_only_same_normalized_host() -> None:
    from ma_poc.pms.adapters.residentservices365 import _same_property_host

    assert _same_property_host(
        "http://example.com/Marketing/FloorPlans",
        "https://www.example.com/Marketing/FloorPlans",
    )
    assert not _same_property_host(
        "https://example.com/Marketing/FloorPlans",
        "https://inventory.example.net/Marketing/FloorPlans",
    )


@pytest.mark.asyncio
async def test_bounded_get_does_not_follow_cross_host_redirect(monkeypatch) -> None:
    import httpx

    requests: list[str] = []
    client_options: dict[str, object] = {}

    class _Response:
        status_code = 302
        url = "https://example.com/Marketing/FloorPlans"
        headers = {"location": "https://inventory.example.net/units"}
        encoding = "utf-8"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def aiter_bytes(self):
            yield b"must not be read"

    class _Client:
        def __init__(self, **kwargs):
            client_options.update(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def stream(self, _method: str, url: str):
            requests.append(url)
            return _Response()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)

    html, final_url = await Residentservices365Adapter._bounded_html_get(
        "https://example.com/Marketing/FloorPlans",
        max_bytes=100,
    )

    assert html == ""
    assert final_url == "https://inventory.example.net/units"
    assert requests == ["https://example.com/Marketing/FloorPlans"]
    assert client_options["follow_redirects"] is False
    assert client_options["trust_env"] is False


@pytest.mark.asyncio
async def test_bounded_get_allows_same_host_redirect_but_enforces_bytes(
    monkeypatch,
) -> None:
    import httpx

    requests: list[str] = []

    class _Response:
        encoding = "utf-8"

        def __init__(self, url: str):
            self.url = url
            if url.startswith("http://"):
                self.status_code = 301
                self.headers = {
                    "location": "https://www.example.com/Marketing/FloorPlans"
                }
            else:
                self.status_code = 200
                self.headers = {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def aiter_bytes(self):
            yield b"1234"
            yield b"5678"

    class _Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def stream(self, _method: str, url: str):
            requests.append(url)
            return _Response(url)

    monkeypatch.setattr(httpx, "AsyncClient", _Client)

    html, final_url = await Residentservices365Adapter._bounded_html_get(
        "http://example.com/Marketing/FloorPlans",
        max_bytes=7,
    )

    assert html == ""
    assert final_url == "https://www.example.com/Marketing/FloorPlans"
    assert requests == [
        "http://example.com/Marketing/FloorPlans",
        "https://www.example.com/Marketing/FloorPlans",
    ]
