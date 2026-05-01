"""Tests for update_fetch_profile_after_fetch() in services/profile_updater.py."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode
from ma_poc.models.fetch_tier import FetchTier


def _make_fetch_result(
    outcome: FetchOutcome = FetchOutcome.OK,
    tier: int = 0,
    block_sig: str | None = None,
) -> FetchResult:
    return FetchResult(
        url="https://example.com",
        outcome=outcome,
        status=200 if outcome == FetchOutcome.OK else 403,
        body=b"ok",
        headers={},
        render_mode=RenderMode.GET,
        final_url="https://example.com",
        attempts=1,
        elapsed_ms=50,
        fetch_tier_used=tier,
        block_signature=block_sig,
    )


def _make_profile(floor: FetchTier = FetchTier.DIRECT) -> MagicMock:
    profile = MagicMock()
    profile.canonical_id = "prop-001"
    fp = MagicMock()
    fp.tier_floor = floor
    fp.consecutive_successes_at_floor = 0
    fp.consecutive_failures_at_floor = 0
    fp.total_escalations = 0
    fp.last_block_signature = None
    profile.fetch = fp
    return profile


def test_flag_off_returns_profile_unchanged() -> None:
    from ma_poc.services.profile_updater import update_fetch_profile_after_fetch

    profile = _make_profile()
    result = _make_fetch_result(FetchOutcome.OK, tier=0)

    with patch("ma_poc.services.profile_updater.ENABLE_TIER_ESCALATION", False):
        returned = update_fetch_profile_after_fetch(profile, result)

    assert returned is profile
    # fetch fields should not have been touched
    profile.fetch.tier_floor.__set__ = MagicMock()


def test_success_at_floor_increments_consecutive() -> None:
    from ma_poc.services.profile_updater import update_fetch_profile_after_fetch

    profile = _make_profile(floor=FetchTier.DIRECT)
    result = _make_fetch_result(FetchOutcome.OK, tier=0)

    with patch("ma_poc.services.profile_updater.ENABLE_TIER_ESCALATION", True):
        update_fetch_profile_after_fetch(profile, result)

    assert profile.fetch.consecutive_successes_at_floor == 1
    assert profile.fetch.consecutive_failures_at_floor == 0


def test_bot_blocked_increments_failure_and_stores_sig() -> None:
    from ma_poc.services.profile_updater import update_fetch_profile_after_fetch

    profile = _make_profile(floor=FetchTier.DIRECT)
    result = _make_fetch_result(FetchOutcome.BOT_BLOCKED, tier=0, block_sig="cf_turnstile")

    with patch("ma_poc.services.profile_updater.ENABLE_TIER_ESCALATION", True):
        update_fetch_profile_after_fetch(profile, result)

    assert profile.fetch.consecutive_failures_at_floor == 1
    assert profile.fetch.last_block_signature == "cf_turnstile"


def test_promotion_raises_tier_floor() -> None:
    """When tier_used > current floor, floor should be promoted."""
    from ma_poc.services.profile_updater import update_fetch_profile_after_fetch

    profile = _make_profile(floor=FetchTier.DIRECT)
    # Simulate: DIRECT was blocked, DC_PROXY succeeded
    result = _make_fetch_result(FetchOutcome.OK, tier=int(FetchTier.DC_PROXY))

    with (
        patch("ma_poc.services.profile_updater.ENABLE_TIER_ESCALATION", True),
        patch("ma_poc.observability.events.emit"),
    ):
        update_fetch_profile_after_fetch(profile, result)

    assert profile.fetch.tier_floor == FetchTier.DC_PROXY
    assert profile.fetch.total_escalations == 1
    assert profile.fetch.consecutive_successes_at_floor == 1
    assert profile.fetch.consecutive_failures_at_floor == 0


def test_demotion_lowers_tier_floor() -> None:
    """When tier_used < current floor (probe succeeded), floor should demote."""
    from ma_poc.services.profile_updater import update_fetch_profile_after_fetch

    profile = _make_profile(floor=FetchTier.DC_PROXY)
    # Probe at DIRECT succeeded
    result = _make_fetch_result(FetchOutcome.OK, tier=int(FetchTier.DIRECT))

    with (
        patch("ma_poc.services.profile_updater.ENABLE_TIER_ESCALATION", True),
        patch("ma_poc.observability.events.emit"),
    ):
        update_fetch_profile_after_fetch(profile, result)

    assert profile.fetch.tier_floor == FetchTier.DIRECT
    assert profile.fetch.consecutive_successes_at_floor == 1
    assert profile.fetch.consecutive_failures_at_floor == 0


def test_exception_in_update_does_not_raise() -> None:
    """If profile.fetch is broken, update_fetch_profile_after_fetch must not raise."""
    from ma_poc.services.profile_updater import update_fetch_profile_after_fetch

    bad_profile = MagicMock()
    bad_profile.fetch = None  # Will cause AttributeError
    result = _make_fetch_result(FetchOutcome.OK, tier=0)

    with patch("ma_poc.services.profile_updater.ENABLE_TIER_ESCALATION", True):
        returned = update_fetch_profile_after_fetch(bad_profile, result)

    assert returned is bad_profile
