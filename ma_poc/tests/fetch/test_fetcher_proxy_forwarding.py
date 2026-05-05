"""Tests confirming the proxy URL flows from ProxyPool → http_client / browser_pool.

These tests mock at the boundary where the proxy string is actually used —
make_http_client for GET/HEAD fetches and BrowserContextPool.acquire for
RENDER fetches — verifying that the BrightData URL reaches the right call
intact and that proxy_used in FetchResult is redacted.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

from ma_poc.discovery.contracts import CrawlTask, TaskReason
from ma_poc.fetch.browser_pool import BrowserContextPool
from ma_poc.fetch.contracts import RenderMode
from ma_poc.fetch.fetcher import Fetcher
from ma_poc.fetch.http_client import _AdapterResponse
from ma_poc.fetch.proxy_pool import ProxyPool
from ma_poc.fetch.stealth import Identity, IdentityPool

_BD_URL = "http://brd-customer-hl_6785472d-zone-residential_proxy1:0owuh5392unq@brd.superproxy.io:33335"

_IDENTITY = Identity(
    user_agent="Mozilla/5.0 (Test)",
    accept_language="en-US,en;q=0.9",
    viewport=(1280, 720),
    platform="Windows",
)


def _make_mock_adapter(url: str) -> AsyncMock:
    """Return a mock that looks like _HttpxAdapter / _CurlCffiAdapter."""
    adapter = AsyncMock()
    adapter.request = AsyncMock(return_value=_AdapterResponse(
        status_code=200,
        headers={},
        content=b"<html></html>",
        final_url=url,
        cookies={},
    ))
    adapter.aclose = AsyncMock()
    return adapter


def _make_fetcher(proxy_url: str | None = _BD_URL) -> Fetcher:
    urls = [proxy_url] if proxy_url else []
    pool = ProxyPool(urls)
    browsers = BrowserContextPool(max_contexts=1)
    return Fetcher(
        proxy_pool=pool,
        rate_limiter=MagicMock(),
        robots=MagicMock(),
        cond_cache=MagicMock(),
        identities=IdentityPool(),
        browsers=browsers,
        retry=MagicMock(),
    )


def _get_task(render_mode: RenderMode = RenderMode.GET) -> CrawlTask:
    return CrawlTask(
        url="https://example.com/apartments",
        property_id="test-prop-01",
        priority=0,
        budget_ms=10_000,
        reason=TaskReason.SCHEDULED,
        render_mode=render_mode,
    )


# ── GET / HEAD path — make_http_client proxy kwarg ────────────────────────────


def test_get_request_passes_proxy_to_httpx() -> None:
    """make_http_client must receive the BrightData proxy URL."""
    fetcher = _make_fetcher(_BD_URL)
    task = _get_task(RenderMode.GET)
    captured: dict = {}

    def _fake_make_client(tier: object, proxy: object) -> AsyncMock:
        captured["proxy"] = proxy
        return _make_mock_adapter(task.url)

    async def _go() -> None:
        with patch("ma_poc.fetch.fetcher.make_http_client", side_effect=_fake_make_client):
            await fetcher._do_request(task, _IDENTITY, _BD_URL, None, None, 1, int(time.time() * 1000))

    asyncio.run(_go())
    assert captured.get("proxy") == _BD_URL


def test_get_no_proxy_passes_none_to_httpx() -> None:
    """When proxy=None, make_http_client must receive proxy=None."""
    fetcher = _make_fetcher(None)
    task = _get_task(RenderMode.GET)
    captured: dict = {}

    def _fake_make_client(tier: object, proxy: object) -> AsyncMock:
        captured["proxy"] = proxy
        return _make_mock_adapter(task.url)

    async def _go() -> None:
        with patch("ma_poc.fetch.fetcher.make_http_client", side_effect=_fake_make_client):
            await fetcher._do_request(task, _IDENTITY, None, None, None, 1, int(time.time() * 1000))

    asyncio.run(_go())
    assert captured.get("proxy") is None


def test_proxy_used_field_is_redacted_in_result() -> None:
    """FetchResult.proxy_used must not contain the plaintext password."""
    fetcher = _make_fetcher(_BD_URL)
    task = _get_task(RenderMode.GET)

    async def _go() -> object:
        with patch("ma_poc.fetch.fetcher.make_http_client", return_value=_make_mock_adapter(task.url)):
            return await fetcher._do_request(task, _IDENTITY, _BD_URL, None, None, 1, int(time.time() * 1000))

    result = asyncio.run(_go())
    assert result.proxy_used is not None
    assert "0owuh5392unq" not in result.proxy_used, "password must be redacted"
    assert "***" in result.proxy_used


# ── RENDER path — BrowserContextPool.acquire proxy arg ────────────────────────


def test_render_request_passes_proxy_to_browser_acquire() -> None:
    """_do_render must forward the proxy URL to BrowserContextPool.acquire."""
    fetcher = _make_fetcher(_BD_URL)
    task = _get_task(RenderMode.RENDER)
    acquired_with: list = []

    mock_page = MagicMock()
    mock_page.context = MagicMock()
    mock_page.goto = AsyncMock(return_value=MagicMock(status=200))
    mock_page.on = MagicMock()
    mock_page.content = AsyncMock(return_value="<html></html>")
    mock_page.url = task.url

    async def _fake_acquire(identity: object, proxy: object) -> MagicMock:
        acquired_with.append(proxy)
        return mock_page

    async def _go() -> None:
        with patch.object(fetcher._browsers, "acquire", side_effect=_fake_acquire):
            await fetcher._do_render(task, _IDENTITY, _BD_URL, 1, int(time.time() * 1000))

    asyncio.run(_go())
    assert len(acquired_with) == 1
    assert acquired_with[0] == _BD_URL


def test_render_no_proxy_passes_none_to_acquire() -> None:
    """When no proxy is configured, acquire must receive proxy=None."""
    fetcher = _make_fetcher(None)
    task = _get_task(RenderMode.RENDER)
    acquired_with: list = []

    mock_page = MagicMock()
    mock_page.context = MagicMock()
    mock_page.goto = AsyncMock(return_value=MagicMock(status=200))
    mock_page.on = MagicMock()
    mock_page.content = AsyncMock(return_value="<html></html>")
    mock_page.url = task.url

    async def _fake_acquire(identity: object, proxy: object) -> MagicMock:
        acquired_with.append(proxy)
        return mock_page

    async def _go() -> None:
        with patch.object(fetcher._browsers, "acquire", side_effect=_fake_acquire):
            await fetcher._do_render(task, _IDENTITY, None, 1, int(time.time() * 1000))

    asyncio.run(_go())
    assert len(acquired_with) == 1
    assert acquired_with[0] is None


# ── ProxyPool.pick() integration — proxy flows from pool to request ────────────


def test_proxy_pool_with_brightdata_url_returns_it() -> None:
    """ProxyPool.pick() must return the BrightData URL unchanged."""
    pool = ProxyPool([_BD_URL])
    picked = pool.pick(sticky_key="prop-01")
    assert picked == _BD_URL


def test_empty_proxy_pool_returns_none() -> None:
    """An empty ProxyPool must return None so fetcher skips the proxy kwarg."""
    pool = ProxyPool([])
    assert pool.pick(sticky_key="prop-01") is None
