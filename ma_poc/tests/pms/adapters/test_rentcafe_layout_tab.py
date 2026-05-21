"""RentCafe layout-tab adapter (2026-05-21, HAR-validation greenfield).

Live-captured drill body texts from:
  - www.tudorplaceapts.com/floorplans/two-bedrooms (tabular row format)
  - www.campobassoapts.com/floorplans/studio (text row format)
"""

from __future__ import annotations

import pytest

from ma_poc.pms.adapters import get_adapter
from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.adapters.rentcafe_layout_tab import (
    RentCafeLayoutTabAdapter,
    _parse_drill_units,
    _parse_plan_specs,
    parse_rentcafe_layout_tab,
)
from ma_poc.pms.detector import detect_pms


# Live-captured drill texts.
_TUDORPLACE_DRILL_TEXT = (
    "opens a dialog Apartment Sq. Ft. Rent Specials Action "
    "#840_09 900 $1,765.00 Specials Available Apply Now for apartment #840_09 "
    "#6009_09 900 $1,765.00 Specials Available Apply Now for apartment #6009_09 "
    "#6005_08 900 $1,765.00 Specials Available Apply Now for apartment #6005_08 "
    "#5800_01 840 $1,765.00 Specials Available Apply Now for apartment #5800_01"
)

_CAMPOBASSO_DRILL_TEXT = (
    "Cable Ready Dishwasher Available Units "
    "Apartment: # F_205 Starting at: $1,425.00 APPLY NOW FOR APARTMENT F_205 "
    "Available Units Apartment: # G_205 Starting at: $1,450.00 APPLY NOW "
    "Available Units Apartment: # G_105 Starting at: $1,475.00 APPLY NOW "
    "Starting at $1,425.00 4 Apartments Available"
)


# ── _parse_drill_units tests ──


def test_parse_tabular_drill_rows() -> None:
    rows = _parse_drill_units(_TUDORPLACE_DRILL_TEXT)
    assert len(rows) == 4
    # First row
    assert rows[0]["unit_number"] == "840_09"
    assert rows[0]["sqft"] == "900"
    assert rows[0]["rent"] == "1765"
    # Last row has different sqft
    assert rows[3]["unit_number"] == "5800_01"
    assert rows[3]["sqft"] == "840"


def test_parse_text_drill_rows() -> None:
    rows = _parse_drill_units(_CAMPOBASSO_DRILL_TEXT)
    assert len(rows) == 3
    units = [r["unit_number"] for r in rows]
    assert units == ["F_205", "G_205", "G_105"]
    rents = [r["rent"] for r in rows]
    assert rents == ["1425", "1450", "1475"]


def test_parse_dedupes_repeated_unit_numbers() -> None:
    """Both row patterns sometimes match a single unit text — the
    parser must dedupe so we don't double-count rows."""
    text = (
        "#A1 600 $1,500.00 Specials Available "
        "Apartment: # A1 Starting at: $1,500.00 APPLY NOW"
    )
    rows = _parse_drill_units(text)
    assert len(rows) == 1
    assert rows[0]["unit_number"] == "A1"


def test_parse_plan_specs_studio() -> None:
    beds, baths, sqft = _parse_plan_specs(
        "Studio Studio 1 Bath 480 Sq. Ft. 4 Available STUDIO APARTMENT"
    )
    assert beds == 0
    assert baths == "1"
    assert sqft == "480"


def test_parse_plan_specs_two_bed_with_half_bath() -> None:
    beds, baths, sqft = _parse_plan_specs(
        "2 Beds 1.5 Bath 900 Sq. Ft. Specials"
    )
    assert beds == 2
    assert baths == "1.5"
    assert sqft == "900"


# ── parse_rentcafe_layout_tab end-to-end tests ──


def test_parse_full_drill_payload_unit_level() -> None:
    plans = [
        {
            "drillPath": "/floorplans/two-bedrooms",
            "anchorText": "2 Beds 1 Bath 13 Available $1,765.00",
            "h1": "Two Bedrooms",
            "bodyText": "2 Beds 1 Bath 900 Sq. Ft. " + _TUDORPLACE_DRILL_TEXT,
        },
    ]
    rows = parse_rentcafe_layout_tab(plans, "https://x.test")
    assert len(rows) == 4
    r0 = rows[0]
    assert r0["floor_plan_name"] == "Two Bedrooms"
    assert r0["unit_number"] == "840_09"
    assert r0["bedrooms"] == "2"
    assert r0["bathrooms"] == "1"
    assert r0["sqft"] == "900"
    assert r0["market_rent_low"] == 1765
    assert r0["extraction_tier"] == "TIER_1_DOM_RENTCAFE_LT"


def test_parse_studio_drill_text_format() -> None:
    plans = [
        {
            "drillPath": "/floorplans/studio",
            "anchorText": "Studio Starting at $1,425.00",
            "h1": "Studio",
            "bodyText": "Studio Studio 1 Bath 480 Sq. Ft. " + _CAMPOBASSO_DRILL_TEXT,
        },
    ]
    rows = parse_rentcafe_layout_tab(plans, "https://x.test")
    assert len(rows) == 3
    assert rows[0]["unit_number"] == "F_205"
    assert rows[0]["bedrooms"] == "0"  # Studio
    assert rows[0]["bathrooms"] == "1"
    assert rows[0]["sqft"] == "480"
    assert rows[0]["market_rent_low"] == 1425


def test_parse_no_units_falls_back_to_plan_level() -> None:
    """Drill with no parseable unit rows must still surface a plan-level
    row when the body text has 'Starting at $X' or the anchor has '$X'."""
    plans = [
        {
            "drillPath": "/floorplans/penthouse",
            "anchorText": "Penthouse Starting at $5,000",
            "h1": "Penthouse",
            "bodyText": "Penthouse 3 Beds 2 Bath 1500 Sq. Ft. Contact for availability",
        },
    ]
    rows = parse_rentcafe_layout_tab(plans, "https://x.test")
    assert len(rows) == 1
    assert rows[0]["unit_number"] == ""  # plan-level
    assert rows[0]["floor_plan_name"] == "Penthouse"
    assert rows[0]["market_rent_low"] == 5000


# ── adapter end-to-end ──


class _FakePage:
    def __init__(self, payload, url="https://www.tudorplaceapts.com/floorplans"):
        self._payload = payload
        self.url = url

    async def evaluate(self, _js):
        return self._payload


@pytest.mark.asyncio
async def test_adapter_extracts_tudorplace_drill() -> None:
    payload = {
        "ok": True,
        "plans": [
            {
                "drillPath": "/floorplans/two-bedrooms",
                "anchorText": "2 Beds 1 Bath 13 Available $1,765.00",
                "h1": "Two Bedrooms",
                "bodyText": "2 Beds 1 Bath 900 Sq. Ft. " + _TUDORPLACE_DRILL_TEXT,
            },
        ],
    }
    ctx = AdapterContext(
        base_url="https://www.tudorplaceapts.com/",
        detected=detect_pms("https://www.tudorplaceapts.com/"),
        profile=None,
        expected_total_units=None,
        property_id="P_TEST",
    )
    result = await RentCafeLayoutTabAdapter().extract(_FakePage(payload), ctx)  # type: ignore[arg-type]
    assert result.tier_used == "TIER_1_DOM_RENTCAFE_LT"
    assert len(result.units) == 4
    assert result.confidence > 0.7


@pytest.mark.asyncio
async def test_adapter_bails_when_listing_not_layout_tab() -> None:
    payload = {"ok": False, "reason": "no .page-content-floorplans.floorplans-layout-tab listing"}
    ctx = AdapterContext(
        base_url="https://x.test/",
        detected=detect_pms("https://x.test/"),
        profile=None,
        expected_total_units=None,
        property_id="P_TEST",
    )
    result = await RentCafeLayoutTabAdapter().extract(_FakePage(payload), ctx)  # type: ignore[arg-type]
    assert result.confidence == 0.0


def test_detector_routes_on_both_classes_present() -> None:
    from ma_poc.pms.detector import _iter_html_markers
    html = '<div class="page-content-floorplans floorplans-layout-tab">x</div>'
    markers = list(_iter_html_markers(html.lower()))
    assert any(m[0] == "rentcafe_layout_tab" for m in markers)


def test_detector_does_not_route_on_one_class_alone() -> None:
    """``floorplans-layout-tab`` alone or ``page-content-floorplans`` alone
    is NOT enough — must require both to discriminate against generic
    sites that might use one class name in isolation."""
    from ma_poc.pms.detector import _iter_html_markers
    # Only "floorplans-layout-tab" (no page-content-floorplans).
    html1 = '<div class="floorplans-layout-tab">x</div>'
    assert not [m for m in _iter_html_markers(html1.lower()) if m[0] == "rentcafe_layout_tab"]
    # Only "page-content-floorplans".
    html2 = '<div class="page-content-floorplans">x</div>'
    assert not [m for m in _iter_html_markers(html2.lower()) if m[0] == "rentcafe_layout_tab"]


def test_adapter_registered() -> None:
    a = get_adapter("rentcafe_layout_tab")
    assert isinstance(a, RentCafeLayoutTabAdapter)


def test_strategy_is_dom_first() -> None:
    from ma_poc.pms.detector import _STRATEGY_BY_PMS
    assert _STRATEGY_BY_PMS["rentcafe_layout_tab"] == "dom_first"
