"""RentPress data-floorplans static parser (prod 2026-07-12 encoreskyline cohort).

RentPress (RentCafe-synced) WordPress sites embed the full unit inventory as an
entity-escaped JSON array in ``<div id="rentpress-app" data-floorplans='[...]'>``
in the initial HTML. The encoreskyline adapter only drove the Jonah per-plan
click flow and never read it, so these sites fell to the LLM/failed tier.
Fixture keys mirror the live themobilelofts.com structure.
"""

from __future__ import annotations

import html as _html
import json

from ma_poc.pms.adapters._encoreskyline_units import (
    parse_rentpress_data_floorplans,
)

_PLANS = [
    {
        "floorplan_name": "The Emogene", "floorplan_bedrooms": 1,
        "floorplan_sqft_min": 653,
        "units": [
            {
                "unit_code": "32679710", "unit_name": "OSL-D1",
                "unit_available_on": "09/10/2026", "unit_available": 1,
                "unit_rent_best": 1589, "unit_rent_min": 1589,
                "unit_bedrooms": 1, "unit_bathrooms": 1, "unit_sqft": 653,
            }
        ],
    },
    {
        "floorplan_name": "The Nicholai", "floorplan_bedrooms": 2,
        "units": [
            {
                "unit_code": "32679999", "unit_name": "OSL-D10",
                "unit_available_on": "10/01/2026", "unit_available": 1,
                "unit_rent_best": 1759, "unit_bedrooms": 2,
                "unit_bathrooms": 2, "unit_sqft": 922,
            },
            {  # a not-available unit
                "unit_code": "32680000", "unit_name": "OSL-D11",
                "unit_available": 0, "unit_rent_best": 1800,
                "unit_bedrooms": 2, "unit_sqft": 922,
            },
        ],
    },
]


def _wrap(plans) -> str:
    attr = _html.escape(json.dumps(plans), quote=True)
    return f"<div id='rentpress-app' data-floorplans=\"{attr}\"></div>"


def test_parses_units_with_rent_sqft_date() -> None:
    units = parse_rentpress_data_floorplans(_wrap(_PLANS), "https://x.com/")
    assert len(units) == 3
    by = {u["unit_number"]: u for u in units}
    d1 = by["OSL-D1"]
    assert d1["market_rent_low"] == 1589
    assert d1["bedrooms"] == "1"
    assert str(d1["sqft"]).startswith("653")
    assert d1["available_date"] == "2026-09-10"          # MM/DD/YYYY → ISO
    assert d1["availability_status"] == "AVAILABLE"
    assert d1["floor_plan_name"] == "The Emogene"
    assert d1["extraction_tier"] == "TIER_1_DOM_RENTPRESS"
    assert d1["source_ids"]["rentpress_unit_code"] == "32679710"
    # unit_available=0 → UNAVAILABLE
    assert by["OSL-D11"]["availability_status"] == "UNAVAILABLE"


def test_absent_attribute_returns_empty() -> None:
    assert parse_rentpress_data_floorplans("<html>no rentpress</html>", "x") == []


def test_malformed_json_does_not_raise() -> None:
    bad = "<div data-floorplans='[{broken'></div>"
    assert parse_rentpress_data_floorplans(bad, "x") == []


def test_floorplan_without_units_yields_nothing() -> None:
    plans = [{"floorplan_name": "Empty", "floorplan_bedrooms": 1, "units": []}]
    assert parse_rentpress_data_floorplans(_wrap(plans), "x") == []


def test_unit_falls_back_to_floorplan_beds_and_sqft() -> None:
    plans = [{
        "floorplan_name": "FP", "floorplan_bedrooms": 3, "floorplan_sqft_min": 1200,
        "units": [{"unit_name": "A1", "unit_rent_best": 2000, "unit_available": 1}],
    }]
    units = parse_rentpress_data_floorplans(_wrap(plans), "x")
    assert len(units) == 1
    assert units[0]["bedrooms"] == "3"
    assert str(units[0]["sqft"]).startswith("1200")
