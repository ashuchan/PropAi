"""Playwright browser session lifecycle manager for the extraction engine.

Extracted from scripts/entrata.py — owns browser launch, context creation,
and guaranteed close (finally-close contract). Each property scrape runs inside
one BrowserSession context.
"""
from __future__ import annotations

import asyncio
import urllib.parse
from typing import Any

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)


def _proxy_config(proxy: str | None) -> dict | None:
    """Parse a proxy URL string into a Playwright proxy config dict."""
    if not proxy:
        return None
    if "://" not in proxy:
        proxy = "http://" + proxy
    parsed = urllib.parse.urlparse(proxy)
    if not parsed.hostname or not parsed.port:
        return None
    cfg: dict = {"server": f"{parsed.scheme or 'http'}://{parsed.hostname}:{parsed.port}"}
    if parsed.username:
        cfg["username"] = urllib.parse.unquote(parsed.username)
        cfg["password"] = urllib.parse.unquote(parsed.password or "")
    return cfg


async def _goto_robust(page: Any, url: str, timeout_ms: int = 45000) -> None:
    """Navigate with domcontentloaded primary + best-effort networkidle settle."""
    await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    try:
        await page.wait_for_load_state("load", timeout=5000)
    except Exception:
        pass
    try:
        await page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
        pass
    await asyncio.sleep(1.0)


class BrowserSession:
    """Async context manager that owns a Playwright browser + context lifecycle.

    Usage::

        async with BrowserSession(proxy="http://...") as session:
            page = await session.new_page()
            await _goto_robust(page, url)
            # ... extract ...
    """

    def __init__(
        self,
        proxy: str | None = None,
        user_agent: str = USER_AGENT,
    ) -> None:
        self.proxy = proxy
        self.user_agent = user_agent
        self._browser: Any = None
        self._context: Any = None

    def _build_launch_args(self) -> dict:
        args: dict = {
            "headless": True,
            "args": ["--no-sandbox", "--disable-dev-shm-usage"],
        }
        proxy_cfg = _proxy_config(self.proxy)
        if proxy_cfg:
            args["proxy"] = proxy_cfg
        return args

    def _build_context_args(self) -> dict:
        return {
            "user_agent": self.user_agent,
            "viewport": {"width": 1280, "height": 900},
            "locale": "en-US",
        }

    async def __aenter__(self) -> "BrowserSession":
        from playwright.async_api import async_playwright

        self._pw = await async_playwright().__aenter__()
        self._browser = await self._pw.chromium.launch(**self._build_launch_args())
        self._context = await self._browser.new_context(**self._build_context_args())
        return self

    async def __aexit__(self, *_: Any) -> None:
        try:
            if self._context:
                await self._context.close()
        except Exception:
            pass
        try:
            if self._browser:
                await self._browser.close()
        except Exception:
            pass
        try:
            await self._pw.__aexit__(None, None, None)
        except Exception:
            pass

    async def new_page(self) -> Any:
        """Create and return a new page in the current browser context."""
        if self._context is None:
            raise RuntimeError("BrowserSession not entered — use 'async with BrowserSession()'")
        return await self._context.new_page()
