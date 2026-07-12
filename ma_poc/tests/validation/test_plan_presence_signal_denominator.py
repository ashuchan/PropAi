"""Plan-presence marker rows must not dilute rent/area-signal denominators.

2026-07-11 quality sweep. ``sightmap.parse_sightmap_payload`` emits one
marker row per sold-out floor plan (UNAVAILABLE / available_units=0 /
``data_quality_flag="SIGHTMAP_PLAN_PRESENCE"``) for catalogue completeness.
Counting those in ``property_has_rent_signal`` / ``property_has_area_signal``
denominators made properties whose every AVAILABLE unit has rent+sqft+uid
read as "no rent signal" whenever sold-out plans outnumbered available units
(thecharlesslc: 21 real priced units + 26 markers → 45% < 0.5) — firing the
Path-B retry for nothing and mislabelling the tier ``*_PLAN_LEVEL`` +
verdict SUCCESS_PLAN_LEVEL. 2026-07-11 canary: 109 props / 1,750 real
priced unit rows mislabelled; 90 flip back with this fix, 19 stay
(honestly) plan-level.
"""

from ma_poc.reporting.verdict import Verdict, compute
from ma_poc.validation.schema_gate import (
    _is_plan_presence_marker,
    property_has_area_signal,
    property_has_rent_signal,
)


def _real_unit(i, rent=1650, area=703):
    return {
        "unit_id": f"unit-{i}", "floor_plan_name": "A1",
        "market_rent_low": rent, "sqft": area,
        "availability_status": "AVAILABLE", "available_units": "1",
    }


def _sightmap_marker(i):
    # exact shape parse_sightmap_payload emits for a sold-out plan
    return {
        "floor_plan_name": f"HH{i}", "unit_number": "", "sqft": "",
        "rent_range": "", "availability_status": "UNAVAILABLE",
        "available_units": "0",
        "data_quality_flag": "SIGHTMAP_PLAN_PRESENCE",
    }


def _v2_marker(i):
    # same row after v2 formatting: flag dropped, uid minted as inferred_*
    return {
        "unit_id": f"inferred_{i:016x}", "floor_plan_name": f"HH{i}",
        "rent_low": None, "area": None,
        "availability_status": "UNAVAILABLE", "available_units": None,
    }


# --- marker predicate -------------------------------------------------------


def test_explicit_sightmap_flag_is_marker():
    assert _is_plan_presence_marker(_sightmap_marker(1)) is True


def test_v2_formatted_marker_shape_is_marker():
    assert _is_plan_presence_marker(_v2_marker(1)) is True


def test_real_unit_is_not_marker():
    assert _is_plan_presence_marker(_real_unit(1)) is False


def test_real_norent_unit_is_not_marker():
    # a real unit with identity but no rent stays in the denominator
    u = _real_unit(1, rent=None)
    u.pop("market_rent_low")
    assert _is_plan_presence_marker(u) is False


def test_unavailable_but_identified_unit_is_not_marker():
    u = _real_unit(1)
    u["availability_status"] = "UNAVAILABLE"
    u.pop("market_rent_low")
    assert _is_plan_presence_marker(u) is False  # has real unit_id


def test_unknown_availability_inferred_row_is_not_marker():
    u = _v2_marker(1)
    u["availability_status"] = "UNKNOWN"
    assert _is_plan_presence_marker(u) is False


# --- signal denominators ----------------------------------------------------


def test_thecharles_shape_now_has_rent_signal():
    # 21 real priced units + 26 sold-out markers (21/47=45% used to fail)
    units = [_real_unit(i) for i in range(21)] + [_sightmap_marker(i) for i in range(26)]
    assert property_has_rent_signal(units) is True
    assert property_has_area_signal(units) is True


def test_v2_shaped_mixed_roster_has_signal():
    units = [
        {"unit_id": f"u{i}", "rent_low": 1650, "area": 700,
         "availability_status": "AVAILABLE"} for i in range(5)
    ] + [_v2_marker(i) for i in range(20)]
    assert property_has_rent_signal(units) is True
    assert property_has_area_signal(units) is True


def test_all_marker_roster_still_no_signal():
    # pure plan-presence catalogue → correctly stays plan-level
    units = [_sightmap_marker(i) for i in range(10)]
    assert property_has_rent_signal(units) is False
    assert property_has_area_signal(units) is False


def test_real_units_without_rent_still_no_signal():
    # markers excluded but real units genuinely lack rent → still no signal
    units = []
    for i in range(5):
        u = _real_unit(i)
        u.pop("market_rent_low")
        units.append(u)
    units += [_sightmap_marker(i) for i in range(5)]
    assert property_has_rent_signal(units) is False


def test_majority_gate_still_applies_to_real_units():
    # 1 priced + 3 unpriced REAL units → 25% < 0.5 → no signal (unchanged)
    units = [_real_unit(0)]
    for i in range(1, 4):
        u = _real_unit(i)
        u.pop("market_rent_low")
        units.append(u)
    assert property_has_rent_signal(units) is False


def test_empty_list_unchanged():
    assert property_has_rent_signal([]) is False
    assert property_has_area_signal([]) is False


# --- end-to-end: verdict no longer downgrades -------------------------------


def test_verdict_success_for_priced_units_plus_markers():
    units = [_real_unit(i) for i in range(21)] + [_sightmap_marker(i) for i in range(26)]
    v = compute(extract_result={"units": units}, units=units)
    assert v.verdict == Verdict.SUCCESS


def test_verdict_still_plan_level_for_pure_marker_roster():
    # all-inferred/no-uid marker roster → plan-level (unchanged behaviour)
    units = [_v2_marker(i) for i in range(10)]
    v = compute(extract_result={"units": units}, units=units)
    assert v.verdict == Verdict.SUCCESS_PLAN_LEVEL
