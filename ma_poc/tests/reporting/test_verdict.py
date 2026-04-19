"""Tests for verdict — property-level outcome computation."""
from __future__ import annotations

from dataclasses import dataclass, field

from ma_poc.reporting.verdict import Verdict, compute


@dataclass
class FakeExtractResult:
    records: list = field(default_factory=list)


@dataclass
class FakeValidated:
    accepted: list = field(default_factory=list)
    rejected: list = field(default_factory=list)


def test_verdict_ssl_error_is_failed_unreachable() -> None:
    r = compute(fetch_outcome="HARD_FAIL")
    assert r.verdict == Verdict.FAILED_UNREACHABLE


def test_verdict_empty_extract_is_failed_no_data() -> None:
    r = compute(fetch_outcome="OK", extract_result=FakeExtractResult())
    assert r.verdict == Verdict.FAILED_NO_DATA


def test_verdict_carry_forward_wins_over_fetch_failure() -> None:
    r = compute(fetch_outcome="HARD_FAIL", carry_forward_applied=True)
    assert r.verdict == Verdict.CARRY_FORWARD


def test_verdict_majority_reject_is_partial() -> None:
    v = FakeValidated(accepted=[1], rejected=[1, 2, 3])
    r = compute(
        fetch_outcome="OK",
        extract_result=FakeExtractResult(records=[1, 2, 3, 4]),
        validated=v,
    )
    assert r.verdict == Verdict.PARTIAL


def test_verdict_all_accept_is_success() -> None:
    v = FakeValidated(accepted=[1, 2, 3])
    r = compute(
        fetch_outcome="OK",
        extract_result=FakeExtractResult(records=[1, 2, 3]),
        validated=v,
    )
    assert r.verdict == Verdict.SUCCESS
