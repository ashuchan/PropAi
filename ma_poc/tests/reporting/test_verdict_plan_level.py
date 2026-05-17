"""Tests for the Stage 2 plan-level verdict path.

Covers:
  * ``SUCCESS_PLAN_LEVEL`` is in the Verdict enum
  * ``verdict_is_success`` recognises both SUCCESS and SUCCESS_PLAN_LEVEL
  * ``compute()`` returns SUCCESS_PLAN_LEVEL when no per-unit records survive
    but ``plan_summaries`` is non-empty
  * Pre-Stage-2 callers (no ``plan_summaries`` arg) keep the old behaviour
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from ma_poc.reporting.verdict import Verdict, compute, verdict_is_success


# ── Enum addition ────────────────────────────────────────────────────────────


def test_success_plan_level_is_in_verdict_enum() -> None:
    assert "SUCCESS_PLAN_LEVEL" in {v.value for v in Verdict}


def test_success_plan_level_distinct_from_success() -> None:
    assert Verdict.SUCCESS != Verdict.SUCCESS_PLAN_LEVEL


# ── verdict_is_success helper ────────────────────────────────────────────────


class TestVerdictIsSuccess:
    @pytest.mark.parametrize(
        "verdict",
        [
            Verdict.SUCCESS,
            Verdict.SUCCESS_PLAN_LEVEL,
            Verdict.SUCCESS_PARTIAL,
            "SUCCESS",
            "SUCCESS_PLAN_LEVEL",
            "SUCCESS_PARTIAL",
        ],
    )
    def test_recognises_all_success_variants(self, verdict):
        """2026-05-17: ``SUCCESS_PARTIAL`` (timeout-rescued with units)
        admitted to the success set. ``PARTIAL`` (validation-majority-
        rejected) stays OUT — its surviving rows are suspect."""
        assert verdict_is_success(verdict) is True

    @pytest.mark.parametrize(
        "verdict",
        [
            Verdict.FAILED_UNREACHABLE,
            Verdict.FAILED_NO_DATA,
            Verdict.CARRY_FORWARD,
            Verdict.PARTIAL,
            "FAILED_NO_DATA",
            "CARRY_FORWARD",
            "PARTIAL",
        ],
    )
    def test_rejects_non_success_verdicts(self, verdict):
        """``PARTIAL`` is validation-majority-rejected — gate dropped more
        than half the rows. The surviving rows are not trustworthy enough
        to count toward the headline success rate."""
        assert verdict_is_success(verdict) is False

    def test_none_is_not_success(self):
        assert verdict_is_success(None) is False

    def test_unknown_string_is_not_success(self):
        assert verdict_is_success("HYPOTHETICAL_FUTURE_STATE") is False


# ── compute() with plan_summaries ────────────────────────────────────────────


@dataclass
class _FakeExtract:
    records: list = field(default_factory=list)


class TestComputeWithPlanSummaries:
    def test_no_units_with_plans_yields_success_plan_level(self):
        """Property with 0 unit-level records but ≥1 plan_summaries →
        SUCCESS_PLAN_LEVEL, not FAILED_NO_DATA."""
        result = compute(
            fetch_outcome="OK",
            extract_result=_FakeExtract(records=[]),
            plan_summaries=[{"beds": 1, "area": 750}],
        )
        assert result.verdict == Verdict.SUCCESS_PLAN_LEVEL
        assert "plan-level data only" in result.reason

    def test_no_units_no_plans_yields_failed_no_data(self):
        """Pre-Stage-2 behaviour preserved: empty records + empty plans →
        FAILED_NO_DATA."""
        result = compute(
            fetch_outcome="OK",
            extract_result=_FakeExtract(records=[]),
            plan_summaries=[],
        )
        assert result.verdict == Verdict.FAILED_NO_DATA

    def test_no_extract_result_with_plans_yields_success_plan_level(self):
        """Symmetric: when extract_result is None but plan_summaries is
        populated, surface plans as SUCCESS_PLAN_LEVEL."""
        result = compute(
            fetch_outcome="OK",
            extract_result=None,
            plan_summaries=[{"beds": 1}],
        )
        assert result.verdict == Verdict.SUCCESS_PLAN_LEVEL

    def test_units_present_keeps_success_verdict(self):
        """A property with both unit-level and plan-level data is SUCCESS
        (the unit-level data is the headline outcome)."""
        result = compute(
            fetch_outcome="OK",
            extract_result=_FakeExtract(records=[{"unit_id": "217", "beds": 2}]),
            plan_summaries=[{"beds": 1}],
        )
        assert result.verdict == Verdict.SUCCESS

    def test_carry_forward_takes_precedence_over_plan_level(self):
        """When carry-forward is applied, it wins regardless of plan data."""
        result = compute(
            carry_forward_applied=True,
            plan_summaries=[{"beds": 1}],
        )
        assert result.verdict == Verdict.CARRY_FORWARD

    def test_failed_unreachable_takes_precedence_over_plan_level(self):
        """Fetch failure overrides any plan-level data attached to the
        result (the data is stale from a prior run)."""
        result = compute(
            fetch_outcome="HARD_FAIL",
            extract_result=_FakeExtract(records=[]),
            plan_summaries=[{"beds": 1}],
        )
        assert result.verdict == Verdict.FAILED_UNREACHABLE

    def test_units_hollow_with_plans_yields_success_plan_level(self):
        """Stage 1+2 composition: when units are admitted by row-count but
        flagged hollow by quality gate, and plan_summaries is non-empty,
        rescue to SUCCESS_PLAN_LEVEL."""
        result = compute(
            fetch_outcome="OK",
            extract_result=_FakeExtract(records=[{"unit_id": "x"}]),
            units_hollow=True,
            plan_summaries=[{"beds": 1, "area": 750}],
        )
        assert result.verdict == Verdict.SUCCESS_PLAN_LEVEL

    def test_units_hollow_no_plans_yields_failed_no_data(self):
        """Pre-Stage-2 behaviour preserved for hollow+no-plans."""
        result = compute(
            fetch_outcome="OK",
            extract_result=_FakeExtract(records=[{"unit_id": "x"}]),
            units_hollow=True,
            plan_summaries=[],
        )
        assert result.verdict == Verdict.FAILED_NO_DATA


# ── Pre-Stage-2 callers (no plan_summaries arg) ──────────────────────────────


class TestComputeBackwardsCompat:
    def test_omitting_plan_summaries_keeps_failed_no_data_for_empty_records(self):
        """Callers that haven't migrated to pass ``plan_summaries`` keep the
        pre-Stage-2 behaviour: empty records → FAILED_NO_DATA."""
        result = compute(fetch_outcome="OK", extract_result=_FakeExtract(records=[]))
        assert result.verdict == Verdict.FAILED_NO_DATA

    def test_omitting_plan_summaries_keeps_success_for_populated_records(self):
        result = compute(
            fetch_outcome="OK",
            extract_result=_FakeExtract(records=[{"unit_id": "x"}]),
        )
        assert result.verdict == Verdict.SUCCESS
