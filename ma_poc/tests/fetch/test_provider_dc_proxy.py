"""Tests for fetch/providers/dc_proxy.py — DcProxyProvider."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ma_poc.fetch.contracts import FetchOutcome, RenderMode
from ma_poc.fetch.http_client import _AdapterResponse
from ma_poc.models.fetch_tier import FetchTier

_URL = "https://example.com/apartments"


def _make_task() -> MagicMock:
    task = MagicMock()
    task.url = _URL
    task.property_id = "prop-xyz"
    task.render_mode = RenderMode.GET
    task.budget_ms = 10000
    task.etag = None
    task.last_modified = None
    return task


def _make_profile() -> MagicMock:
    return MagicMock()


def _adapter_resp(status: int = 200, body: bytes = b"<html>ok</html>") -> _AdapterResponse:
    return _AdapterResponse(
        status_code=status,
        headers={"content-type": "text/html"},
        content=body,
        final_url=_URL,
        cookies={},
    )


def _mock_adapter(resp: _AdapterResponse) -> AsyncMock:
    adapter = AsyncMock()
    adapter.request = AsyncMock(return_value=resp)
    adapter.aclose = AsyncMock()
    return adapter


@pytest.mark.asyncio
async def test_dc_proxy_sets_tier() -> None:
    """DcProxyProvider should stamp fetch_tier_used = DC_PROXY."""
    task = _make_task()
    profile = _make_profile()

    with (
        patch("ma_poc.fetch.providers.dc_proxy.BrightDataProvider.__init__", return_value=None),
        patch("ma_poc.fetch.providers.dc_proxy.BrightDataProvider.get_config") as mock_get_cfg,
        patch("ma_poc.fetch.providers.dc_proxy.make_http_client", return_value=_mock_adapter(_adapter_resp(200))),
    ):
        fake_cfg = MagicMock()
        fake_cfg.to_httpx_url.return_value = "http://***@brd.superproxy.io:33335"
        mock_get_cfg.return_value = fake_cfg

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

    call_count = 0

    async def fake_request(*args, **kwargs):  # noqa: ANN202
        nonlocal call_count
        call_count += 1
        return _adapter_resp(403, b"<html>blocked</html>")

    adapter = AsyncMock()
    adapter.request = fake_request
    adapter.aclose = AsyncMock()

    with (
        patch("ma_poc.fetch.providers.dc_proxy.BrightDataProvider.__init__", return_value=None),
        patch("ma_poc.fetch.providers.dc_proxy.BrightDataProvider.get_config") as mock_get_cfg,
        patch("ma_poc.fetch.providers.dc_proxy.make_http_client", return_value=adapter),
    ):
        fake_cfg = MagicMock()
        fake_cfg.to_httpx_url.return_value = "http://***@proxy:33335"
        mock_get_cfg.return_value = fake_cfg

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
