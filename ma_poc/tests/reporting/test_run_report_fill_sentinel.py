"""Field-fill rates must treat typed ABSENT sentinels as NOT filled.

Regression 2026-07-28. ``run_report.build`` counted a field as filled with
an inline ``value not in (None, "", "null", [], {})`` test. ``area`` says
"unknown" with the integer ``-1`` sentinel (``schema_v2._format_area``),
which is not empty — so every unit scored as area-filled. All 100 shard
reports of run-2026-07-27-full-0d54ca7 published ``area`` fill = 100.0%
against a true 92.61% (97,212 positive areas of 104,964 units; 7,752 rows
carried ``-1``).

The same recompute lives in ``scripts/build_excel_report._recompute_report``
for runs with no ``report.json``; it is pinned here too so the two paths
cannot drift apart again.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ma_poc.core.schema_v2 import field_is_absent
from ma_poc.reporting.run_report import build
from ma_poc.scripts.build_excel_report import _recompute_report


def _unit(**over: Any) -> dict[str, Any]:
    u: dict[str, Any] = {
        "unit_id": "101",
        "beds": 1,
        "baths": 1.0,
        "area": 750,
        "rent_low": 1500.0,
        "available_date": "2026-08-01",
        "availability_status": "AVAILABLE",
        "floor_plan_name": "A1",
        "floor": "1",
        "building": "1",
        "concession_text": "1 month free",
        "source_ids": {"sightmap_unit_id": "9"},
    }
    u.update(over)
    return u


def _prop(units: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "_meta": {"verdict": "SUCCESS", "canonical_id": "p1"},
        "_extract_result": {"tier_used": "TIER_1_API"},
        "units": units,
    }


def test_area_minus_one_sentinel_is_not_filled(tmp_path: Path) -> None:
    """4 real areas + 1 sentinel row => 80%, not 100%."""
    units = [_unit(area=a) for a in (700, 800, 900, 1000, -1)]
    report = build([_prop(units)], tmp_path, "2026-07-27")
    assert report["data_quality"]["total_units"] == 5
    assert report["field_fill_rates"]["area"] == 80.0


def test_all_sentinel_areas_report_zero_fill(tmp_path: Path) -> None:
    units = [_unit(area=-1) for _ in range(4)]
    report = build([_prop(units)], tmp_path, "2026-07-27")
    assert report["field_fill_rates"]["area"] == 0.0


def test_positive_areas_still_report_full_fill(tmp_path: Path) -> None:
    """Guards against over-correcting: real measurements stay filled."""
    units = [_unit(area=a) for a in (150, 751, 10_000)]
    report = build([_prop(units)], tmp_path, "2026-07-27")
    assert report["field_fill_rates"]["area"] == 100.0


def test_other_fields_unaffected_by_the_area_sentinel(tmp_path: Path) -> None:
    """``-1`` is an ``area`` sentinel only — it must not be generalised.

    A floor literally reported as "-1" (a basement level) is real data.
    """
    units = [_unit(area=-1, floor="-1", beds=0, baths=0.0, rent_low=1.0)]
    report = build([_prop(units)], tmp_path, "2026-07-27")
    r = report["field_fill_rates"]
    assert r["area"] == 0.0
    assert r["floor"] == 100.0
    assert r["beds"] == 100.0  # 0 beds = studio, a real value
    assert r["baths"] == 100.0
    assert r["rent_low"] == 100.0


def test_excel_recompute_path_applies_the_same_sentinel_rule() -> None:
    units = [_unit(area=a) for a in (700, 800, 900, 1000, -1)]
    rep = _recompute_report([_prop(units)])
    assert rep["field_fill_rates"]["area"] == 80.0
    assert rep["field_fill_rates"]["unit_id"] == 100.0


# ── field_is_absent value table ──────────────────────────────────────────────
#
# The sentinel matcher is a value test, not a substring/regex test. The table
# below fixes the boundary: what it MUST treat as absent, and — the part that
# matters — everything nearby that it must NOT.
@pytest.mark.parametrize(
    ("field", "value", "absent", "why"),
    [
        # MUST match — the area sentinel in every shape it can arrive in
        ("area", -1, True, "the canonical int sentinel"),
        ("area", -1.0, True, "float form of the same sentinel"),
        ("area", "-1", True, "string form (older/JSON-roundtripped rows)"),
        ("area", " -1 ", True, "string form with whitespace"),
        ("area", None, True, "generic empty"),
        ("area", "", True, "generic empty"),
        # MUST NOT match — real area measurements
        ("area", 1, False, "1 sqft is junk data, but it is not the sentinel"),
        ("area", 750, False, "an ordinary real area"),
        ("area", -2, False, "a different negative is not the sentinel"),
        ("area", 0, False, "zero is not the sentinel"),
        ("area", "-10", False, "must compare numerically, not by prefix"),
        ("area", "-1 bedroom", False, "must not match text containing -1"),
        ("area", "1-1", False, "must not match a value merely containing '-1'"),
        ("area", True, False, "bool must never collide with a numeric sentinel"),
        ("area", False, False, "bool must never collide with a numeric sentinel"),
        # MUST NOT match — -1 on a field that has no such sentinel
        ("floor", -1, False, "floor -1 = basement, real data"),
        ("floor", "-1", False, "floor -1 = basement, real data"),
        ("beds", -1, False, "no sentinel registered for beds"),
        ("rent_low", -1, False, "no sentinel registered for rent_low"),
        ("building", "-1", False, "no sentinel registered for building"),
        # generic empties still absent on non-sentinel fields
        ("source_ids", {}, True, "empty dict"),
        ("source_ids", {"a": "1"}, False, "populated dict"),
        ("building", "null", True, "the literal string 'null'"),
        ("beds", 0, False, "studio"),
    ],
)
def test_field_is_absent_table(field: str, value: Any, absent: bool, why: str) -> None:
    assert field_is_absent(field, value) is absent, why
