"""Apts247 rent-range fix (#25, 2026-07-16).

Apts247 gives per-unit rent as a lease-term RANGE ("$1,960-$3,112"). The
adapter used to take only the first number and mirror it into both
rent_low/rent_high, losing the high end on ~88% of units (0 preserved a real
range in the 07-12 run). Live-verified: all sampled units carry range rents.
"""

from __future__ import annotations

from ma_poc.pms.adapters.apts247 import _rent_range, parse_apts247_floorplans

# ── _rent_range helper ───────────────────────────────────────────────────────

def test_rent_range_parses_range():
    assert _rent_range("$1,960-$3,112") == (1960, 3112)
    assert _rent_range("$1700-$3112") == (1700, 3112)


def test_rent_range_single_value():
    assert _rent_range("$899") == (899, 899)


def test_rent_range_non_numeric_and_empty():
    assert _rent_range("Call for details") == (None, None)
    assert _rent_range("") == (None, None)
    assert _rent_range(None) == (None, None)


def test_rent_range_orders_low_high():
    # min/max regardless of the order the numbers appear
    assert _rent_range("$3,112-$1,960") == (1960, 3112)


def test_rent_range_drops_stray_small_numbers():
    # a "$0 deposit"-style stray shouldn't become the low
    assert _rent_range("$0 - $1,850") == (1850, 1850)


# ── parse_apts247_floorplans: range preserved end-to-end ─────────────────────

_ENVELOPE = {
    "objects": [
        {
            "name": "Plan A Studio",
            "display_bed": "Studio",
            "bath": 1,
            "sq_ft": "468",
            "rent": "$1,700-$3,112",
            "units": [
                {"id": 1038, "number": "1038A", "rent": "$1,960-$3,112",
                 "sq_ft": "468", "available_date": "2026-08-01", "floor": "10"},
                {"id": 1040, "number": "1040", "rent": "$1,850",  # single
                 "sq_ft": "468", "available_date": "2026-08-15"},
            ],
        },
        {
            "name": "Plan B",
            "display_bed": "1 Bed",
            "bath": 1,
            "sq_ft": "621",
            "rent": "$2,100-$2,900",
            "units": [],  # no available units → plan-level fallback
        },
    ]
}


def test_unit_range_preserved():
    units = parse_apts247_floorplans(_ENVELOPE, "https://x.com/")
    u0 = next(u for u in units if u["unit_number"] == "1038A")
    assert u0["market_rent_low"] == 1960
    assert u0["market_rent_high"] == 3112     # was 1960 before the fix
    assert u0["market_rent_low"] < u0["market_rent_high"]
    assert "1,960" in u0["rent_range"] and "3,112" in u0["rent_range"]


def test_unit_single_rent_low_equals_high():
    units = parse_apts247_floorplans(_ENVELOPE, "https://x.com/")
    u1 = next(u for u in units if u["unit_number"] == "1040")
    assert u1["market_rent_low"] == 1850
    assert u1["market_rent_high"] == 1850


def test_plan_level_fallback_uses_range():
    units = parse_apts247_floorplans(_ENVELOPE, "https://x.com/")
    planb = [u for u in units if u["floor_plan_name"] == "Plan B"]
    assert len(planb) == 1
    assert planb[0]["market_rent_low"] == 2100
    assert planb[0]["market_rent_high"] == 2900


def test_unit_missing_rent_falls_back_to_plan_range():
    env = {
        "objects": [
            {
                "name": "Plan C", "display_bed": "2 Bed", "bath": 2, "sq_ft": "900",
                "rent": "$2,400-$2,800",
                "units": [{"id": 7, "number": "7-17", "rent": "", "sq_ft": "900"}],
            }
        ]
    }
    units = parse_apts247_floorplans(env, "https://x.com/")
    u = next(u for u in units if u["unit_number"] == "7-17")
    assert u["market_rent_low"] == 2400
    assert u["market_rent_high"] == 2800
