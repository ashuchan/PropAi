"""Tests for evidence-backed unit completeness reconciliation."""

from __future__ import annotations

import pytest

from ma_poc.validation.unit_completeness import (
    UnitCompletenessStatus,
    normalise_unit_key,
    reconcile_unit_completeness,
)


def _unit(unit_id: str, rent: int = 1200, **extra: object) -> dict[str, object]:
    """Build a strict unit row for reconciliation tests."""
    return {"unit_id": unit_id, "rent_low": rent, **extra}


def test_normalise_key_preserves_leading_zeroes_and_ignores_formatting() -> None:
    """Displayed unit formatting must not create a false mismatch."""
    assert normalise_unit_key("# 08-B") == "08B"
    assert normalise_unit_key("011T") == "011T"
    assert normalise_unit_key("inferred_abc") is None


def test_reconcile_complete_requires_count_and_exact_observed_coverage() -> None:
    """A real count plus matching strict unit rows proves completeness."""
    audit = reconcile_unit_completeness(
        captured_units=[_unit("08-B"), _unit("20-D")],
        observed_units=[_unit("#08B"), _unit("20 D")],
        routes_checked=["https://example.test/availability"],
        expected_available_count=2,
    )
    assert audit.status == UnitCompletenessStatus.COMPLETE
    assert audit.missing_observed_unit_keys == []


def test_reconcile_marks_observed_unit_missing_from_output_incomplete() -> None:
    """A clean plan row cannot hide an omitted displayed unit."""
    audit = reconcile_unit_completeness(
        captured_units=[_unit("08-B"), _unit("inferred_plan")],
        observed_units=[_unit("08-B"), _unit("20-D")],
        routes_checked=["https://example.test/availability"],
        expected_available_count=2,
    )
    assert audit.status == UnitCompletenessStatus.INCOMPLETE
    assert audit.missing_observed_unit_keys == ["20D"]


def test_reconcile_excludes_durably_marked_plan_rows() -> None:
    """A plan summary with rent and an ID-like token is never a unit match."""
    audit = reconcile_unit_completeness(
        captured_units=[
            _unit("A1", data_quality_flag="PLAN_LEVEL_NO_UNIT_ANCHOR"),
            _unit("08-B"),
        ],
        observed_units=[_unit("08-B"), _unit("20-D")],
        routes_checked=["https://example.test/availability"],
        expected_available_count=2,
    )
    assert audit.status == UnitCompletenessStatus.INCOMPLETE
    assert audit.missing_observed_unit_keys == ["20D"]


def test_reconcile_rejects_stale_extra_output_unit() -> None:
    """Equal counts alone cannot accept a stale unit from another route state."""
    audit = reconcile_unit_completeness(
        captured_units=[_unit("08-B"), _unit("20-D")],
        observed_units=[_unit("08-B")],
        routes_checked=["https://example.test/availability"],
        expected_available_count=1,
    )
    assert audit.status == UnitCompletenessStatus.INCOMPLETE
    assert audit.captured_not_observed_unit_keys == ["20D"]


def test_reconcile_never_claims_complete_without_operator_count() -> None:
    """Matching partial observations alone cannot prove exhaustive coverage."""
    audit = reconcile_unit_completeness(
        captured_units=[_unit("08-B")],
        observed_units=[_unit("08-B")],
        routes_checked=["https://example.test/availability"],
    )
    assert audit.status == UnitCompletenessStatus.UNKNOWN


def test_reconcile_zero_requires_explicit_zero_and_no_strict_rows() -> None:
    """An explicit public zero is valid but distinct from a unit success."""
    audit = reconcile_unit_completeness(
        captured_units=[],
        observed_units=[],
        routes_checked=["https://example.test/availability"],
        expected_available_count=0,
    )
    assert audit.status == UnitCompletenessStatus.COMPLETE_PUBLIC_ZERO


def test_reconcile_detects_count_mismatch() -> None:
    """A truncated page cannot be called complete even when output matches it."""
    audit = reconcile_unit_completeness(
        captured_units=[_unit("08-B")],
        observed_units=[_unit("08-B")],
        routes_checked=["https://example.test/availability"],
        expected_available_count=2,
    )
    assert audit.status == UnitCompletenessStatus.INCOMPLETE


def test_reconcile_keeps_access_blocked_separate_from_public_zero() -> None:
    """An inaccessible route is neither a no-data result nor a clean pass."""
    audit = reconcile_unit_completeness(
        captured_units=[],
        observed_units=[],
        routes_checked=["https://example.test/availability"],
        access_blocked=True,
    )
    assert audit.status == UnitCompletenessStatus.ACCESS_BLOCKED


def test_reconcile_rejects_negative_operator_count() -> None:
    """Malformed operator counts must fail visibly instead of being accepted."""
    with pytest.raises(ValueError, match="non-negative"):
        reconcile_unit_completeness(
            captured_units=[],
            observed_units=[],
            routes_checked=[],
            expected_available_count=-1,
        )
