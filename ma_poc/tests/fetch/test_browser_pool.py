"""Tests for BrowserContextPool.acquire() proxy handling.

These tests mock out Playwright so they run without a browser installed.
They verify that proxy URL strings are correctly parsed into Playwright's
{server, username, password} format and that ignore_https_errors is set
when a proxy is in use (required because BrightData terminates TLS at the
proxy edge).
"""

from __future__ import annotations

import asyncio
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
