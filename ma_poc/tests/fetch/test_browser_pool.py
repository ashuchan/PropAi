"""Tests for BrowserContextPool.acquire() proxy handling.

These tests mock out Playwright so they run without a browser installed.
They verify that proxy URL strings are correctly parsed into Playwright's
{server, username, password} format and that ignore_https_errors is set
when a proxy is in use (required because BrightData terminates TLS at the
proxy edge).
"""

from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

from ma_poc.fetch.browser_pool import BrowserContextPool
from ma_poc.fetch.proxy.base import ProxyConfig, ProxyTier
from ma_poc.fetch.stealth import Identity

_IDENTITY = Identity(
    user_agent="Mozilla/5.0 (Test)",
    accept_language="en-US,en;q=0.9",
    viewport=(1280, 720),
    platform="Windows",
)


def _run(pool: BrowserContextPool, proxy: object) -> dict:
    """Run acquire() with a mocked browser; return the kwargs passed to new_context()."""
    captured: dict = {}

    async def _fake_new_context(**kwargs: object) -> AsyncMock:
        captured.update(kwargs)
        ctx = AsyncMock()
        ctx.new_page = AsyncMock(return_value=MagicMock())
        return ctx

    mock_browser = AsyncMock()
    mock_browser.new_context = _fake_new_context

    async def _go() -> None:
        with patch.object(pool, "_ensure_browser", return_value=mock_browser):
            await pool.acquire(_IDENTITY, proxy)  # type: ignore[arg-type]

    asyncio.run(_go())
    return captured


def _run_capturing_page(pool: BrowserContextPool, proxy: object) -> MagicMock:
    """Variant of _run that returns the mocked Page, so tests can inspect
    set_default_timeout / set_default_navigation_timeout calls."""
    page = MagicMock()

    async def _fake_new_context(**kwargs: object) -> AsyncMock:
        ctx = AsyncMock()
        ctx.new_page = AsyncMock(return_value=page)
        return ctx

    mock_browser = AsyncMock()
    mock_browser.new_context = _fake_new_context

    async def _go() -> None:
        with patch.object(pool, "_ensure_browser", return_value=mock_browser):
            await pool.acquire(_IDENTITY, proxy)  # type: ignore[arg-type]

    asyncio.run(_go())
    return page


# ── No proxy ──────────────────────────────────────────────────────────────────


def test_no_proxy_sets_no_proxy_opts() -> None:
    opts = _run(BrowserContextPool(max_contexts=1), None)
    assert "proxy" not in opts
    assert "ignore_https_errors" not in opts


def test_empty_string_treated_as_no_proxy() -> None:
    opts = _run(BrowserContextPool(max_contexts=1), "")
    assert "proxy" not in opts


def test_proxy_config_direct_sets_no_proxy() -> None:
    cfg = ProxyConfig(tier=ProxyTier.DIRECT)
    opts = _run(BrowserContextPool(max_contexts=1), cfg)
    assert "proxy" not in opts
    assert "ignore_https_errors" not in opts


# ── String proxy URL (PROXY_POOL_URLS path) ───────────────────────────────────


def test_proxy_string_with_credentials_splits_correctly() -> None:
    """Chromium ignores credentials embedded in the server URL; they must be
    passed as separate username/password fields."""
    url = "http://brd-customer-X-zone-datacenter-country-us:SECRETPASS@brd.superproxy.io:33335"
    opts = _run(BrowserContextPool(max_contexts=1), url)
    assert opts["proxy"] == {
        "server": "http://brd.superproxy.io:33335",
        "username": "brd-customer-X-zone-datacenter-country-us",
        "password": "SECRETPASS",
    }


def test_proxy_string_sets_ignore_https_errors() -> None:
    url = "http://user:pass@brd.superproxy.io:33335"
    opts = _run(BrowserContextPool(max_contexts=1), url)
    assert opts.get("ignore_https_errors") is True


def test_proxy_string_without_credentials_sets_server_only() -> None:
    url = "http://brd.superproxy.io:33335"
    opts = _run(BrowserContextPool(max_contexts=1), url)
    pw_proxy = opts["proxy"]
    assert isinstance(pw_proxy, dict)
    assert pw_proxy["server"] == "http://brd.superproxy.io:33335"
    assert "username" not in pw_proxy
    assert "password" not in pw_proxy
    assert opts.get("ignore_https_errors") is True


def test_proxy_string_does_not_embed_creds_in_server() -> None:
    """The server field must be host-only — no user:pass@ prefix."""
    url = "http://user:pass@brd.superproxy.io:33335"
    opts = _run(BrowserContextPool(max_contexts=1), url)
    server = opts["proxy"]["server"]  # type: ignore[index]
    assert "@" not in server
    assert "user" not in server
    assert "pass" not in server


# ── ProxyConfig object path (BrightDataProvider) ─────────────────────────────


def test_proxy_config_with_server_sets_playwright_format() -> None:
    cfg = ProxyConfig(
        tier=ProxyTier.DATACENTER,
        server="http://brd.superproxy.io:33335",
        username="brd-customer-X-zone-Y",
        password="PASS",
    )
    opts = _run(BrowserContextPool(max_contexts=1), cfg)
    assert opts["proxy"] == {
        "server": "http://brd.superproxy.io:33335",
        "username": "brd-customer-X-zone-Y",
        "password": "PASS",
    }
    assert opts.get("ignore_https_errors") is True


def test_proxy_config_direct_produces_no_proxy_key() -> None:
    cfg = ProxyConfig(tier=ProxyTier.DIRECT, server=None)
    opts = _run(BrowserContextPool(max_contexts=1), cfg)
    assert "proxy" not in opts


# ── Real BrightData PROXY_POOL_URLS format ────────────────────────────────────


def test_real_brightdata_url_format_parses_correctly() -> None:
    """Exercises the exact URL shape stored in the proxy-credentials-production
    GCP Secret Manager secret and injected as PROXY_POOL_URLS on Cloud Run."""
    url = "http://brd-customer-hl_6785472d-zone-residential_proxy1:0owuh5392unq@brd.superproxy.io:33335"
    opts = _run(BrowserContextPool(max_contexts=1), url)
    assert opts["proxy"] == {
        "server": "http://brd.superproxy.io:33335",
        "username": "brd-customer-hl_6785472d-zone-residential_proxy1",
        "password": "0owuh5392unq",
    }
    assert opts.get("ignore_https_errors") is True


# ── Page-level default timeouts ──────────────────────────────────────────────
#
# These guard the per-property pipeline against renderer-IPC hangs: Playwright
# ops without an explicit timeout would otherwise inherit the SDK default and,
# on a dead Chromium child, can park indefinitely on a websocket that never
# replies. Setting page-level defaults surfaces such hangs as proper
# TimeoutErrors that the per-property guard in jugnu.py can act on.


def test_acquire_sets_page_default_timeout() -> None:
    page = _run_capturing_page(BrowserContextPool(max_contexts=1), None)
    page.set_default_timeout.assert_called_once()
    (args, kwargs) = page.set_default_timeout.call_args
    value = args[0] if args else kwargs.get("timeout")
    assert isinstance(value, int) and value > 0


def test_acquire_sets_page_default_navigation_timeout() -> None:
    page = _run_capturing_page(BrowserContextPool(max_contexts=1), None)
    page.set_default_navigation_timeout.assert_called_once()
    (args, kwargs) = page.set_default_navigation_timeout.call_args
    value = args[0] if args else kwargs.get("timeout")
    assert isinstance(value, int) and value > 0


def test_default_timeouts_match_module_constants() -> None:
    """Lock the default values so an accidental edit can't silently lower
    them — these caps are part of the wedge-recovery contract."""
    from ma_poc.fetch import browser_pool

    page = _run_capturing_page(BrowserContextPool(max_contexts=1), None)
    page.set_default_timeout.assert_called_with(browser_pool.DEFAULT_PAGE_TIMEOUT_MS)
    page.set_default_navigation_timeout.assert_called_with(browser_pool.DEFAULT_NAV_TIMEOUT_MS)


def test_env_override_for_page_timeout(monkeypatch) -> None:
    """PLAYWRIGHT_PAGE_TIMEOUT_MS env var overrides the default at import time."""
    monkeypatch.setenv("PLAYWRIGHT_PAGE_TIMEOUT_MS", "12345")
    from ma_poc.fetch.browser_pool import _resolve_int_env
    assert _resolve_int_env("PLAYWRIGHT_PAGE_TIMEOUT_MS", 60_000) == 12345


def test_env_override_falls_back_on_garbage(monkeypatch) -> None:
    monkeypatch.setenv("PLAYWRIGHT_PAGE_TIMEOUT_MS", "not-a-number")
    from ma_poc.fetch.browser_pool import _resolve_int_env
    assert _resolve_int_env("PLAYWRIGHT_PAGE_TIMEOUT_MS", 60_000) == 60_000


def test_env_override_rejects_non_positive(monkeypatch) -> None:
    monkeypatch.setenv("PLAYWRIGHT_NAV_TIMEOUT_MS", "0")
    from ma_poc.fetch.browser_pool import _resolve_int_env
    assert _resolve_int_env("PLAYWRIGHT_NAV_TIMEOUT_MS", 45_000) == 45_000


# ── permit safety under cancellation (2026-07-27 review) ───────────────────
#
# ``acquire()`` takes the semaphore permit and THEN does four more awaits —
# ``_ensure_browser()`` (may launch Chromium), ``new_context()``,
# ``new_page()``, ``page.route()`` — before ``return page``. The only releaser
# is the caller's ``finally: await self._browsers.release(page)``, which needs a
# page that does not exist yet. A cancellation landing in that window therefore
# leaked the permit permanently and shrank MAX_CONCURRENT_BROWSERS by one for
# the process lifetime: the documented shard-wedge mode.
#
# ``new_context()``/``new_page()`` are precisely the un-timeouted Playwright IPC
# calls this module's header warns "can in practice park forever" —
# ``page.set_default_timeout`` is not applied until after both. The 2026-07-27
# link-hop per-fetch cap makes such cancellations ~6x more frequent (measured
# against the real ``_try_link_hop``: 1 in-flight cancellation with the cap off,
# 6 with it on), which is what turned a latent leak into a live risk.


def _pool_with_hanging_new_page() -> tuple[BrowserContextPool, list[str]]:
    """A pool whose ``new_page()`` never returns, plus a context-close log."""
    pool = BrowserContextPool(max_contexts=2)
    closed: list[str] = []

    class _HangingCtx:
        async def new_page(self) -> None:
            await asyncio.sleep(30)

        async def close(self) -> None:
            closed.append("closed")

    class _Browser:
        async def new_context(self, **_kw: object) -> _HangingCtx:
            return _HangingCtx()

    async def _ensure() -> _Browser:
        return _Browser()

    pool._ensure_browser = _ensure  # type: ignore[method-assign,assignment]
    return pool, closed


def test_cancelled_acquire_releases_the_semaphore_permit() -> None:
    """Cancelling mid-acquire must not consume a pool slot forever."""

    async def _go() -> tuple[int, int, int]:
        pool, closed = _pool_with_hanging_new_page()
        for _ in range(2):
            task = asyncio.create_task(pool.acquire(_IDENTITY, None))
            await asyncio.sleep(0.05)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        return pool._semaphore._value, len(pool._active_contexts), len(closed)

    permits, active, closed_n = asyncio.run(_go())
    assert permits == 2, (
        f"{permits}/2 permits left after two cancelled acquires — the pool "
        "shrinks by one per cancellation and eventually wedges"
    )
    assert active == 0, (
        f"{active} half-built contexts still tracked as active; release() can "
        "never reach them because they have no page"
    )
    assert closed_n == 2, "half-built contexts must also be closed in the browser"


def test_cancelled_acquire_still_raises_cancelled_error() -> None:
    """Releasing the permit must not swallow the cancellation."""

    async def _go() -> bool:
        pool, _ = _pool_with_hanging_new_page()
        task = asyncio.create_task(pool.acquire(_IDENTITY, None))
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            return True
        return False

    assert asyncio.run(_go()), "acquire() swallowed the cancellation"


def test_failed_acquire_releases_the_permit_too() -> None:
    """A plain exception in new_context() must not strand the permit either."""

    async def _go() -> int:
        pool = BrowserContextPool(max_contexts=1)

        class _Browser:
            async def new_context(self, **_kw: object) -> object:
                raise RuntimeError("chromium died")

        async def _ensure() -> _Browser:
            return _Browser()

        pool._ensure_browser = _ensure  # type: ignore[method-assign,assignment]
        with contextlib.suppress(RuntimeError):
            await pool.acquire(_IDENTITY, None)
        return pool._semaphore._value

    assert asyncio.run(_go()) == 1, "a failed acquire leaked the permit"
