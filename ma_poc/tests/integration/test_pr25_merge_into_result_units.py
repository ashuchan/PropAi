"""PR25 — _merge_into_result_units rank-ladder tests (deferred from PR24)."""

from __future__ import annotations

import sys
import types

for _name in ("playwright", "playwright.async_api"):
    if _name not in sys.modules:
        _mod = types.ModuleType(_name)
        _mod.BrowserContext = object  # type: ignore[attr-defined]
        _mod.Page = object  # type: ignore[attr-defined]
        _mod.async_playwright = object  # type: ignore[attr-defined]
        sys.modules[_name] = _mod

from ma_poc.pms.adapters.generic import _merge_into_result_units


def test_merge_rank1_unit_id_match() -> None:
    """Rank 1: same unit_id on both sides → field union into single output row."""
    existing = [{"unit_id": "U1", "beds": 2, "sqft": 900}]
    incoming = [{"unit_id": "U1", "rent_low": 1500, "available_date": "2026-06-01"}]
    result = _merge_into_result_units(existing, incoming)

    assert len(result) == 1, "matching unit_id must merge into single row"
    assert result[0]["unit_id"] == "U1"
    assert result[0]["beds"] == 2
    assert result[0]["rent_low"] == 1500
    assert result[0]["available_date"] == "2026-06-01"


def test_merge_rank2_floor_plan_fan_out() -> None:
    """Rank 2: DOM unit-rows match an api floor-plan-row on (floor_plan_name, beds, baths)
    → field union."""
    existing = [
        {"floor_plan_name": "A1", "beds": 1, "baths": 1, "sqft": 750},
    ]
    incoming = [
        {"floor_plan_name": "A1", "beds": 1, "baths": 1, "rent_low": 1400},
    ]
    result = _merge_into_result_units(existing, incoming)

    assert len(result) == 1, "same floor plan name+beds+baths must merge"
    assert result[0]["floor_plan_name"] == "A1"
    assert result[0]["sqft"] == 750
    assert result[0]["rent_low"] == 1400


def test_merge_rank2_blocks_when_beds_mismatch() -> None:
    """Two floor plans named 'A1' with different bed counts must remain separate."""
    existing = [{"floor_plan_name": "A1", "beds": 1, "baths": 1, "sqft": 750}]
    incoming = [{"floor_plan_name": "A1", "beds": 2, "baths": 1, "sqft": 950}]
    result = _merge_into_result_units(existing, incoming)

    assert len(result) == 2, "different bed counts under same name must not merge"
    assert result[0]["beds"] == 1
    assert result[1]["beds"] == 2


def test_merge_rank3_fuzzy_by_beds_baths_sqft() -> None:
    """Rank 3: sqft 752 and 748 bucket to the same 100-sqft bucket → fuzzy match fires."""
    existing = [{"beds": 1, "baths": 1, "sqft": 752, "rent_low": 1400}]
    incoming = [{"beds": 1, "baths": 1, "sqft": 748, "available_date": "2026-07-01"}]
    result = _merge_into_result_units(existing, incoming)

    # 752 // 100 = 7, 748 // 100 = 7 → same bucket → fuzzy match
    assert len(result) == 1, "same sqft bucket must fuzzy-match and merge"
    assert result[0]["rent_low"] == 1400
    assert result[0]["available_date"] == "2026-07-01"


def test_merge_field_union_preserves_both_sides() -> None:
    """Existing wins on tie (field is not None); new fields from incoming are additive."""
    existing = [{"unit_id": "U1", "rent_low": 9999}]
    incoming = [{"unit_id": "U1", "rent_low": 1500, "sqft": 800}]
    result = _merge_into_result_units(existing, incoming)

    assert len(result) == 1
    # Existing non-null value must NOT be overwritten
    assert result[0]["rent_low"] == 9999, "existing non-null field must not be overwritten"
    # New field from incoming must be added
    assert result[0]["sqft"] == 800, "new field from incoming must be appended"


def test_merge_no_match_appends() -> None:
    """New unit with no shared identity → appended as a new row."""
    existing = [{"unit_id": "U1", "rent_low": 1500}]
    incoming = [{"unit_id": "U2", "rent_low": 1700}]
    result = _merge_into_result_units(existing, incoming)

    assert len(result) == 2, "non-matching unit must be appended"
    ids = {r.get("unit_id") for r in result}
    assert ids == {"U1", "U2"}
