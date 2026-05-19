"""iLoveLeasing JS-widget availability-table parser tests.

Validated 2026-05-18 against the REAL rendered DOM of
krcapartments.com/property-detail/wedgewood-place/ (fixture
iloveleasing_wedgewood.html — 5 units). main's generic
extract_units_from_dom returns 0 on this structure (confirmed via
ephemeral origin/main worktree), which is why this dedicated parser
exists.
"""
from __future__ import annotations

from pathlib import Path

from ma_poc.pms.adapters._iloveleasing_table import (
    _iso,
    _money,
    parse_iloveleasing_table,
)

_FIX = (
    Path(__file__).parent / "fixtures" / "iloveleasing_wedgewood.html"
)
_URL = "https://krcapartments.com/property-detail/wedgewood-place/"


def test_money() -> None:
    assert _money("$1,325.00") == 1325
    assert _money("$1,150.00") == 1150
    assert _money("1295") == 1295
    assert _money("") is None
    assert _money("call") is None


def test_iso() -> None:
    assert _iso("8/1/2026") == "2026-08-01"
    assert _iso("10/1/2025") == "2025-10-01"
    assert _iso("2026-08-01") == "2026-08-01"
    assert _iso("Now") == ""
    assert _iso("") == ""


def test_parse_real_fixture_unit_level() -> None:
    html = _FIX.read_text()
    units = parse_iloveleasing_table(html, _URL)
    assert len(units) == 5
    u0 = units[0]
    assert u0["unit_number"] == "303"
    assert u0["market_rent_low"] == 1325
    assert u0["market_rent_high"] == 1325
    assert u0["bedrooms"] == "2"
    assert u0["sqft"] == "815"
    assert u0["availability_status"] == "AVAILABLE"
    assert u0["extraction_tier"] == "TIER_1_DOM_RENTMANAGER_ILOVELEASING"
    # 1-bed end unit, future availability parsed from td.unit-date
    last = units[-1]
    assert last["unit_number"] == "107"
    assert last["market_rent_low"] == 1150
    assert last["bedrooms"] == "1"
    assert last["sqft"] == "650"
    assert last["availability_date"] == "2026-08-01"
    # every row has a real unit_number + positive rent (strict-gate ready)
    assert all(
        u["unit_number"] and (u["market_rent_low"] or 0) > 0 for u in units
    )
    assert [u["unit_number"] for u in units] == ["303", "103", "200", "105", "107"]


def test_parse_empty_and_non_table() -> None:
    assert parse_iloveleasing_table("", _URL) == []
    assert parse_iloveleasing_table("<div>no units</div>", _URL) == []
    assert parse_iloveleasing_table(
        "<table class='other'><tr><td>x</td></tr></table>", _URL
    ) == []


def test_dedup_by_featherlight_id() -> None:
    # Two rows, same featherlight detail id -> one unit (stable-id dedup).
    dup = (
        '<table class="unit-avail-table"><tbody>'
        '<tr class="unit_avail_container" data-date="1/1/2026">'
        '<td class="unit-number">11</td><td class="unit-beds">1</td>'
        '<td class="unit-rent">$1,000.00</td><td class="unit-sqft">500</td>'
        '<td class="unit-date">Now</td>'
        '<td class="unit-info"><a data-featherlight="#999-detail">d</a></td>'
        "</tr>"
        '<tr class="unit_avail_container" data-date="2/1/2026">'
        '<td class="unit-number">11B</td><td class="unit-beds">1</td>'
        '<td class="unit-rent">$1,050.00</td><td class="unit-sqft">500</td>'
        '<td class="unit-date">Now</td>'
        '<td class="unit-info"><a data-featherlight="#999-detail">d</a></td>'
        "</tr></tbody></table>"
    )
    units = parse_iloveleasing_table(dup, _URL)
    assert len(units) == 1
    assert units[0]["unit_number"] == "11"
