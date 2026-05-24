"""Verdict tests — SUCCESS_NO_AVAILABILITY path (2026-05-23).

The 2026-05-22 grind flagged ~10 krcapartments.com property pages as
FAILED_NO_DATA. The operator's page actually says "Sorry, there are no
available units at this time." — that's a SUCCESS scrape of a
documented zero-availability state, NOT a data-extraction failure.

This module pins the verdict routing:
  • operator_no_availability=True + 0 records → SUCCESS_NO_AVAILABILITY
  • operator_no_availability=True + units_hollow → SUCCESS_NO_AVAILABILITY
  • operator_no_availability=False + 0 records → FAILED_NO_DATA (unchanged)
  • SUCCESS_NO_AVAILABILITY counts as success in the headline metric
"""
from __future__ import annotations

from ma_poc.reporting.verdict import (
    Verdict,
    compute,
    verdict_excluded_from_success_rate,
    verdict_is_success,
)


def _extract_result_with_records(records: list) -> dict:
    return {"records": records}


# ─── compute: SUCCESS_NO_AVAILABILITY emission ───────────────────────


def test_compute_zero_records_with_no_availability_signal_succeeds() -> None:
    """The krcapartments base case: extract produced zero records but
    the page carried an explicit zero-availability statement."""
    result = compute(
        fetch_outcome="OK",
        extract_result=_extract_result_with_records([]),
        operator_no_availability=True,
    )
    assert result.verdict == Verdict.SUCCESS_NO_AVAILABILITY
    assert "zero availability" in result.reason


def test_compute_no_extract_result_with_no_availability_signal_succeeds() -> None:
    """When the adapter never produces an ``extract_result`` object
    (early-exit detection path) we still honor the no-availability signal."""
    result = compute(
        fetch_outcome="OK",
        extract_result=None,
        operator_no_availability=True,
    )
    assert result.verdict == Verdict.SUCCESS_NO_AVAILABILITY


def test_compute_units_hollow_with_no_availability_signal_succeeds() -> None:
    """If a placeholder unit gets emitted but the schema-gate flags
    it as hollow, the no-availability signal still wins."""
    result = compute(
        fetch_outcome="OK",
        extract_result=_extract_result_with_records([{"placeholder": True}]),
        units_hollow=True,
        operator_no_availability=True,
    )
    assert result.verdict == Verdict.SUCCESS_NO_AVAILABILITY


# ─── regression guard: no-signal default unchanged ───────────────────


def test_compute_zero_records_without_no_availability_still_fails() -> None:
    """Behavioural regression guard: when the signal is absent, the
    existing FAILED_NO_DATA path must still fire."""
    result = compute(
        fetch_outcome="OK",
        extract_result=_extract_result_with_records([]),
        operator_no_availability=False,
    )
    assert result.verdict == Verdict.FAILED_NO_DATA


def test_compute_zero_records_no_signal_no_kwargs_still_fails() -> None:
    """The kwarg defaults must preserve historical behaviour — old
    callers that don't pass operator_no_availability get FAILED_NO_DATA."""
    result = compute(
        fetch_outcome="OK",
        extract_result=_extract_result_with_records([]),
    )
    assert result.verdict == Verdict.FAILED_NO_DATA


# ─── precedence ──────────────────────────────────────────────────────


def test_no_availability_loses_to_fetch_failure() -> None:
    """A fetch failure still wins — we never even got to read the page."""
    result = compute(
        fetch_outcome="HTTP_500",
        operator_no_availability=True,
    )
    assert result.verdict == Verdict.FAILED_UNREACHABLE


def test_no_availability_loses_to_carry_forward() -> None:
    """Carry-forward is checked first per the existing rule order."""
    result = compute(
        carry_forward_applied=True,
        operator_no_availability=True,
    )
    assert result.verdict == Verdict.CARRY_FORWARD


def test_no_availability_loses_to_actual_records() -> None:
    """If extract DID produce records, those win over the
    no-availability flag — maybe the operator left stale 'no units'
    copy at the page top while publishing a real unit table below."""
    result = compute(
        fetch_outcome="OK",
        extract_result=_extract_result_with_records(
            [{"unit_number": "101", "market_rent_low": 1500, "sqft": "750"}]
        ),
        operator_no_availability=True,
        units=[{"unit_number": "101", "market_rent_low": 1500, "sqft": "750"}],
    )
    assert result.verdict == Verdict.SUCCESS


def test_no_availability_dead_url_still_wins() -> None:
    """A dead URL is always terminal — no-availability flag doesn't
    matter when the page itself returned 404 / 410."""
    result = compute(
        fetch_outcome="DEAD_URL",
        operator_no_availability=True,
    )
    assert result.verdict == Verdict.DEAD_URL


# ─── headline-metric integration ─────────────────────────────────────


def test_no_availability_counts_as_success_in_headline_metric() -> None:
    """``verdict_is_success`` must return True so dashboards count
    the krcapartments cohort toward the success-rate numerator."""
    assert verdict_is_success(Verdict.SUCCESS_NO_AVAILABILITY) is True
    assert verdict_is_success("SUCCESS_NO_AVAILABILITY") is True


def test_no_availability_not_excluded_from_success_rate() -> None:
    """Unlike DEAD_URL (which is excluded from the denominator),
    SUCCESS_NO_AVAILABILITY belongs in the denominator AND the
    numerator — the scrape happened."""
    assert (
        verdict_excluded_from_success_rate(Verdict.SUCCESS_NO_AVAILABILITY)
        is False
    )
