"""RentCafe-hosted portal unit-table parser tests.

Validated 2026-05-18 against the REAL rendered DOM of
rentcafe.com/.../tall-oaks-apartment-homes/default.aspx (fixture
rentcafe_hosted_units.html — 2 units, matches the user's screenshots).
main's generic extract_units_from_dom returns 0 on this structure
(confirmed); hence this dedicated parser. The hosted portal is
server-200 / no bot-wall / no login — so this is a no-residential
Tier-1 recovery path for RentCafe properties.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

from ma_poc.pms.adapters._rentcafe_hosted_table import (
    _iso,
    _rent_range,
    parse_rentcafe_hosted_table,
)

_FIX = Path(__file__).parent / "fixtures" / "rentcafe_hosted_units.html"
_URL = "https://www.rentcafe.com/apartments/mi/kalamazoo/tall-oaks-apartment-homes/default.aspx"
_TODAY = date(2026, 5, 18)


def test_rent_range() -> None:
    assert _rent_range("$1,357 - $1,656") == (1357, 1656)
    assert _rent_range("$1,392") == (1392, 1392)
    assert _rent_range("") == (None, None)
    assert _rent_range("Call for pricing") == (None, None)


def test_iso_month_name_year_inference() -> None:
    # future month this year -> this year
    assert _iso("Available on Aug 3", _TODAY) == "2026-08-03"
    assert _iso("Sep 7", _TODAY) == "2026-09-07"
    # month already passed this year -> next year
    assert _iso("Mar 1", _TODAY) == "2027-03-01"
    assert _iso("8/3/2026", _TODAY) == "2026-08-03"
    assert _iso("Now", _TODAY) == ""
    assert _iso("", _TODAY) == ""


def test_parse_real_fixture() -> None:
    units = parse_rentcafe_hosted_table(_FIX.read_text(), _URL)
    assert len(units) == 2
    u0 = units[0]
    assert u0["unit_number"] == "6869-1A"
    assert u0["market_rent_low"] == 1357
    assert u0["market_rent_high"] == 1656
    assert u0["bedrooms"] == "1"
    assert u0["bathrooms"] == "1"
    assert u0["sqft"] == "716"
    assert u0["availability_status"] == "AVAILABLE"
    assert u0["extraction_tier"] == "TIER_1_DOM_RENTCAFE_HOSTED"
    u1 = units[1]
    assert u1["unit_number"] == "6714-2B"
    assert u1["market_rent_low"] == 1392
    assert u1["market_rent_high"] == 1717
    assert u1["sqft"] == "732"
    # strict-gate ready: every row has unit# + positive rent
    assert all(
        u["unit_number"] and (u["market_rent_low"] or 0) > 0 for u in units
    )


def test_parse_empty_and_non_table() -> None:
    assert parse_rentcafe_hosted_table("", _URL) == []
    assert parse_rentcafe_hosted_table("<div>no units</div>", _URL) == []
    assert parse_rentcafe_hosted_table(
        '<table><tr class="other"><td>x</td></tr></table>', _URL
    ) == []


def test_dedup_by_unit_id() -> None:
    dup = (
        '<table id="floorplanUnits1"><tbody>'
        '<tr class="fp-unit" data-unit-id="999" data-unit-name="A1"'
        ' data-unit-beds="1 Bed" data-unit-baths="1 Bath"'
        ' data-unit-size="700 Sqft" data-unit-rent="$1,000 - $1,100">'
        '<th>A1</th><td>$1,000 - $1,100</td>'
        '<td><span aria-label="Available on Jul 1">Jul 1</span></td></tr>'
        '<tr class="fp-unit" data-unit-id="999" data-unit-name="A1-dup"'
        ' data-unit-beds="1 Bed" data-unit-baths="1 Bath"'
        ' data-unit-size="700 Sqft" data-unit-rent="$1,050">'
        '<th>A1-dup</th><td>$1,050</td>'
        '<td><span aria-label="Available on Jul 2">Jul 2</span></td></tr>'
        "</tbody></table>"
    )
    units = parse_rentcafe_hosted_table(dup, _URL)
    assert len(units) == 1
    assert units[0]["unit_number"] == "A1"
