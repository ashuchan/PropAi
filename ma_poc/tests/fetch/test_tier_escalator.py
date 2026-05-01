"""Tests for fetch/tier_escalator.py — fetch_with_escalation()."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode
from ma_poc.fetch.tier_escalator import (
    MAX_ESCALATIONS_PER_RUN,
    _build_ladder,
    _merge_attempts,
    fetch_with_escalation,
)
from ma_poc.models.fetch_tier import FetchTier


def _make_result(
    outcome: FetchOutcome = FetchOutcome.OK,
    tier: int = 0,
    block_sig: str | None = None,
) -> FetchResult:
    return FetchResult(
        url="https://example.com",
        outcome=outcome,
        status=200 if outcome == FetchOutcome.OK else 403,
        body=b"body",
        headers={},
        render_mode=RenderMode.GET,
        final_url="https://example.com",
        attempts=1,
        elapsed_ms=100,
        fetch_tier_used=tier,
        fetch_tier_attempts=[tier],
        block_signature=block_sig,
    )


def _make_task() -> MagicMock:
    task = MagicMock()
    task.url = "https://example.com"
    task.property_id = "prop-123"
    task.render_mode = RenderMode.GET
    task.budget_ms = 10000
    return task


def _make_profile(floor: FetchTier = FetchTier.DIRECT) -> MagicMock:
    profile = MagicMock()
    profile.fetch = MagicMock()
    profile.fetch.tier_floor = floor
    return profile


# ── _build_ladder ─────────────────────────────────────────────────────────────

def test_build_ladder_default_flags_direct_only() -> None:
    """With all sub-flags off, ladder only contains DIRECT."""
    with (
        patch("ma_poc.fetch.tier_escalator.ENABLE_DC_PROXY_TIER", False),
        patch("ma_poc.fetch.tier_escalator.ENABLE_RESIDENTIAL_TIER", False),
        patch("ma_poc.fetch.tier_escalator.ENABLE_UNLOCKER_TIER", False),
    ):
        ladder = _build_ladder(FetchTier.DIRECT)
    assert ladder == [FetchTier.DIRECT]


def test_build_ladder_dc_proxy_enabled() -> None:
    with (
        patch("ma_poc.fetch.tier_escalator.ENABLE_DC_PROXY_TIER", True),
        patch("ma_poc.fetch.tier_escalator.ENABLE_RESIDENTIAL_TIER", False),
        patch("ma_poc.fetch.tier_escalator.ENABLE_UNLOCKER_TIER", False),
    ):
        ladder = _build_ladder(FetchTier.DIRECT)
    assert ladder == [FetchTier.DIRECT, FetchTier.DC_PROXY]


def test_build_ladder_floor_skips_lower_tiers() -> None:
    """When floor is DC_PROXY, DIRECT is omitted from the ladder."""
    with (
        patch("ma_poc.fetch.tier_escalator.ENABLE_DC_PROXY_TIER", True),
        patch("ma_poc.fetch.tier_escalator.ENABLE_RESIDENTIAL_TIER", False),
        patch("ma_poc.fetch.tier_escalator.ENABLE_UNLOCKER_TIER", False),
    ):
        ladder = _build_ladder(FetchTier.DC_PROXY)
    assert FetchTier.DIRECT not in ladder
    assert FetchTier.DC_PROXY in ladder


# ── fetch_with_escalation — escalation disabled ───────────────────────────────

@pytest.mark.asyncio
async def test_escalation_disabled_calls_direct() -> None:
    task = _make_task()
    profile = _make_profile()
    expected = _make_result(FetchOutcome.OK, tier=0)

    with (
        patch("ma_poc.fetch.tier_escalator.ENABLE_TIER_ESCALATION", False),
        patch("ma_poc.fetch.providers.direct.DirectProvider.fetch", new_callable=AsyncMock, return_value=expected),
    ):
        result = await fetch_with_escalation(task, profile)

    assert result.outcome == FetchOutcome.OK
    assert result.fetch_tier_used == 0


# ── fetch_with_escalation — happy path ───────────────────────────────────────

@pytest.mark.asyncio
async def test_direct_ok_no_escalation() -> None:
    """OK at DIRECT tier — no escalation should happen."""
    task = _make_task()
    profile = _make_profile(FetchTier.DIRECT)
    ok_result = _make_result(FetchOutcome.OK, tier=0)

    direct_mock = AsyncMock(return_value=ok_result)

    with (
        patch("ma_poc.fetch.tier_escalator.ENABLE_TIER_ESCALATION", True),
        patch("ma_poc.fetch.tier_escalator.ENABLE_DC_PROXY_TIER", True),
        patch("ma_poc.fetch.tier_escalator.ENABLE_RESIDENTIAL_TIER", False),
        patch("ma_poc.fetch.tier_escalator.ENABLE_UNLOCKER_TIER", False),
        patch("ma_poc.fetch.providers.direct.DirectProvider.fetch", direct_mock),
    ):
        result = await fetch_with_escalation(task, profile)

    assert result.outcome == FetchOutcome.OK
    assert result.fetch_tier_used == int(FetchTier.DIRECT)
    direct_mock.assert_awaited_once()


# ── fetch_with_escalation — escalation on BOT_BLOCKED ────────────────────────

@pytest.mark.asyncio
async def test_bot_blocked_escalates_to_dc_proxy() -> None:
    """BOT_BLOCKED at DIRECT should escalate to DC_PROXY."""
    task = _make_task()
    profile = _make_profile(FetchTier.DIRECT)

    blocked = _make_result(FetchOutcome.BOT_BLOCKED, tier=0, block_sig="cf_turnstile")
    dc_ok = _make_result(FetchOutcome.OK, tier=2)

    direct_mock = AsyncMock(return_value=blocked)
    dc_mock = AsyncMock(return_value=dc_ok)

    with (
        patch("ma_poc.fetch.tier_escalator.ENABLE_TIER_ESCALATION", True),
        patch("ma_poc.fetch.tier_escalator.ENABLE_DC_PROXY_TIER", True),
        patch("ma_poc.fetch.tier_escalator.ENABLE_RESIDENTIAL_TIER", False),
        patch("ma_poc.fetch.tier_escalator.ENABLE_UNLOCKER_TIER", False),
        patch("ma_poc.fetch.providers.direct.DirectProvider.fetch", direct_mock),
        patch("ma_poc.fetch.providers.dc_proxy.DcProxyProvider.fetch", dc_mock),
        patch("ma_poc.fetch.providers.dc_proxy.BrightDataProvider.__init__", return_value=None),
    ):
        result = await fetch_with_escalation(task, profile)

    assert result.outcome == FetchOutcome.OK
    assert result.fetch_tier_used == int(FetchTier.DC_PROXY)
    assert int(FetchTier.DIRECT) in result.fetch_tier_attempts
    assert int(FetchTier.DC_PROXY) in result.fetch_tier_attempts


# ── fetch_with_escalation — ladder exhaustion ────────────────────────────────

@pytest.mark.asyncio
async def test_bot_blocked_at_all_tiers_returns_last_result() -> None:
    """BOT_BLOCKED at every tier — returns the last result after ladder exhaust."""
    task = _make_task()
    profile = _make_profile(FetchTier.DIRECT)

    blocked_direct = _make_result(FetchOutcome.BOT_BLOCKED, tier=0, block_sig="cf_turnstile")
    blocked_dc = _make_result(FetchOutcome.BOT_BLOCKED, tier=2, block_sig="cf_turnstile")

    with (
        patch("ma_poc.fetch.tier_escalator.ENABLE_TIER_ESCALATION", True),
        patch("ma_poc.fetch.tier_escalator.ENABLE_DC_PROXY_TIER", True),
        patch("ma_poc.fetch.tier_escalator.ENABLE_RESIDENTIAL_TIER", False),
        patch("ma_poc.fetch.tier_escalator.ENABLE_UNLOCKER_TIER", False),
        patch("ma_poc.fetch.providers.direct.DirectProvider.fetch", AsyncMock(return_value=blocked_direct)),
        patch("ma_poc.fetch.providers.dc_proxy.DcProxyProvider.fetch", AsyncMock(return_value=blocked_dc)),
        patch("ma_poc.fetch.providers.dc_proxy.BrightDataProvider.__init__", return_value=None),
    ):
        result = await fetch_with_escalation(task, profile)

    assert result.outcome == FetchOutcome.BOT_BLOCKED


# ── fetch_with_escalation — HARD_FAIL stops escalation ───────────────────────

@pytest.mark.asyncio
async def test_hard_fail_stops_escalation() -> None:
    """HARD_FAIL at DIRECT tier should not escalate — DNS/SSL errors won't help."""
    task = _make_task()
    profile = _make_profile(FetchTier.DIRECT)

    hard_fail = _make_result(FetchOutcome.HARD_FAIL, tier=0)
    dc_mock = AsyncMock(return_value=_make_result(FetchOutcome.OK, tier=2))

    with (
        patch("ma_poc.fetch.tier_escalator.ENABLE_TIER_ESCALATION", True),
        patch("ma_poc.fetch.tier_escalator.ENABLE_DC_PROXY_TIER", True),
        patch("ma_poc.fetch.tier_escalator.ENABLE_RESIDENTIAL_TIER", False),
        patch("ma_poc.fetch.tier_escalator.ENABLE_UNLOCKER_TIER", False),
        patch("ma_poc.fetch.providers.direct.DirectProvider.fetch", AsyncMock(return_value=hard_fail)),
        patch("ma_poc.fetch.providers.dc_proxy.DcProxyProvider.fetch", dc_mock),
        patch("ma_poc.fetch.providers.dc_proxy.BrightDataProvider.__init__", return_value=None),
    ):
        result = await fetch_with_escalation(task, profile)

    assert result.outcome == FetchOutcome.HARD_FAIL
    dc_mock.assert_not_awaited()


# ── fetch_with_escalation — floor respected ───────────────────────────────────

@pytest.mark.asyncio
async def test_floor_dc_proxy_skips_direct() -> None:
    """If profile floor is DC_PROXY, DIRECT should not be called."""
    task = _make_task()
    profile = _make_profile(FetchTier.DC_PROXY)

    dc_ok = _make_result(FetchOutcome.OK, tier=2)
    direct_mock = AsyncMock(return_value=_make_result(FetchOutcome.OK, tier=0))
    dc_mock = AsyncMock(return_value=dc_ok)

    with (
        patch("ma_poc.fetch.tier_escalator.ENABLE_TIER_ESCALATION", True),
        patch("ma_poc.fetch.tier_escalator.ENABLE_DC_PROXY_TIER", True),
        patch("ma_poc.fetch.tier_escalator.ENABLE_RESIDENTIAL_TIER", False),
        patch("ma_poc.fetch.tier_escalator.ENABLE_UNLOCKER_TIER", False),
        patch("ma_poc.fetch.providers.direct.DirectProvider.fetch", direct_mock),
        patch("ma_poc.fetch.providers.dc_proxy.DcProxyProvider.fetch", dc_mock),
        patch("ma_poc.fetch.providers.dc_proxy.BrightDataProvider.__init__", return_value=None),
    ):
        result = await fetch_with_escalation(task, profile)

    direct_mock.assert_not_awaited()
    dc_mock.assert_awaited_once()
    assert result.outcome == FetchOutcome.OK


# ── _merge_attempts ───────────────────────────────────────────────────────────

def test_merge_attempts_replaces_list() -> None:
    result = _make_result(FetchOutcome.OK, tier=2)
    merged = _merge_attempts(result, [0, 2])
    assert merged.fetch_tier_attempts == [0, 2]
    assert merged.fetch_tier_used == 2


def test_merge_attempts_preserves_all_fields() -> None:
    result = _make_result(FetchOutcome.OK, tier=2)
    merged = _merge_attempts(result, [0, 2])
    assert merged.url == result.url
    assert merged.outcome == result.outcome
    assert merged.block_signature == result.block_signature


# ── MAX_ESCALATIONS_PER_RUN cap ───────────────────────────────────────────────

def test_max_escalations_constant() -> None:
    assert MAX_ESCALATIONS_PER_RUN == 3
