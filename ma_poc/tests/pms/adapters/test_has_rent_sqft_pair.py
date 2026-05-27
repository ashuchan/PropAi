"""_has_rent_sqft_pair guard tests (2026-05-23).

This guard is wired into TIER_1_5_EMBEDDED and TIER_3_DOM acceptance
sites in generic.py. When True the planner's STOP decision is honored;
when False the cascade is forced to continue regardless of planner
opinion. This pins the rent+sqft pair semantics that drive the
fall-through behavior on the canary's 110+ partial-cohort properties
(43 TIER_1_5_EMBEDDED + 67 TIER_3_DOM).
"""
from __future__ import annotations

from ma_poc.pms.adapters.generic import _has_rent_sqft_pair


def _unit(rent: int | None = None, sqft: int | str | None = None,
          rent_range: str = "") -> dict:
    """Build a unit dict in the v2 schema shape."""
    u: dict = {}
    if rent is not None:
        u["market_rent_low"] = rent
    if sqft is not None:
        u["sqft"] = sqft
    if rent_range:
        u["rent_range"] = rent_range
    return u


# ─── happy path: pair present ─────────────────────────────────────────


def test_returns_true_when_at_least_one_unit_has_rent_and_sqft() -> None:
    units = [
        _unit(rent=None, sqft=750),  # sqft only — doesn't qualify
        _unit(rent=1500, sqft=850),  # has both — qualifies
        _unit(rent=1700, sqft=None),  # rent only
    ]
    assert _has_rent_sqft_pair(units) is True


def test_returns_true_when_rent_range_string_with_sqft() -> None:
    """Legacy rent_range string format should also count as rent."""
    units = [{"rent_range": "$1,500 - $2,000", "sqft": "850"}]
    assert _has_rent_sqft_pair(units) is True


def test_returns_true_when_legacy_rent_low_and_area_keys() -> None:
    """Legacy schema (rent_low / area) is also recognized."""
    units = [{"rent_low": 1500, "area": 850}]
    assert _has_rent_sqft_pair(units) is True


def test_returns_true_when_sqft_is_string_with_digits() -> None:
    units = [{"market_rent_low": 1500, "sqft": "850"}]
    assert _has_rent_sqft_pair(units) is True


# ─── rejection: no pair ──────────────────────────────────────────────


def test_returns_false_when_all_units_lack_rent() -> None:
    units = [_unit(sqft=750), _unit(sqft=850), _unit(sqft=950)]
    assert _has_rent_sqft_pair(units) is False


def test_returns_false_when_all_units_lack_sqft() -> None:
    units = [_unit(rent=1500), _unit(rent=1700)]
    assert _has_rent_sqft_pair(units) is False


def test_returns_false_when_rent_in_some_sqft_in_others_never_together() -> None:
    """Mixed: rent in one, sqft in another, but never paired in one unit."""
    units = [
        _unit(rent=1500, sqft=None),
        _unit(rent=None, sqft=850),
    ]
    assert _has_rent_sqft_pair(units) is False


def test_returns_false_on_sqft_minus_one_sentinel() -> None:
    """``area=-1`` is the legacy sentinel for missing — must NOT count."""
    units = [{"market_rent_low": 1500, "area": -1}]
    assert _has_rent_sqft_pair(units) is False


def test_returns_false_on_zero_sqft() -> None:
    units = [{"market_rent_low": 1500, "sqft": 0}]
    assert _has_rent_sqft_pair(units) is False


def test_returns_false_on_empty_rent_range_string() -> None:
    """An empty rent_range string must not count as rent present."""
    units = [{"rent_range": "", "sqft": "850"}]
    assert _has_rent_sqft_pair(units) is False


def test_returns_false_on_rent_range_without_digits() -> None:
    """Garbage rent_range like '$' or 'Call' must not count."""
    units = [{"rent_range": "Call for pricing", "sqft": "850"}]
    assert _has_rent_sqft_pair(units) is False


# ─── edge cases ──────────────────────────────────────────────────────


def test_returns_false_on_empty_list() -> None:
    assert _has_rent_sqft_pair([]) is False


def test_returns_false_on_none() -> None:
    # Defensive — never raise even on bad input.
    assert _has_rent_sqft_pair(None) is False  # type: ignore[arg-type]


def test_skips_non_dict_items() -> None:
    """A list with junk items should still find the valid pair."""
    units = [None, "string", 42, {"market_rent_low": 1500, "sqft": 850}]
    assert _has_rent_sqft_pair(units) is True  # type: ignore[arg-type]


def test_tolerates_garbage_sqft_string() -> None:
    """Non-numeric sqft strings must not raise."""
    units = [{"market_rent_low": 1500, "sqft": "TBD"}]
    assert _has_rent_sqft_pair(units) is False
