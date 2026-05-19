"""Playwright browser context pool for RENDER mode.

Manages a single browser instance with multiple isolated contexts.
Each property gets its own context (torn down after use) but the browser
is reused across properties.

Uses context.close() not browser.close() — existing convention.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from patchright.async_api import Browser, BrowserContext, Page

from .proxy.base import ProxyConfig
from .stealth import Identity

log = logging.getLogger(__name__)


def _resolve_int_env(name: str, default_ms: int) -> int:
    """Parse a millisecond env var with a positive-value guard.

    Falls back to *default_ms* with a warning when the value is unparseable
    or non-positive — prod misconfig must never hard-fail container startup
    or, worse, silently disable a safety cap.
    """
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default_ms
    try:
        v = int(raw)
    except (TypeError, ValueError):
        log.warning("%s=%r is not an int; using default %dms", name, raw, default_ms)
        return default_ms
    if v <= 0:
        log.warning("%s=%r must be positive; using default %dms", name, raw, default_ms)
        return default_ms
    return v


# Page-level Playwright defaults applied to every page after new_page().
# These act as a safety net behind explicit per-call timeouts (e.g. the
# 20s cap on page.goto in fetch/fetcher.py): any Playwright op that
# *doesn't* pass an explicit timeout — page.content(), page.evaluate(),
# page.wait_for_selector(), page.click(), etc. — would otherwise inherit
# Playwright's default 30s and, on a renderer-IPC hang (dead Chromium
# child), can in practice park forever waiting on a websocket reply
# that's never coming.
#
# Surfacing those hangs as ``playwright.TimeoutError`` propagates through
# the existing per-property guard (asyncio.wait_for(_process_property,
# 600s) in scripts/runners/jugnu.py:307) as a normal exception, instead
# of leaving the coroutine pinned on a non-cancellable await. Without
# this, the per-property timeout's CancelledError can't take effect on
# code parked inside Playwright's IPC layer — that was the wedge mode
# observed across "shards 8/12/17 on three consecutive days" (see
# jugnu.py:_resolve_per_property_timeout).
#
# 60s for general ops is 2x Playwright's default, leaving headroom for
# legitimate slow page.content() on heavy pages (~10-20s observed); 45s
# for navigation is a backstop for the rare page.reload/frame.goto path
# that doesn't pass its own timeout. Override via env vars below.
DEFAULT_PAGE_TIMEOUT_MS = _resolve_int_env("PLAYWRIGHT_PAGE_TIMEOUT_MS", 60_000)
DEFAULT_NAV_TIMEOUT_MS = _resolve_int_env("PLAYWRIGHT_NAV_TIMEOUT_MS", 45_000)


def _parse_blocked_types(val: str | None) -> frozenset[str]:
    """Parse ``BLOCK_RESOURCE_TYPES`` into a set of Playwright resource types.

    ``None`` (unset) → the default block set. An explicit empty string
    disables blocking entirely. Otherwise a comma list, whitespace-stripped
    and lowercased.
    """
    if val is None:
        return frozenset({"image", "font", "media"})
    return frozenset(t.strip().lower() for t in val.split(",") if t.strip())


# Resource types aborted on every render. image/font/media carry the bulk
# of page weight but are never needed for unit/availability extraction
# (we read DOM + XHR/fetch/JSON). Blocking them slashes per-page bandwidth
# — directly material to residential-proxy (per-GB) cost — and speeds
# renders. document/script/xhr/fetch/stylesheet pass through untouched so
# SSR data, API interception, and layout-dependent JS are unaffected.
# Evaluated at import so reload() picks up env overrides.
_BLOCKED_RESOURCE_TYPES = _parse_blocked_types(os.getenv("BLOCK_RESOURCE_TYPES"))


async def _resource_block_route(route: object) -> None:
    """page.route("**/*") handler: abort blocked resource types, pass rest."""
    try:
        rtype = route.request.resource_type  # type: ignore[attr-defined]
    except Exception:
        rtype = ""
    try:
        if rtype in _BLOCKED_RESOURCE_TYPES:
            await route.abort()  # type: ignore[attr-defined]
        else:
            await route.continue_()  # type: ignore[attr-defined]
    except Exception:
        # A route-already-handled race or a torn-down context must never
        # propagate out of the network layer and fail the navigation.
        pass


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
                    from patchright.async_api import async_playwright

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
            # Inject timezone matching the identity's plausible US region.
            # Without this, Playwright contexts on a UTC host (Cloud Run, CI)
            # expose the server timezone via JS Intl.DateTimeFormat — a
            # detectable inconsistency against the locale/UA combination.
            "timezone_id": identity.timezone_id,
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
            from urllib.parse import urlparse

            _parsed = urlparse(proxy)
            _port = f":{_parsed.port}" if _parsed.port else ""
            _server = f"{_parsed.scheme}://{_parsed.hostname}{_port}"
            _pw_proxy: dict[str, str] = {"server": _server}
            if _parsed.username:
                _pw_proxy["username"] = _parsed.username
            if _parsed.password:
                _pw_proxy["password"] = _parsed.password
            context_opts["proxy"] = _pw_proxy
            # BrightData terminates TLS at the proxy edge; without this flag
            # Chromium aborts HTTPS navigations with ERR_CERT_AUTHORITY_INVALID.
            context_opts["ignore_https_errors"] = True

        context = await browser.new_context(**context_opts)  # type: ignore[arg-type]
        self._active_contexts.append(context)
        page = await context.new_page()
        if _BLOCKED_RESOURCE_TYPES:
            try:
                await page.route("**/*", _resource_block_route)
            except Exception as exc:
                # A route-install hiccup must never abort page acquisition —
                # degrade to an unfiltered (costlier) render, not a failed one.
                log.warning("resource-block route install failed: %s", exc)
        page.set_default_timeout(DEFAULT_PAGE_TIMEOUT_MS)
        page.set_default_navigation_timeout(DEFAULT_NAV_TIMEOUT_MS)
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
