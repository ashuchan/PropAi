"""Tests for fetch/providers/dc_proxy.py — DcProxyProvider."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ma_poc.fetch.contracts import FetchOutcome, RenderMode
from ma_poc.models.fetch_tier import FetchTier


def _make_task() -> MagicMock:
    task = MagicMock()
    task.url = "https://example.com/apartments"
    task.property_id = "prop-xyz"
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
async def test_dc_proxy_sets_tier() -> None:
    """DcProxyProvider should stamp fetch_tier_used = DC_PROXY."""
    task = _make_task()
    profile = _make_profile()

    mock_resp = _mock_response(200)

    with (
        patch("ma_poc.fetch.providers.dc_proxy.BrightDataProvider.__init__", return_value=None),
        patch("ma_poc.fetch.providers.dc_proxy.BrightDataProvider.get_config") as mock_get_cfg,
        patch("httpx.AsyncClient") as mock_client_cls,
    ):
        # Fake proxy config
        fake_cfg = MagicMock()
        fake_cfg.to_httpx_url.return_value = "http://***@brd.superproxy.io:33335"
        mock_get_cfg.return_value = fake_cfg

        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_client.request = AsyncMock(return_value=mock_resp)

        from ma_poc.fetch.providers.dc_proxy import DcProxyProvider
        provider = DcProxyProvider()
        result = await provider.fetch(task, profile)

    assert result.fetch_tier_used == int(FetchTier.DC_PROXY)
    assert int(FetchTier.DC_PROXY) in result.fetch_tier_attempts


@pytest.mark.asyncio
async def test_dc_proxy_bot_blocked_no_retry() -> None:
    """BOT_BLOCKED from DC proxy returns immediately."""
    task = _make_task()
    profile = _make_profile()

    mock_resp = _mock_response(403, b"<html>blocked</html>")
    call_count = 0

    async def fake_request(*args, **kwargs):  # noqa: ANN202
        nonlocal call_count
        call_count += 1
        return mock_resp

    with (
        patch("ma_poc.fetch.providers.dc_proxy.BrightDataProvider.__init__", return_value=None),
        patch("ma_poc.fetch.providers.dc_proxy.BrightDataProvider.get_config") as mock_get_cfg,
        patch("httpx.AsyncClient") as mock_client_cls,
    ):
        fake_cfg = MagicMock()
        fake_cfg.to_httpx_url.return_value = "http://***@proxy:33335"
        mock_get_cfg.return_value = fake_cfg

        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_client.request = fake_request

        from ma_poc.fetch.providers.dc_proxy import DcProxyProvider
        provider = DcProxyProvider()
        result = await provider.fetch(task, profile)

    assert result.outcome == FetchOutcome.BOT_BLOCKED
    assert call_count == 1


def test_dc_proxy_tier_name() -> None:
    with patch("ma_poc.fetch.providers.dc_proxy.BrightDataProvider.__init__", return_value=None):
        from ma_poc.fetch.providers.dc_proxy import DcProxyProvider
        provider = DcProxyProvider()
    assert provider.tier_name == "DC_PROXY"
