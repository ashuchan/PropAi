"""Tests for ma_poc.fetch.proxy.escalation."""

from __future__ import annotations

from ma_poc.fetch.proxy.base import ProxyTier
from ma_poc.fetch.proxy.escalation import (
    DEESCALATE_SUCCESS_STREAK,
    TIMEOUT_FAIL_STREAK_THRESHOLD,
    decide,
)


def test_success_resets_fail_count_and_increments_success() -> None:
    d = decide(
        current_tier=ProxyTier.DATACENTER,
        fail_count=2,
        success_count=0,
        fetch_status=200,
        fetch_signal=None,
        is_timeout=False,
    )
    assert d.new_tier is ProxyTier.DATACENTER
    assert d.new_fail_count == 0
    assert d.new_success_count == 1


def test_403_escalates_immediately_from_datacenter_to_residential() -> None:
    d = decide(
        current_tier=ProxyTier.DATACENTER,
        fail_count=0,
        success_count=0,
        fetch_status=403,
        fetch_signal=None,
        is_timeout=False,
    )
    assert d.new_tier is ProxyTier.RESIDENTIAL
    assert "403" in d.reason


def test_429_escalates_immediately() -> None:
    d = decide(
        current_tier=ProxyTier.DATACENTER,
        fail_count=0,
        success_count=0,
        fetch_status=429,
        fetch_signal=None,
        is_timeout=False,
    )
    assert d.new_tier is ProxyTier.RESIDENTIAL


def test_407_does_not_escalate() -> None:
    # 407 = proxy auth — our problem, not theirs. No escalation.
    d = decide(
        current_tier=ProxyTier.DATACENTER,
        fail_count=0,
        success_count=0,
        fetch_status=407,
        fetch_signal=None,
        is_timeout=False,
    )
    assert d.new_tier is ProxyTier.DATACENTER


def test_captcha_signal_escalates() -> None:
    d = decide(
        current_tier=ProxyTier.DATACENTER,
        fail_count=0,
        success_count=0,
        fetch_status=200,
        fetch_signal="captcha",
        is_timeout=False,
    )
    # 200 is present but captcha signal overrides — wait, spec says success
    # path requires NOT a timeout and 2xx/3xx. To make the signal win, the
    # caller must pass status=None or a non-success status. Document it:
    # signal AND 2xx together is not a real-world shape — we treat it as
    # success. Test the realistic shape here instead: status is 200 from a
    # bot wall, signal is set, timeout is false — the bot wall returns 200.
    # In practice the fetcher sets fetch_status=None when the body looks like
    # a challenge. This test uses that convention.
    assert d.new_tier is ProxyTier.DATACENTER  # success path wins

    d2 = decide(
        current_tier=ProxyTier.DATACENTER,
        fail_count=0,
        success_count=0,
        fetch_status=None,
        fetch_signal="captcha",
        is_timeout=False,
    )
    assert d2.new_tier is ProxyTier.RESIDENTIAL


def test_cloudflare_challenge_escalates() -> None:
    d = decide(
        current_tier=ProxyTier.DATACENTER,
        fail_count=0,
        success_count=0,
        fetch_status=None,
        fetch_signal="cloudflare_challenge",
        is_timeout=False,
    )
    assert d.new_tier is ProxyTier.RESIDENTIAL


def test_timeout_accumulates_before_escalating() -> None:
    d1 = decide(
        current_tier=ProxyTier.DATACENTER,
        fail_count=0,
        success_count=0,
        fetch_status=None,
        fetch_signal=None,
        is_timeout=True,
    )
    assert d1.new_tier is ProxyTier.DATACENTER
    assert d1.new_fail_count == 1

    d2 = decide(
        current_tier=ProxyTier.DATACENTER,
        fail_count=TIMEOUT_FAIL_STREAK_THRESHOLD - 1,
        success_count=0,
        fetch_status=None,
        fetch_signal=None,
        is_timeout=True,
    )
    assert d2.new_tier is ProxyTier.RESIDENTIAL
    assert d2.new_fail_count == 0


def test_no_escalation_above_top_tier() -> None:
    d = decide(
        current_tier=ProxyTier.UNBLOCKER,
        fail_count=0,
        success_count=0,
        fetch_status=403,
        fetch_signal=None,
        is_timeout=False,
    )
    assert d.new_tier is ProxyTier.UNBLOCKER
    assert d.new_fail_count == 1


def test_deescalation_after_success_streak() -> None:
    d = decide(
        current_tier=ProxyTier.RESIDENTIAL,
        fail_count=0,
        success_count=DEESCALATE_SUCCESS_STREAK - 1,
        fetch_status=200,
        fetch_signal=None,
        is_timeout=False,
    )
    assert d.new_tier is ProxyTier.DATACENTER
    assert d.new_success_count == 0


def test_deescalation_not_from_direct() -> None:
    d = decide(
        current_tier=ProxyTier.DIRECT,
        fail_count=0,
        success_count=DEESCALATE_SUCCESS_STREAK - 1,
        fetch_status=200,
        fetch_signal=None,
        is_timeout=False,
    )
    # At DIRECT there's no tier below — stays at DIRECT and increments count
    assert d.new_tier is ProxyTier.DIRECT
    assert d.new_success_count == DEESCALATE_SUCCESS_STREAK


def test_500_increments_fail_but_no_escalation() -> None:
    d = decide(
        current_tier=ProxyTier.DATACENTER,
        fail_count=1,
        success_count=0,
        fetch_status=500,
        fetch_signal=None,
        is_timeout=False,
    )
    assert d.new_tier is ProxyTier.DATACENTER
    assert d.new_fail_count == 2
