"""Tests for F7 — verdict carry-forward reason fix."""

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


def test_verdict_carry_forward_always_sets_reason_carry_forward_applied() -> None:
    r = compute(carry_forward_applied=True)
    assert r.reason == "carry_forward_applied"
    assert r.verdict == Verdict.CARRY_FORWARD


def test_verdict_carry_forward_overrides_all_checks_passed() -> None:
    """carry_forward_applied=True must win over all downstream signals."""
    r = compute(
        fetch_outcome="OK",
        extract_result=FakeExtractResult(records=[1, 2, 3]),
        validated=FakeValidated(accepted=[1, 2, 3]),
        carry_forward_applied=True,
    )
    assert r.verdict == Verdict.CARRY_FORWARD
    assert r.reason == "carry_forward_applied"


def test_verdict_non_cf_success_keeps_all_checks_passed() -> None:
    r = compute(
        fetch_outcome="OK",
        extract_result=FakeExtractResult(records=[1, 2]),
        validated=FakeValidated(accepted=[1, 2]),
        carry_forward_applied=False,
    )
    assert r.verdict == Verdict.SUCCESS
    assert r.reason == "all checks passed"
