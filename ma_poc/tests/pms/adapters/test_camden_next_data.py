"""Camden Living NEXT_DATA parser (2026-06-27).

Camden Property Trust runs 165+ properties on camdenliving.com as a
Next.js SPA. Every URL returns the same shell HTML; inventory lives in
__NEXT_DATA__.props.pageProps.suggestedFloorPlans. Before this adapter
the generic plan-text tier emitted 1 row per property (just the page-
visible "Starting at $X"); now we emit one row per available unit.

Live-validated against Camden Vanderbilt 2026-06-27: 1 → 24 unit rows.
"""
from __future__ import annotations

import json

import pytest

from ma_poc.pms.adapters._camden import (
    detect_camden_next_data,
    is_camden_host,
    parse_camden_next_data,
)


# ─── Host detection ───────────────────────────────────────────


@pytest.mark.parametrize("url,expected", [
    ("https://www.camdenliving.com/apartments/houston-tx/camden-vanderbilt", True),
    ("https://camdenliving.com/", True),
    ("http://www.camdenliving.com/apartments/", True),
    # Negatives — never fire on non-Camden hosts
    ("https://www.gables.com/aster", False),
    ("https://camdenliving.com.evil.com/", False),  # suffix-attack guard
    ("", False),
    (None, False),
    ("https://billingsleycollection.com/the-hudson/", False),
])
def test_is_camden_host(url, expected) -> None:
    assert is_camden_host(url) is expected


# ─── Detector ─────────────────────────────────────────────────


def test_detect_fires_on_real_next_data_shape() -> None:
    """Smallest realistic snippet with both required fingerprints."""
    html = (
        '<html><body>...'
        '<script id="__NEXT_DATA__" type="application/json">'
        '{"props":{"pageProps":{"suggestedFloorPlans":[]}}}</script>'
        + ('x' * 1000)  # body padding so length check passes
        + '</body></html>'
    )
    assert detect_camden_next_data(html) is True


def test_detect_skips_short_html() -> None:
    assert detect_camden_next_data("<html><body></body></html>") is False


def test_detect_skips_html_without_next_data() -> None:
    big = "<html>" + "x" * 5000 + "suggestedFloorPlans</html>"
    # has the key but no __NEXT_DATA__ script tag — don't fire
    assert detect_camden_next_data(big) is False


def test_detect_skips_next_data_without_suggested_floor_plans() -> None:
    html = (
        '<html><script id="__NEXT_DATA__">{"foo":"bar"}</script>'
        + 'x' * 2000 + '</html>'
    )
    assert detect_camden_next_data(html) is False


# ─── Parser ────────────────────────────────────────────────────


def _make_camden_html(sfp_data: list[dict]) -> str:
    """Wrap a list of plan dicts into a realistic Camden NEXT_DATA shell."""
    nd = {"props": {"pageProps": {"suggestedFloorPlans": sfp_data}}}
    return (
        '<html><body>'
        f'<script id="__NEXT_DATA__">{json.dumps(nd)}</script>'
        + ('x' * 2000) + '</body></html>'
    )


def test_parse_emits_one_row_per_available_unit_id() -> None:
    """The cross-product test — 1 plan × 3 unit ids = 3 rows."""
    html = _make_camden_html([{
        "name": "B.2",
        "bedrooms": "1",
        "bathrooms": "1",
        "squareFeet": 618,
        "monthlyRent": 1469,
        "totalMonthlyRent": 1644,
        "availableUnits": 3,
        "availableUnitIds": ["4611", "4613", "4632"],
        "available": True,
        "moveInDate": "2026-07-31T00:00:00.000Z",
    }])
    units = parse_camden_next_data(html, source_url="https://www.camdenliving.com/x")
    assert len(units) == 3
    assert all(u["floor_plan_name"] == "B.2" for u in units)
    assert {u["unit_id"] for u in units} == {"4611", "4613", "4632"}
    u = units[0]
    assert u["beds"] == 1
    assert u["baths"] == 1.0
    assert u["area"] == 618
    assert u["rent_low"] == 1469
    assert u["rent_high"] == 1644
    assert u["available_date"] == "2026-07-31"
    assert u["availability_status"] == "AVAILABLE"
    assert u["extraction_tier"] == "TIER_1_DOM_CAMDEN_NEXT_DATA"


def test_parse_uses_total_rent_as_high_when_higher() -> None:
    html = _make_camden_html([{
        "name": "A1", "bedrooms": "1", "bathrooms": "1",
        "squareFeet": 700, "monthlyRent": 1500,
        "totalMonthlyRent": 1700, "available": True,
        "availableUnitIds": ["1"],
    }])
    u = parse_camden_next_data(html)[0]
    assert u["rent_low"] == 1500
    assert u["rent_high"] == 1700


def test_parse_keeps_low_eq_high_when_total_missing() -> None:
    html = _make_camden_html([{
        "name": "A1", "bedrooms": "1", "bathrooms": "1",
        "squareFeet": 700, "monthlyRent": 1500,
        "available": True, "availableUnitIds": ["1"],
    }])
    u = parse_camden_next_data(html)[0]
    assert u["rent_low"] == 1500
    assert u["rent_high"] == 1500


def test_parse_never_emits_high_below_low() -> None:
    """Defensive — bad data must not produce inverted rent ranges."""
    html = _make_camden_html([{
        "name": "A1", "bedrooms": "1", "bathrooms": "1",
        "squareFeet": 700, "monthlyRent": 1700,
        "totalMonthlyRent": 1500,  # < monthlyRent — corrupt
        "available": True, "availableUnitIds": ["1"],
    }])
    u = parse_camden_next_data(html)[0]
    assert u["rent_low"] == 1700
    assert u["rent_high"] >= u["rent_low"]


def test_parse_skips_plans_without_name() -> None:
    html = _make_camden_html([
        {"name": "", "availableUnitIds": ["1"]},
        {"name": None, "availableUnitIds": ["2"]},
        {"name": "X", "bedrooms": "1", "bathrooms": "1",
         "squareFeet": 500, "monthlyRent": 1000,
         "available": True, "availableUnitIds": ["3"]},
    ])
    units = parse_camden_next_data(html)
    assert len(units) == 1
    assert units[0]["floor_plan_name"] == "X"


def test_parse_falls_back_to_unit_number_when_ids_empty() -> None:
    """Available plan with no per-unit IDs still emits one plan row
    using unitNumber so the plan shows up downstream."""
    html = _make_camden_html([{
        "name": "Z", "bedrooms": "2", "bathrooms": "2",
        "squareFeet": 1000, "monthlyRent": 2000,
        "available": True, "availableUnitIds": [],
        "unitNumber": "201",
    }])
    units = parse_camden_next_data(html)
    assert len(units) == 1
    assert units[0]["unit_id"] == "201"


def test_parse_returns_empty_on_no_next_data() -> None:
    assert parse_camden_next_data("<html></html>") == []
    assert parse_camden_next_data("") == []


def test_parse_returns_empty_on_malformed_json() -> None:
    html = '<html><script id="__NEXT_DATA__">{broken json{</script></html>'
    assert parse_camden_next_data(html) == []


def test_parse_returns_empty_when_sfp_missing() -> None:
    html = (
        '<html><script id="__NEXT_DATA__">'
        '{"props":{"pageProps":{"otherKey":[]}}}'
        '</script></html>'
    )
    assert parse_camden_next_data(html) == []


def test_parse_uses_media_override_name_when_top_name_missing() -> None:
    """Some Camden entries put the plan label in media.overrideName
    when the top-level name is null. Don't lose those plans."""
    html = _make_camden_html([{
        "name": None,
        "bedrooms": "1", "bathrooms": "1",
        "squareFeet": 600, "monthlyRent": 1300,
        "available": True, "availableUnitIds": ["x"],
        "media": {"overrideName": "MediaPlan"},
    }])
    units = parse_camden_next_data(html)
    assert len(units) == 1
    assert units[0]["floor_plan_name"] == "MediaPlan"
