"""Playwright browser context pool for RENDER mode.

Manages a single browser instance with multiple isolated contexts.
Each property gets its own context (torn down after use) but the browser
is reused across properties.

Uses context.close() not browser.close() — existing convention.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, Page

from .proxy.base import ProxyConfig
from .stealth import Identity

log = logging.getLogger(__name__)


class BrowserContextPool:
    """Pool of Playwright browser contexts, one per property.

    Args:
        max_contexts: Maximum concurrent contexts.
    """

    def __init__(self, max_contexts: int = 1) -> None:
        self._max_contexts = max_contexts
        self._semaphore = asyncio.Semaphore(max_contexts)
        self._browser: Browser | None = None
        self._lock = asyncio.Lock()
        self._active_contexts: list[BrowserContext] = []

    async def _ensure_browser(self) -> Browser:
        """Launch browser if not already running."""
        if self._browser is None or not self._browser.is_connected():
            async with self._lock:
                if self._browser is None or not self._browser.is_connected():
                    from playwright.async_api import async_playwright

                    pw = await async_playwright().start()
                    self._browser = await pw.chromium.launch(headless=True)
                    log.info("Launched Playwright browser")
        return self._browser

    async def acquire(
        self,
        identity: Identity,
        proxy: str | ProxyConfig | None = None,
    ) -> Page:
        """Acquire a new page in an isolated browser context.

        Args:
            identity: Browser identity (UA, viewport, etc.).
            proxy: Either a legacy proxy URL string, a ``ProxyConfig``, or
                None for direct. When a ``ProxyConfig`` routes through
                Bright Data, ``ignore_https_errors`` is flipped on so the
                proxy's TLS termination doesn't abort requests with
                ``ERR_CERT_AUTHORITY_INVALID``. For direct fetches, TLS is
                verified normally.

        Returns:
            A Playwright Page ready for navigation.
        """
        await self._semaphore.acquire()
        browser = await self._ensure_browser()

        context_opts: dict[str, object] = {
            "user_agent": identity.user_agent,
            "viewport": {"width": identity.viewport[0], "height": identity.viewport[1]},
            "locale": identity.accept_language.split(",")[0],
        }
        if isinstance(proxy, ProxyConfig):
            pw_proxy = proxy.to_playwright()
            if pw_proxy is not None:
                context_opts["proxy"] = pw_proxy
                # Bright Data terminates TLS at the proxy; its proxy cert
                # is not the target site's cert. Only relax verification
                # when a non-direct proxy is in use.
                context_opts["ignore_https_errors"] = True
        elif isinstance(proxy, str) and proxy:
            context_opts["proxy"] = {"server": proxy}

        context = await browser.new_context(**context_opts)  # type: ignore[arg-type]
        self._active_contexts.append(context)
        page = await context.new_page()
        return page

    async def release(self, page: Page) -> None:
        """Release a page and close its context.

        Args:
            page: The page to release.
        """
        try:
            context = page.context
            await context.close()
            if context in self._active_contexts:
                self._active_contexts.remove(context)
        except Exception as exc:
            log.warning("Error releasing browser context: %s", exc)
        finally:
            self._semaphore.release()

    async def close(self) -> None:
        """Close all contexts and the browser."""
        for ctx in list(self._active_contexts):
            try:
                await ctx.close()
            except Exception:
                pass
        self._active_contexts.clear()
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None
        log.info("Browser pool closed")
