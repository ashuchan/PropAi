"""Tests for fetch/providers/residential.py — ResidentialProvider (Phase E4)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ma_poc.fetch.contracts import FetchOutcome, RenderMode
from ma_poc.models.fetch_tier import FetchTier


def _make_task() -> MagicMock:
    task = MagicMock()
    task.url = "https://example.com/apartments"
    task.property_id = "prop-resi-001"
    task.render_mode = RenderMode.GET
    task.budget_ms = 10000
    return task


def _make_profile() -> MagicMock:
    return MagicMock()


def _mock_response(status: int = 200, body: bytes = b"<html>ok</html>") -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.content = body
    resp.headers = {"content-type": "text/html"}
    resp.url = "https://example.com/apartments"
    return resp


@pytest.mark.asyncio
async def test_residential_sets_tier() -> None:
    task = _make_task()
    profile = _make_profile()
    mock_resp = _mock_response(200)

    with (
        patch("ma_poc.fetch.providers.residential.BrightDataProvider.__init__", return_value=None),
        patch("ma_poc.fetch.providers.residential.BrightDataProvider.get_config") as mock_get_cfg,
        patch("httpx.AsyncClient") as mock_client_cls,
    ):
        fake_cfg = MagicMock()
        fake_cfg.to_httpx_url.return_value = "http://***@brd.superproxy.io:33335"
        mock_get_cfg.return_value = fake_cfg

        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_client.request = AsyncMock(return_value=mock_resp)

        from ma_poc.fetch.providers.residential import ResidentialProvider
        provider = ResidentialProvider()
        result = await provider.fetch(task, profile)

    assert result.fetch_tier_used == int(FetchTier.RESIDENTIAL)
    assert int(FetchTier.RESIDENTIAL) in result.fetch_tier_attempts
    assert result.outcome == FetchOutcome.OK


@pytest.mark.asyncio
async def test_residential_bot_blocked_no_retry() -> None:
    """BOT_BLOCKED on residential must return immediately — no retry."""
    task = _make_task()
    profile = _make_profile()
    mock_resp = _mock_response(403, b"<html>blocked</html>")
    call_count = 0

    async def fake_request(*args, **kwargs):  # noqa: ANN202
        nonlocal call_count
        call_count += 1
        return mock_resp

    with (
        patch("ma_poc.fetch.providers.residential.BrightDataProvider.__init__", return_value=None),
        patch("ma_poc.fetch.providers.residential.BrightDataProvider.get_config") as mock_get_cfg,
        patch("httpx.AsyncClient") as mock_client_cls,
    ):
        fake_cfg = MagicMock()
        fake_cfg.to_httpx_url.return_value = "http://***@proxy:33335"
        mock_get_cfg.return_value = fake_cfg

        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_client.request = fake_request

        from ma_poc.fetch.providers.residential import ResidentialProvider
        provider = ResidentialProvider()
        result = await provider.fetch(task, profile)

    assert result.outcome == FetchOutcome.BOT_BLOCKED
    assert call_count == 1


@pytest.mark.asyncio
async def test_residential_uses_sticky_session() -> None:
    """Residential provider must pass property_id as canonical_id to BrightData."""
    task = _make_task()
    profile = _make_profile()
    mock_resp = _mock_response(200)

    with (
        patch("ma_poc.fetch.providers.residential.BrightDataProvider.__init__", return_value=None),
        patch("ma_poc.fetch.providers.residential.BrightDataProvider.get_config") as mock_get_cfg,
        patch("httpx.AsyncClient") as mock_client_cls,
    ):
        fake_cfg = MagicMock()
        fake_cfg.to_httpx_url.return_value = "http://***@proxy:33335"
        mock_get_cfg.return_value = fake_cfg

        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_client.request = AsyncMock(return_value=mock_resp)

        from ma_poc.fetch.providers.residential import ResidentialProvider
        provider = ResidentialProvider()
        await provider.fetch(task, profile)

    # Verify canonical_id was passed (sticky session)
    call_kwargs = mock_get_cfg.call_args
    assert call_kwargs.kwargs["canonical_id"] == task.property_id


@pytest.mark.asyncio
async def test_escalator_includes_residential_when_enabled() -> None:
    """With ENABLE_RESIDENTIAL_TIER=True, ladder should include RESIDENTIAL."""
    from ma_poc.fetch.tier_escalator import _build_ladder

    with (
        patch("ma_poc.fetch.tier_escalator.ENABLE_DC_PROXY_TIER", True),
        patch("ma_poc.fetch.tier_escalator.ENABLE_RESIDENTIAL_TIER", True),
        patch("ma_poc.fetch.tier_escalator.ENABLE_UNLOCKER_TIER", False),
    ):
        ladder = _build_ladder(FetchTier.DIRECT)

    assert FetchTier.RESIDENTIAL in ladder
    assert ladder.index(FetchTier.RESIDENTIAL) > ladder.index(FetchTier.DC_PROXY)


def test_residential_tier_name() -> None:
    with patch("ma_poc.fetch.providers.residential.BrightDataProvider.__init__", return_value=None):
        from ma_poc.fetch.providers.residential import ResidentialProvider
        provider = ResidentialProvider()
    assert provider.tier_name == "RESIDENTIAL"


@pytest.mark.asyncio
async def test_escalation_direct_blocked_dc_blocked_residential_ok() -> None:
    """Full E4 escalation path: DIRECT→DC_PROXY→RESIDENTIAL on BOT_BLOCKED."""
    from ma_poc.fetch.contracts import FetchResult
    from ma_poc.fetch.tier_escalator import fetch_with_escalation

    task = _make_task()
    profile = MagicMock()
    profile.fetch = MagicMock()
    profile.fetch.tier_floor = FetchTier.DIRECT

    blocked_direct = FetchResult(
        url=task.url, outcome=FetchOutcome.BOT_BLOCKED, status=403, body=b"",
        headers={}, render_mode=RenderMode.GET, final_url=task.url,
        attempts=1, elapsed_ms=50, fetch_tier_used=0, fetch_tier_attempts=[0],
        block_signature="cf_turnstile",
    )
    blocked_dc = FetchResult(
        url=task.url, outcome=FetchOutcome.BOT_BLOCKED, status=403, body=b"",
        headers={}, render_mode=RenderMode.GET, final_url=task.url,
        attempts=1, elapsed_ms=50, fetch_tier_used=2, fetch_tier_attempts=[2],
        block_signature="cf_turnstile",
    )
    ok_resi = FetchResult(
        url=task.url, outcome=FetchOutcome.OK, status=200, body=b"<html>ok</html>",
        headers={}, render_mode=RenderMode.GET, final_url=task.url,
        attempts=1, elapsed_ms=150, fetch_tier_used=3, fetch_tier_attempts=[3],
    )

    with (
        patch("ma_poc.fetch.tier_escalator.ENABLE_TIER_ESCALATION", True),
        patch("ma_poc.fetch.tier_escalator.ENABLE_DC_PROXY_TIER", True),
        patch("ma_poc.fetch.tier_escalator.ENABLE_RESIDENTIAL_TIER", True),
        patch("ma_poc.fetch.tier_escalator.ENABLE_UNLOCKER_TIER", False),
        patch("ma_poc.fetch.providers.direct.DirectProvider.fetch", AsyncMock(return_value=blocked_direct)),
        patch("ma_poc.fetch.providers.dc_proxy.DcProxyProvider.fetch", AsyncMock(return_value=blocked_dc)),
        patch("ma_poc.fetch.providers.dc_proxy.BrightDataProvider.__init__", return_value=None),
        patch("ma_poc.fetch.providers.residential.ResidentialProvider.fetch", AsyncMock(return_value=ok_resi)),
        patch("ma_poc.fetch.providers.residential.BrightDataProvider.__init__", return_value=None),
    ):
        result = await fetch_with_escalation(task, profile)

    assert result.outcome == FetchOutcome.OK
    assert result.fetch_tier_used == int(FetchTier.RESIDENTIAL)
    assert int(FetchTier.DIRECT) in result.fetch_tier_attempts
    assert int(FetchTier.DC_PROXY) in result.fetch_tier_attempts
    assert int(FetchTier.RESIDENTIAL) in result.fetch_tier_attempts
