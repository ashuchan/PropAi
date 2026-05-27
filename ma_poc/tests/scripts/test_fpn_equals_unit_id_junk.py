"""Drop ``floor_plan_name`` when it equals ``unit_id`` (87b837b QC).

555 units in the 87b837b canary had ``floor_plan_name == unit_id``
(both ``"259"``, ``"103"``, ``"B3"``, etc.) — the adapter couldn't
find a real plan name and fell back to the unit number. That's not a
real plan name; it pollutes plan-level analytics. The fix nulls
``floor_plan_name`` in ``_format_v2_unit`` so downstream consumers
treat the unit as having no plan name (and ``compute_floor_plan_id``
will derive a deterministic id from beds+baths instead).
"""
from __future__ import annotations

from datetime import datetime, UTC

from ma_poc.scripts.runners.jugnu import _format_v2_unit


_TS = datetime.now(UTC)


def _emit(unit_input: dict) -> dict:
    """Format one unit through the v2 pipeline (single-unit call)."""
    return _format_v2_unit(unit_input, _TS, property_id="testprop")


def test_fpn_equal_to_unit_id_is_nulled() -> None:
    """The canonical case — both fields are ``"259"``. After v2 format
    the output ``floor_plan_name`` must be None."""
    out = _emit({"unit_number": "259", "floor_plan_name": "259", "beds": 1, "baths": 1})
    assert out["floor_plan_name"] is None
    # unit_id is preserved
    assert out["unit_id"] == "259"


def test_fpn_alphanumeric_match_is_nulled() -> None:
    """B3 / A1 / 0528 — operator unit numbers reused as plan names."""
    for shared in ("B3", "A1", "0528", "PH-101"):
        out = _emit({"unit_number": shared, "floor_plan_name": shared, "beds": 2, "baths": 2})
        assert out["floor_plan_name"] is None, f"failed for {shared!r}"


def test_real_plan_name_distinct_from_unit_id_is_kept() -> None:
    """The expected happy path — distinct values stay distinct."""
    out = _emit({
        "unit_number": "259",
        "floor_plan_name": "The Madison",
        "beds": 1, "baths": 1,
    })
    assert out["floor_plan_name"] == "The Madison"
    assert out["unit_id"] == "259"


def test_fpn_whitespace_match_is_nulled() -> None:
    """Leading/trailing whitespace differences shouldn't keep the
    junk match alive — the comparison normalises via .strip()."""
    out = _emit({"unit_number": "259", "floor_plan_name": "  259 ", "beds": 1, "baths": 1})
    assert out["floor_plan_name"] is None


def test_empty_fpn_with_unit_id_is_kept_as_none() -> None:
    """No floor_plan_name input → output is None (existing behaviour);
    the new guard doesn't fire on the empty side."""
    out = _emit({"unit_number": "259", "floor_plan_name": "", "beds": 1, "baths": 1})
    assert out["floor_plan_name"] is None


def test_floor_plan_id_still_computed_after_nulling() -> None:
    """After P5 junk-nulls the floor_plan_name, the floor_plan_id
    should still be derivable from beds+baths (key analytics
    invariant — units with same bed/bath count share an id)."""
    a = _emit({"unit_number": "259", "floor_plan_name": "259", "beds": 1, "baths": 1.0})
    b = _emit({"unit_number": "317", "floor_plan_name": "317", "beds": 1, "baths": 1.0})
    # Both nulled the fpn; both should still have a floor_plan_id
    assert a["floor_plan_id"] is not None
    assert b["floor_plan_id"] is not None
    # And — since neither has a real plan name — they should share
    # the deterministic beds+baths-derived id
    assert a["floor_plan_id"] == b["floor_plan_id"], (
        "Same beds+baths but different ids — compute_floor_plan_id "
        "isn't deterministic when fp_name is None."
    )
