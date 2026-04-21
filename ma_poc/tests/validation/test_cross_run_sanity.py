"""Tests for cross_run_sanity — historical comparison checks."""

from __future__ import annotations

from ma_poc.validation.cross_run_sanity import check


def test_sanity_no_history_returns_no_flags() -> None:
    result = check({"asking_rent": 1500, "sqft": 750}, None)
    assert result.flags == []


def test_sanity_rent_swing_50pct_flagged() -> None:
    current = {"asking_rent": 3000, "sqft": 750}
    history = {"asking_rent": 1500, "sqft": 750}
    result = check(current, history)
    assert "rent_swing_>50pct" in result.flags


def test_sanity_rent_swing_20pct_warns_not_rejects() -> None:
    current = {"asking_rent": 1850, "sqft": 750}
    history = {"asking_rent": 1500, "sqft": 750}
    result = check(current, history)
    assert "rent_swing_>20pct" in result.flags
    assert "rent_swing_>50pct" not in result.flags


def test_sanity_sqft_changed_flagged() -> None:
    current = {"asking_rent": 1500, "sqft": 900}
    history = {"asking_rent": 1500, "sqft": 750}
    result = check(current, history)
    assert "sqft_changed" in result.flags


def test_sanity_floor_plan_rename_flagged() -> None:
    current = {"floor_plan_type": "Studio", "asking_rent": 1500}
    history = {"floor_plan_type": "1BR", "asking_rent": 1500}
    result = check(current, history)
    assert "floor_plan_renamed" in result.flags


def test_sanity_identical_to_history_no_flags() -> None:
    record = {"asking_rent": 1500, "sqft": 750, "floor_plan_type": "1BR"}
    result = check(record, dict(record))
    assert result.flags == []
