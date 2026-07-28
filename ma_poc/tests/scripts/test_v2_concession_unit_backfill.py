"""Property→unit concession backfill in _format_v2 (2026-07-12).

The property-level banner has TWO sources: the scraper's
``concessions_text`` (smeared to units by scraper.py Step 9c) and the
page-metadata ``md["concessions"]`` — which only ever fed the property
field. 94 props / 2,539 units in the 2026-07-11 canary shipped with a
property-level banner but empty unit concession columns. _format_v2 now
backfills at the formatting chokepoint, covering every source and every
path (including timeout-salvage records that bypass Step 9c).
"""
from __future__ import annotations

from ma_poc.scripts.runners.jugnu import _format_v2

_BANNER = "Get 1 month FREE on select homes!"


def _raw_unit(i):
    return {
        "unit_number": f"10{i}", "floor_plan_name": "A1",
        "bedrooms": "1", "bathrooms": "1", "sqft": "700",
        "market_rent_low": 1500, "market_rent_high": 1500,
        "availability_status": "AVAILABLE",
    }


def test_md_concessions_backfills_units():
    result = {
        "units": [_raw_unit(i) for i in range(3)],
        "property_metadata": {"concessions": _BANNER},
        "base_url": "https://x.test/",
    }
    prop = _format_v2(result, {"property_id": "1", "apartment_id": "1"})
    assert prop["concessions"] == _BANNER
    units = prop["units"]
    assert units and all(u.get("concession_text") == _BANNER for u in units)
    assert all(u.get("offer_type") == "free_rent" for u in units)
    assert all(u.get("offer_value") == "1 month" for u in units)


def test_unit_level_text_not_overwritten():
    u0 = _raw_unit(0)
    u0["concession_text"] = "2 weeks free on THIS unit only"
    result = {
        "units": [u0, _raw_unit(1)],
        "property_metadata": {"concessions": _BANNER},
        "base_url": "https://x.test/",
    }
    prop = _format_v2(result, {"property_id": "1", "apartment_id": "1"})
    texts = [u.get("concession_text") for u in prop["units"]]
    assert "2 weeks free on THIS unit only" in texts  # kept
    assert _BANNER in texts                            # backfilled


def test_no_banner_no_backfill():
    result = {
        "units": [_raw_unit(0)],
        "property_metadata": {},
        "base_url": "https://x.test/",
    }
    prop = _format_v2(result, {"property_id": "1", "apartment_id": "1"})
    assert prop["concessions"] is None
    assert prop["units"][0].get("concession_text") is None


def test_plan_summaries_emit_separately_without_fallback_unit_id():
    """A plan card never leaks an ``inferred_*`` ID into ``units``."""
    result = {
        "units": [],
        "plan_summaries": [
            {
                "floor_plan_name": "A1",
                "bedrooms": 1,
                "bathrooms": 1,
                "sqft": 700,
                "asking_rent": 1500,
                "data_quality_flag": "UNIT_ROUTE_UNVERIFIED",
            }
        ],
        "base_url": "https://x.test/",
    }

    prop = _format_v2(result, {"property_id": "1", "apartment_id": "1"})

    assert prop["units"] == []
    assert len(prop["floor_plans"]) == 1
    plan = prop["floor_plans"][0]
    assert plan["unit_id"] is None
    assert plan["unit_name"] is None
    assert plan["is_floor_plan_level"] is True
    assert "inferred_" not in str(plan)
    assert plan["rent_low"] == 1500.0
