"""Tests for retry_policy — pure retry decision logic."""

from __future__ import annotations

from ma_poc.fetch.contracts import FetchOutcome
from ma_poc.fetch.retry_policy import RetryPolicy


def test_retry_ok_no_retry() -> None:
    policy = RetryPolicy()
    decision = policy.decide(FetchOutcome.OK, 1)
    assert decision.should_retry is False


def test_retry_hard_fail_no_retry() -> None:
    policy = RetryPolicy()
    decision = policy.decide(FetchOutcome.HARD_FAIL, 1)
    assert decision.should_retry is False


def test_retry_transient_schedules_backoff() -> None:
    policy = RetryPolicy(max_attempts=3, base_ms=1000)
    decision = policy.decide(FetchOutcome.TRANSIENT, 1)
    assert decision.should_retry is True
    assert decision.wait_ms > 0
    assert decision.rotate_identity is False


def test_retry_rate_limited_honours_retry_after() -> None:
    policy = RetryPolicy()
    decision = policy.decide(FetchOutcome.RATE_LIMITED, 1, retry_after_header="3")
    assert decision.should_retry is True
    assert decision.wait_ms == 3000


def test_retry_bot_blocked_rotates_identity() -> None:
    policy = RetryPolicy()
    decision = policy.decide(FetchOutcome.BOT_BLOCKED, 1)
    assert decision.should_retry is True
    assert decision.rotate_identity is True


def test_retry_proxy_error_rotates_proxy_twice() -> None:
    policy = RetryPolicy()
    d1 = policy.decide(FetchOutcome.PROXY_ERROR, 1)
    assert d1.should_retry is True
    d2 = policy.decide(FetchOutcome.PROXY_ERROR, 2)
    assert d2.should_retry is True
    d3 = policy.decide(FetchOutcome.PROXY_ERROR, 3)
    assert d3.should_retry is False


def test_retry_exhausts_after_max_attempts() -> None:
    policy = RetryPolicy(max_attempts=2)
    decision = policy.decide(FetchOutcome.TRANSIENT, 2)
    assert decision.should_retry is False
