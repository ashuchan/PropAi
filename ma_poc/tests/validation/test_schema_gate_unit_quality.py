"""Tests for F1 — schema-gated unit quality functions.

Stage 1 contract change (2026-05-12): ``is_substantive`` now delegates to
``unit_validity.is_valid_unit``. The new bar requires at least one
**numeric physical dimension** (beds / baths / area) — rent alone or
floor_plan_name alone no longer qualifies. See
docs/2026_05_11_regressions_fix_design.md.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ma_poc.validation.schema_gate import (
    _has_area,
    _has_rent,
    is_substantive,
    property_has_area_signal,
    property_has_rent_signal,
    property_passes_quality_gate,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _hollow() -> dict[str, Any]:
    return {
        "unit_id": "abc123",
        "beds": None,
        "baths": None,
        "floor_plan_name": None,
        "area": -1,
        "rent_low": None,
        "rent_high": None,
    }


def _with(**kw: Any) -> dict[str, Any]:
    return {**_hollow(), **kw}


# ── _is_present ──────────────────────────────────────────────────────────────


def test_is_substantive_detects_beds_present() -> None:
    assert is_substantive(_with(beds=1))


def test_is_substantive_rejects_rent_low_alone() -> None:
    """Stage 1 contract: rent is NOT a physical dimension; alone it does
    not qualify a row as a unit. (Pre-Stage-1: this asserted True.)"""
    assert not is_substantive(_with(rent_low=1500))


def test_is_substantive_rejects_floor_plan_name_alone() -> None:
    """Stage 1 contract: floor_plan_name is identity-text, not a dimension;
    alone it does not qualify a row. Closes the Skyline-at-Kessler 2026-05-11
    regression shape (rows with only ``floor_plan_name="Hoboken"``).
    (Pre-Stage-1: this asserted True.)"""
    assert not is_substantive(_with(floor_plan_name="Studio A"))


def test_is_substantive_detects_baths_present() -> None:
    """New under Stage 1: baths alone is a dimension and qualifies."""
    assert is_substantive(_with(baths=1.5))


def test_is_substantive_detects_area_present() -> None:
    assert is_substantive(_with(area=750))


def test_is_substantive_rejects_area_sentinel_minus_one() -> None:
    assert not is_substantive(_with(area=-1))


def test_is_substantive_rejects_empty_string_floor_plan() -> None:
    assert not is_substantive(_with(floor_plan_name="   "))


def test_is_substantive_rejects_all_null_unit() -> None:
    assert not is_substantive(_hollow())


# ── property_passes_quality_gate ─────────────────────────────────────────────


def test_property_passes_quality_gate_empty_list_fails() -> None:
    assert not property_passes_quality_gate([])


def test_property_passes_quality_gate_all_substantive_passes() -> None:
    """All units carry a dimension (Stage 1 bar). Pre-Stage-1 this mixed
    in rent_low-only and floor_plan_name-only rows; those no longer qualify."""
    units = [_with(beds=1), _with(area=750), _with(baths=2.0)]
    assert property_passes_quality_gate(units)


def test_property_passes_quality_gate_exactly_half_substantive_passes() -> None:
    units = [_with(beds=1), _hollow()]
    assert property_passes_quality_gate(units)


def test_property_passes_quality_gate_one_third_substantive_fails() -> None:
    units = [_with(beds=1), _hollow(), _hollow()]
    assert not property_passes_quality_gate(units)


def test_property_passes_quality_gate_all_hollow_fails() -> None:
    units = [_hollow(), _hollow(), _hollow()]
    assert not property_passes_quality_gate(units)


# ── Orchestrator integration ──────────────────────────────────────────────────


def test_orchestrator_flips_next_tier_requested_on_hollow_success() -> None:
    """When units pass row-count but all are hollow, next_tier_requested becomes True."""
    from ma_poc.validation.orchestrator import validate

    class FakeExtract:
        property_id = "280734"
        # Use old-style field names the schema gate understands
        records = [
            {
                "unit_id": f"id{i}",
                "floor_plan_type": None,
                "bedrooms": None,
                "asking_rent": None,
                "sqft": None,
                # v2 fields all hollow
                "beds": None,
                "baths": None,
                "floor_plan_name": None,
                "area": -1,
                "rent_low": None,
                "rent_high": None,
            }
            for i in range(5)
        ]

    result = validate(FakeExtract())
    # When all accepted units are hollow, the orchestrator marks next_tier_requested
    assert result.next_tier_requested is True


def test_verdict_flips_to_failed_no_data_when_all_tiers_hollow() -> None:
    """Hollow-unit SUCCESS with no rescue yields FAILED_NO_DATA verdict."""
    from ma_poc.reporting.verdict import Verdict, compute

    class FakeExtract:
        records = [
            {
                "unit_id": f"id{i}",
                "beds": None,
                "baths": None,
                "floor_plan_name": None,
                "area": -1,
                "rent_low": None,
            }
            for i in range(3)
        ]

    verdict = compute(
        fetch_outcome="OK",
        extract_result=FakeExtract(),
        validated=None,
        carry_forward_applied=False,
        units_hollow=True,
    )
    assert verdict.verdict == Verdict.FAILED_NO_DATA
    assert "hollow" in verdict.reason


def test_regression_northside_place_fixture_now_fails_no_data() -> None:
    """The 14 hollow units from Northside Place (id=280734) must fail quality gate."""
    units = json.loads((FIXTURES / "northside_place_units.json").read_text())
    assert not property_passes_quality_gate(units), (
        "Northside Place fixture should fail quality gate — all 14 units are hollow"
    )


# ─────────────────────────────────────────────────────────────────────
# 2026-05-20 Path C extensions — rent-signal + area-signal predicates.
#
# The default ``property_passes_quality_gate`` admits the
# beds+baths+sqft-but-no-rent shape (the 1,031 JSON-LD inflated-SUCCESS
# bucket). These two predicates are the secondary signals the Path C
# retry hook uses to detect the rent-deficient / area-deficient shapes.
# ─────────────────────────────────────────────────────────────────────




def test_has_rent_accepts_numeric_rent_fields() -> None:
    """Direct numeric rent in any of the canonical rent fields counts."""
    assert _has_rent({"asking_rent": 1500}) is True
    assert _has_rent({"market_rent_low": 1500, "market_rent_high": 1800}) is True
    assert _has_rent({"rent_low": 1500.0}) is True
    assert _has_rent({"rent": 2000}) is True


def test_has_rent_accepts_string_currency_rent() -> None:
    """``$1,500`` and ``1500.00`` string forms parse to positive numerics."""
    assert _has_rent({"asking_rent": "$1,500"}) is True
    assert _has_rent({"market_rent_low": "1500.00"}) is True
    assert _has_rent({"rent": "  $2,499.99  "}) is True


def test_has_rent_rejects_missing_or_zero() -> None:
    """No rent fields at all, or all set to 0/None/empty, is no signal."""
    assert _has_rent({}) is False
    assert _has_rent({"unit_id": "1", "beds": 1, "sqft": 750}) is False
    assert _has_rent({"asking_rent": None}) is False
    assert _has_rent({"asking_rent": 0}) is False
    assert _has_rent({"asking_rent": ""}) is False
    assert _has_rent({"market_rent_low": "call for pricing"}) is False


def test_has_rent_rejects_boolean_false_positive() -> None:
    """``bool`` is a subclass of ``int`` in Python. Make sure ``True`` in
    a rent field isn't accepted as a numeric rent (defensive)."""
    assert _has_rent({"asking_rent": True}) is False
    assert _has_rent({"asking_rent": False}) is False


def test_has_area_accepts_numeric_and_string_area() -> None:
    assert _has_area({"sqft": 750}) is True
    assert _has_area({"area": 1200.5}) is True
    assert _has_area({"sqft": "750"}) is True
    assert _has_area({"square_feet": "1,200"}) is True


def test_has_area_rejects_sentinel_and_missing() -> None:
    assert _has_area({}) is False
    assert _has_area({"sqft": None}) is False
    assert _has_area({"sqft": 0}) is False
    assert _has_area({"sqft": ""}) is False
    # The -1 sentinel sometimes used for "absent" — _is_positive_numeric
    # rejects (>0 only).
    assert _has_area({"sqft": -1}) is False


# property_has_rent_signal ----------------------------------------------------


def test_property_has_rent_signal_empty_returns_false() -> None:
    assert property_has_rent_signal([]) is False


def test_property_has_rent_signal_all_with_rent_passes() -> None:
    units = [
        {"unit_id": "1", "beds": 1, "asking_rent": 1500},
        {"unit_id": "2", "beds": 2, "asking_rent": 2200},
    ]
    assert property_has_rent_signal(units) is True


def test_property_has_rent_signal_all_missing_rent_fails() -> None:
    """The 1,031 JSON-LD inflated-SUCCESS shape: every row has dims but no rent."""
    units = [
        {"unit_id": "inferred_1", "beds": 1, "baths": 1, "sqft": 750},
        {"unit_id": "inferred_2", "beds": 2, "baths": 2, "sqft": 1100},
        {"unit_id": "inferred_3", "beds": 3, "baths": 2, "sqft": 1400},
    ]
    assert property_has_rent_signal(units) is False, (
        "all-rows-no-rent shape must fail rent-signal — this is exactly "
        "the JSON-LD inflated-SUCCESS bucket Path C needs to catch"
    )


def test_property_has_rent_signal_half_with_rent_passes_at_default() -> None:
    """Default 0.5 threshold: 1/2 with rent → exactly meets, passes."""
    units = [
        {"unit_id": "1", "asking_rent": 1500},
        {"unit_id": "2", "asking_rent": None},
    ]
    assert property_has_rent_signal(units) is True


def test_property_has_rent_signal_below_threshold_fails() -> None:
    """1/3 = 0.33 < 0.5 threshold → fails."""
    units = [
        {"unit_id": "1", "asking_rent": 1500},
        {"unit_id": "2", "asking_rent": None},
        {"unit_id": "3", "asking_rent": None},
    ]
    assert property_has_rent_signal(units) is False


# property_has_area_signal ----------------------------------------------------


def test_property_has_area_signal_empty_returns_false() -> None:
    assert property_has_area_signal([]) is False


def test_property_has_area_signal_all_with_area_passes() -> None:
    units = [
        {"unit_id": "1", "sqft": 750, "asking_rent": 1500},
        {"unit_id": "2", "sqft": 1100, "asking_rent": 2200},
    ]
    assert property_has_area_signal(units) is True


def test_property_has_area_signal_all_missing_area_fails() -> None:
    """SightMap responses sometimes ship rent+beds+baths but no area."""
    units = [
        {"unit_id": "1", "beds": 1, "asking_rent": 1500},
        {"unit_id": "2", "beds": 2, "asking_rent": 2200},
    ]
    assert property_has_area_signal(units) is False
