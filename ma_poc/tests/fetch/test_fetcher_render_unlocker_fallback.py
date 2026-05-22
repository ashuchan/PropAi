"""RENDER-task → Web Unlocker fallback in Fetcher._do_request.

2026-05-22 — commit 0e85fbf gates the tier escalator off for
RenderMode.RENDER tasks (the escalator's plain-GET lower tiers serve
un-rendered HTML for SPA pages — broke 50/50 controls). But a Cloudflare
bot-fight 403 on a RENDER task then has NO escape: patchright is blocked
and the escalator is bypassed. Entrata /conventional/ pages (~286
properties in the 5K run) died here.

The fix: when a RENDER task comes back BOT_BLOCKED and the Unlocker tier
is enabled, fall back to JUST the Web Unlocker (solved real HTML — fine
for server-rendered pages). These tests pin that behaviour.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ma_poc.discovery.contracts import CrawlTask, TaskReason
from ma_poc.fetch.browser_pool import BrowserContextPool
from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode
from ma_poc.fetch.fetcher import Fetcher
from ma_poc.fetch.proxy_pool import ProxyPool
from ma_poc.fetch.stealth import Identity, IdentityPool

_IDENTITY = Identity(
    user_agent="Mozilla/5.0 (Test)",
    accept_language="en-US,en;q=0.9",
    viewport=(1280, 720),
    platform="Windows",
)


def _make_fetcher() -> Fetcher:
    return Fetcher(
        proxy_pool=ProxyPool([]),
        rate_limiter=MagicMock(),
        robots=MagicMock(),
        cond_cache=MagicMock(),
        identities=IdentityPool(),
        browsers=BrowserContextPool(max_contexts=1),
        retry=MagicMock(),
    )


def _render_task() -> CrawlTask:
    return CrawlTask(
        url="https://www.example.com/x/conventional/",
        property_id="ENT-001",
        priority=0,
        budget_ms=10_000,
        reason=TaskReason.SCHEDULED,
        render_mode=RenderMode.RENDER,
    )


def _result(outcome: FetchOutcome, body: bytes, status: int | None) -> FetchResult:
    return FetchResult(
        url="https://www.example.com/x/conventional/",
        outcome=outcome,
        status=status,
        body=body,
        headers={},
        render_mode=RenderMode.RENDER,
        final_url="https://www.example.com/x/conventional/",
        attempts=1,
        elapsed_ms=10,
    )


def _BLOCKED() -> FetchResult:
    return _result(FetchOutcome.BOT_BLOCKED, b"<html>Just a moment</html>", 403)


def _UNLOCKED_OK() -> FetchResult:
    return _result(
        FetchOutcome.OK, b"<html><div class='fp-card'>$1,500</div></html>", 200
    )


def _mock_unlocker(fetch_result: FetchResult) -> MagicMock:
    """Patch target for providers.unlocker.UnlockerProvider."""
    inst = MagicMock()
    inst.fetch = AsyncMock(return_value=fetch_result)
    cls = MagicMock(return_value=inst)
    return cls


@pytest.mark.asyncio
async def test_render_bot_blocked_falls_back_to_unlocker() -> None:
    """RENDER + BOT_BLOCKED + flags on → Unlocker result is returned."""
    fetcher = _make_fetcher()
    fetcher._do_render = AsyncMock(return_value=_BLOCKED())
    with (
        patch("ma_poc.fetch.fetcher.ENABLE_TIER_ESCALATION", True),
        patch("ma_poc.fetch.fetcher.ENABLE_UNLOCKER_TIER", True),
        patch(
            "ma_poc.fetch.providers.unlocker.UnlockerProvider",
            _mock_unlocker(_UNLOCKED_OK()),
        ),
    ):
        result = await fetcher._do_request(
            _render_task(), _IDENTITY, None, None, None, 1, 0
        )
    assert result.outcome == FetchOutcome.OK
    assert result.body and b"fp-card" in result.body


@pytest.mark.asyncio
async def test_render_bot_blocked_no_fallback_when_flag_off() -> None:
    """With the Unlocker tier disabled, the BOT_BLOCKED render result
    stands — no fallback."""
    fetcher = _make_fetcher()
    fetcher._do_render = AsyncMock(return_value=_BLOCKED())
    with (
        patch("ma_poc.fetch.fetcher.ENABLE_TIER_ESCALATION", True),
        patch("ma_poc.fetch.fetcher.ENABLE_UNLOCKER_TIER", False),
        patch(
            "ma_poc.fetch.providers.unlocker.UnlockerProvider",
            _mock_unlocker(_UNLOCKED_OK()),
        ) as cls,
    ):
        result = await fetcher._do_request(
            _render_task(), _IDENTITY, None, None, None, 1, 0
        )
    assert result.outcome == FetchOutcome.BOT_BLOCKED
    cls.assert_not_called()


@pytest.mark.asyncio
async def test_render_ok_does_not_invoke_unlocker() -> None:
    """A successful RENDER never touches the Unlocker (no needless cost)."""
    fetcher = _make_fetcher()
    fetcher._do_render = AsyncMock(
        return_value=_result(FetchOutcome.OK, b"<html>ok</html>", 200)
    )
    with (
        patch("ma_poc.fetch.fetcher.ENABLE_TIER_ESCALATION", True),
        patch("ma_poc.fetch.fetcher.ENABLE_UNLOCKER_TIER", True),
        patch(
            "ma_poc.fetch.providers.unlocker.UnlockerProvider",
            _mock_unlocker(_UNLOCKED_OK()),
        ) as cls,
    ):
        result = await fetcher._do_request(
            _render_task(), _IDENTITY, None, None, None, 1, 0
        )
    assert result.outcome == FetchOutcome.OK
    cls.assert_not_called()


@pytest.mark.asyncio
async def test_render_bot_blocked_failed_unlock_keeps_blocked() -> None:
    """If the Unlocker itself fails, the original BOT_BLOCKED stands."""
    fetcher = _make_fetcher()
    fetcher._do_render = AsyncMock(return_value=_BLOCKED())
    with (
        patch("ma_poc.fetch.fetcher.ENABLE_TIER_ESCALATION", True),
        patch("ma_poc.fetch.fetcher.ENABLE_UNLOCKER_TIER", True),
        patch(
            "ma_poc.fetch.providers.unlocker.UnlockerProvider",
            _mock_unlocker(_result(FetchOutcome.BOT_BLOCKED, b"", None)),
        ),
    ):
        result = await fetcher._do_request(
            _render_task(), _IDENTITY, None, None, None, 1, 0
        )
    assert result.outcome == FetchOutcome.BOT_BLOCKED


@pytest.mark.asyncio
async def test_unlocker_fallback_none_when_provider_unavailable() -> None:
    """_try_unlocker_fallback returns None (never raises) when the
    UnlockerProvider cannot be constructed."""
    fetcher = _make_fetcher()
    with patch(
        "ma_poc.fetch.providers.unlocker.UnlockerProvider",
        side_effect=RuntimeError("no creds"),
    ):
        out = await fetcher._try_unlocker_fallback(_render_task(), 0)
    assert out is None
