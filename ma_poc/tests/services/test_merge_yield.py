"""Tests for the post-merge yield gate.

The gate is a pure decision function: given a :class:`UnitDiff`, decide
whether more than half the captured units failed to key. The behaviour is
small enough that the test surface mirrors the function 1:1 (threshold,
strict-greater-than, empty-input, default-fields-on-old-callers).
"""

from __future__ import annotations

import pytest

from ma_poc.data_provider.dtos import UnitDiff
from ma_poc.services.merge_yield import evaluate


def _diff(*, input_count: int, skipped: int) -> UnitDiff:
    return UnitDiff(input_count=input_count, skipped_no_identity=skipped)


def test_empty_input_does_not_trigger() -> None:
    v = evaluate(_diff(input_count=0, skipped=0))
    assert v.next_tier_requested is False
    assert v.keyless_ratio == 0.0


def test_zero_skipped_does_not_trigger() -> None:
    v = evaluate(_diff(input_count=10, skipped=0))
    assert v.next_tier_requested is False
    assert v.keyless_ratio == 0.0


def test_under_half_does_not_trigger() -> None:
    v = evaluate(_diff(input_count=10, skipped=4))
    assert v.next_tier_requested is False
    assert v.keyless_ratio == pytest.approx(0.4)


def test_exactly_half_does_not_trigger() -> None:
    """Strictly-greater-than threshold — 50/50 is borderline OK."""
    v = evaluate(_diff(input_count=10, skipped=5))
    assert v.next_tier_requested is False
    assert v.keyless_ratio == 0.5


def test_just_over_half_triggers() -> None:
    v = evaluate(_diff(input_count=10, skipped=6))
    assert v.next_tier_requested is True
    assert v.keyless_ratio == pytest.approx(0.6)


def test_all_skipped_triggers() -> None:
    v = evaluate(_diff(input_count=4, skipped=4))
    assert v.next_tier_requested is True
    assert v.keyless_ratio == 1.0


def test_custom_threshold() -> None:
    """0.25 threshold flips on a 30% skip rate that the default 0.5 wouldn't."""
    diff = _diff(input_count=10, skipped=3)
    assert evaluate(diff).next_tier_requested is False
    assert evaluate(diff, threshold=0.25).next_tier_requested is True


def test_old_caller_with_default_zero_fields_does_not_trigger() -> None:
    """An older caller that constructs ``UnitDiff()`` without populating
    the new counters must produce a no-op verdict. This protects us from
    a noisy flood of false-positive ``UNITS_KEYLESS_HIGH`` issues during
    rollout while consumers are still catching up to the new contract."""
    old_shape = UnitDiff()
    v = evaluate(old_shape)
    assert v.next_tier_requested is False
    assert v.keyless_ratio == 0.0


def test_threshold_is_echoed_in_verdict() -> None:
    """Logging convenience — the verdict carries the threshold it was
    compared against."""
    v = evaluate(_diff(input_count=10, skipped=6), threshold=0.4)
    assert v.threshold == 0.4


def test_negative_counters_are_clamped_to_zero() -> None:
    """Defence-in-depth — the helper must never produce a negative ratio
    if a buggy caller hands in a negative ``skipped_no_identity``."""
    bogus = UnitDiff(input_count=10, skipped_no_identity=-3)
    v = evaluate(bogus)
    assert v.keyless_ratio == 0.0
    assert v.next_tier_requested is False
