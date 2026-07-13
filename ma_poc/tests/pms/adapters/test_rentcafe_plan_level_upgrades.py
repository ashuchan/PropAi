"""RentCafe plan-level → unit-level upgrades (prod 2026-07-12 cohort).

Two independent fixes, both verified live (curl_cffi, zero proxy) during the
80%-gold campaign:

1. ``_SECURECAFE_FP_HDR_RE`` forbade '-' in the plan-name group, so SecureCafe
   ``availableunits.aspx`` headers whose plan name contains a hyphen
   ("1bd x 1ba - 850sqft - The Birch - 1 Bedroom, 1 Bathroom") never matched
   and the page parsed to 0 units despite real ``AvailUnitRow`` rows.

2. RentCafe-vanity ``/floorplans`` pages SSR a ``ysi.unitsList = [...]`` JSON
   array of full unit-level data; the anchor-walk previously ran only the
   AvailUnitRow / fp-unit parsers and dropped it, so the LLM-DOM tier won on
   the same page (parksouth/fairwood/regencyparknorth: 9/11/12 units lost).

Fixtures use the real field shapes observed on the live pages.
"""

from __future__ import annotations

import json

from ma_poc.pms.adapters.rentcafe import (
    parse_rentcafe_ysi_unitslist,
    parse_securecafe_availableunits,
)

# ── Fix 1: hyphenated SecureCafe plan header ────────────────────────────────

# Real markup captured live from
# roundtree-roundtree-mckinley.securecafe.com/.../availableunits.aspx —
# the header carries interior hyphens, the row uses data-label cells.
_SECURECAFE_HYPHENATED = (
    "<div class='floorplanPanel'>"
    "Floor Plan: 1bd x 1ba - 850sqft - The Birch - 1 Bedroom, 1 Bathroom"
    "<table><tr class='AvailUnitRow' id='unitrow_35921988'>"
    "<th data-label='Apartment'>#2963-A1</th>"
    "<td data-label=Sq.Ft.>850</td>"
    "<td data-label='Rent'>$1,149</td></tr></table>"
    "Floor Plan: 2bd x 1ba - 950sqft - The Willow - 2 Bedroom, 1 Bathroom"
    "<table><tr class='AvailUnitRow' id='unitrow_35921990'>"
    "<th data-label='Apartment'>#2923-B3</th>"
    "<td data-label=Sq.Ft.>950</td>"
    "<td data-label='Rent'>$1,249</td></tr></table>"
    "</div>"
)


def test_securecafe_hyphenated_plan_name_now_parses() -> None:
    """Plan names with interior hyphens must not zero out the parse."""
    units = parse_securecafe_availableunits(
        _SECURECAFE_HYPHENATED, "https://x.securecafe.com/onlineleasing/x/availableunits.aspx"
    )
    assert len(units) == 2
    nums = {u["unit_number"] for u in units}
    assert nums == {"2963-A1", "2923-B3"}
    # the hyphenated plan name survives into the record
    fps = {u.get("floor_plan_name") for u in units}
    assert any("The Birch" in (fp or "") for fp in fps)


def test_securecafe_plain_plan_name_still_parses() -> None:
    """Regression guard: the non-hyphenated header path is unchanged."""
    html = (
        "Floor Plan: A1 One Bedroom - 1 Bedroom, 1 Bathroom"
        "<table><tr class='AvailUnitRow' id='unitrow_1'>"
        "<th data-label='Apartment'>#101</th>"
        "<td data-label=Sq.Ft.>700</td>"
        "<td data-label='Rent'>$1,000</td></tr></table>"
    )
    units = parse_securecafe_availableunits(html, "https://x/availableunits.aspx")
    assert len(units) == 1
    assert units[0]["unit_number"] == "101"


# ── Fix 2: ysi.unitsList embedded JSON ──────────────────────────────────────

# Field shape observed live on parksouthapartments.com/floorplans:
# Id, UnitCode, Beds, Baths, SqFt, MinRent, MaxRent, HasSpecials, Amenities,
# AvailableDate, FloorplanName, FloorplanId, isCommercial, PropertyId.
_YSI_ROWS = [
    {
        "Id": 1, "UnitCode": "171-14", "Beds": 1, "Baths": 1, "SqFt": 735,
        "MinRent": 2080.0, "MaxRent": 2080.0, "HasSpecials": False,
        "Amenities": [], "AvailableDate": "2026-07-31T00:00:00",
        "FloorplanName": "Brownstone One Bedroom", "FloorplanId": 5030347,
        "isCommercial": False, "PropertyId": 99,
    },
    {
        "Id": 2, "UnitCode": "166-11", "Beds": 2, "Baths": 2, "SqFt": 1100,
        "MinRent": 2790.0, "MaxRent": 2950.0, "AvailableDate": "2026-08-31T00:00:00",
        "FloorplanName": "Two Bed Deluxe", "FloorplanId": 5030348,
    },
]
_YSI_HTML = (
    "<html><head><script>window.ysi = {};</script></head><body>"
    "<script>ysi.unitsList = " + json.dumps(_YSI_ROWS) + "; ysi.ready();</script>"
    "</body></html>"
)


def test_ysi_unitslist_parses_units_with_rent_sqft_date() -> None:
    units = parse_rentcafe_ysi_unitslist(_YSI_HTML, "https://x.com/floorplans")
    assert len(units) == 2
    u0 = units[0]
    assert u0["unit_number"] == "171-14"
    # rent lands in the schema's market_rent_low (float→int via money_to_int)
    assert u0["market_rent_low"] == 2080
    assert str(u0["sqft"]).startswith("735")
    assert u0["floor_plan_name"] == "Brownstone One Bedroom"
    assert "2026-07-31" in str(u0.get("available_date") or u0.get("availability_date") or "")
    assert u0["extraction_tier"] == "TIER_1_API_RENTCAFE_YSI_UNITSLIST"
    # min/max spread preserved on the 2-bed
    assert units[1]["market_rent_low"] == 2790
    assert units[1]["market_rent_high"] == 2950


def test_ysi_unitslist_absent_marker_returns_empty() -> None:
    assert parse_rentcafe_ysi_unitslist("<html>no marker here</html>", "x") == []


def test_ysi_unitslist_malformed_json_returns_empty_not_raises() -> None:
    bad = "<script>ysi.unitsList = [ {broken , ; </script>"
    assert parse_rentcafe_ysi_unitslist(bad, "x") == []


def test_ysi_unitslist_skips_rows_without_unit_code() -> None:
    rows = [{"Beds": 1, "SqFt": 700, "MinRent": 1500}, {"UnitCode": "A1", "MinRent": 1600}]
    html = "<script>ysi.unitsList = " + json.dumps(rows) + ";</script>"
    units = parse_rentcafe_ysi_unitslist(html, "x")
    assert len(units) == 1
    assert units[0]["unit_number"] == "A1"
