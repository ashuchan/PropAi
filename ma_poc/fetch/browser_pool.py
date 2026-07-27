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


# 2026-05-28 — Time-sink third-party host blocklist. The 2026-05-27 c612
# canary surfaced 77 per-property 600s timeouts where direct curl_cffi
# fetched the same site in <1s — i.e. the page itself is fine, but our
# Playwright session stalls on background bundles. Pattern analysis of
# the 77 timeouts found 26 sites carrying Elise AI chatbot bundles (open
# persistent WebSockets that keep network active) and 19 sites carrying
# G5 marketing bundles (heavy analytics + mutation observers). These
# third-party assets contribute nothing to unit-extraction — blocking
# them at the route layer lets the page finish loading cleanly without
# changing any extraction logic.
#
# Membership rule: domain MUST be confirmed as (a) third-party (never the
# property's own host), (b) carrying no extractable unit data, AND (c)
# observed to cause stalls in real canary runs. Don't add a host on
# speculation — false positives can break legit SSR rent extraction.
_BLOCKED_HOST_SUFFIXES: frozenset[str] = frozenset({
    # Chatbots / virtual leasing agents — heavy WebSocket workers
    "meetelise.com",         # Elise AI — 26 timeouts in c612 canary
    "elise.com",
    "sierra.chat",           # Sierra AI chatbot
    "theconversioncloud.com",  # Conversion Cloud chatbot
    "nestiolistings.com",    # Nestio chat widget
    "rentgrata.com",         # Rentgrata lead widget
    # Marketing / analytics bundles that stall page-load
    "g5search.com",          # G5 marketing — 19 timeouts in c612 canary
    "g5dxcdn.com",
    "g5marketingcloud.com",
    # Tag managers / generic analytics (still let GA/GTM document loads
    # through if we ever depend on them, but their script bundles are
    # the issue). Resource-type blocking image/font/media already covers
    # pixel beacons; this catches the heavy script bundles.
    "googletagmanager.com",
    "doubleclick.net",
    "go-mpulse.net",
    "visitor-analytics.io",
    "hotjar.com",
    "userway.org",
    # Note: NOT blocking google-analytics.com itself, as some legit
    # extraction paths read GA-injected JSON for property-id discovery.
})


def _host_is_blocked(url: str) -> bool:
    """True when ``url``'s host matches any entry in ``_BLOCKED_HOST_SUFFIXES``.

    Matches on suffix (so ``foo.meetelise.com`` matches ``meetelise.com``)
    but not on prefix-only (so ``meetelise.com.evil.com`` would NOT match
    — but the urlparse netloc is the eTLD+1 in practice).
    """
    try:
        from urllib.parse import urlparse
        host = urlparse(url).netloc.lower()
    except Exception:
        return False
    if not host:
        return False
    # Strip port if present
    if ":" in host:
        host = host.split(":", 1)[0]
    for suffix in _BLOCKED_HOST_SUFFIXES:
        if host == suffix or host.endswith("." + suffix):
            return True
    return False


async def _resource_block_route(route: object) -> None:
    """page.route("**/*") handler: abort blocked resource types AND blocked
    third-party hosts, pass everything else through."""
    req = None
    rtype = ""
    url = ""
    try:
        req = route.request  # type: ignore[attr-defined]
        rtype = req.resource_type
        url = req.url
    except Exception:
        pass
    try:
        # 2026-05-28: host-blocklist precedes resource-type check so a
        # `xhr` request to meetelise.com still gets aborted (resource-type
        # alone never blocks xhr/fetch/document).
        if url and _host_is_blocked(url):
            await route.abort()  # type: ignore[attr-defined]
            return
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
        driver: ``"patchright"`` (default — the stealth-Chromium fork used by
            the existing bot-blocked render path) or ``"playwright"`` (vanilla,
            un-patched — required by the clean "2a" residential-render tier so
            it does NOT ship an anti-detection browser).
        engine: ``"chromium"`` (default) or ``"firefox"``.
        headless: ``True`` (default; modern Playwright launches new-headless)
            or ``False`` for a headful window (needs a display / xvfb on
            headless hosts — passes CF challenges far better).
        channel: e.g. ``"chrome"`` to drive the stock installed Chrome instead
            of the bundled Chromium (more genuinely a real browser). Chromium
            only; ignored for Firefox. ``None`` = bundled build.

    Defaults reproduce the previous behaviour exactly (patchright / chromium /
    headless / bundled), so existing callers are unaffected.
    """

    def __init__(
        self,
        max_contexts: int = 1,
        *,
        driver: str = "patchright",
        engine: str = "chromium",
        headless: bool = True,
        channel: str | None = None,
    ) -> None:
        self._max_contexts = max_contexts
        self._semaphore = asyncio.Semaphore(max_contexts)
        self._browser: Browser | None = None
        self._lock = asyncio.Lock()
        self._active_contexts: list[BrowserContext] = []
        self._driver = driver
        self._engine = engine
        self._headless = headless
        self._channel = channel

    async def _ensure_browser(self) -> Browser:
        """Launch browser if not already running."""
        if self._browser is None or not self._browser.is_connected():
            async with self._lock:
                if self._browser is None or not self._browser.is_connected():
                    # Vanilla ``playwright`` is used ONLY by the clean 2a
                    # residential-render tier (driver="playwright"), which
                    # deliberately ships a real, un-patched browser; every other
                    # caller uses the patchright stealth fork. Single dynamic
                    # import so the two shims' differing types don't collide.
                    # See docs/STEALTH_SHIM_AUDIT.md for the documented exception.
                    import importlib

                    _mod = importlib.import_module(
                        "playwright.async_api"
                        if self._driver == "playwright"
                        else "patchright.async_api"
                    )
                    pw = await _mod.async_playwright().start()
                    launcher = pw.firefox if self._engine == "firefox" else pw.chromium
                    launch_kwargs: dict[str, object] = {"headless": self._headless}
                    # ``channel`` (stock Chrome) applies to Chromium only.
                    if self._channel and self._engine == "chromium":
                        launch_kwargs["channel"] = self._channel
                    self._browser = await launcher.launch(**launch_kwargs)
                    log.info(
                        "Launched %s/%s browser (headless=%s channel=%s)",
                        self._driver, self._engine, self._headless, self._channel,
                    )
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

        Raises:
            Whatever the launch/context/page calls raise, and
            ``asyncio.CancelledError`` — but never while still holding the
            semaphore permit (see ``_open_page``).
        """
        await self._semaphore.acquire()
        try:
            browser = await self._ensure_browser()
            return await self._open_page(browser, identity, proxy)
        except BaseException:
            # The permit is taken BEFORE five more awaits — _ensure_browser()
            # may launch Chromium, and new_context()/new_page() are the
            # un-timeouted Playwright IPC calls this module's header warns can
            # park forever. A cancellation landing on any of them (the link-hop
            # per-fetch cap, the 600s per-property guard) used to leak the
            # permit permanently: release() is the only releaser and it needs a
            # `page` that does not exist yet, so MAX_CONCURRENT_BROWSERS shrank
            # by one for the process lifetime — the documented shard-wedge mode.
            # BaseException, not Exception, because CancelledError is the case
            # that matters.
            self._semaphore.release()
            raise

    async def _open_page(
        self,
        browser: Browser,
        identity: Identity,
        proxy: str | ProxyConfig | None,
    ) -> Page:
        """Build an isolated context and page on *browser*.

        Args:
            browser: Live browser to build the context on.
            identity: Browser identity (UA, viewport, etc.).
            proxy: Legacy proxy URL string, a ``ProxyConfig``, or None.

        Returns:
            A Playwright Page ready for navigation.

        Raises:
            Propagates any failure/cancellation, after discarding a half-built
            context so it is neither leaked into ``_active_contexts`` nor left
            open in the browser.
        """
        from ma_poc.fetch.stealth import REALISTIC_SCREEN

        context_opts: dict[str, object] = {
            "user_agent": identity.user_agent,
            "viewport": {"width": identity.viewport[0], "height": identity.viewport[1]},
            # A real desktop monitor. Headless browsers report a tiny/absent
            # ``window.screen`` (a classic automation tell); pin a realistic,
            # fixed 1080p screen (>= viewport). Honest realism, not evasion.
            "screen": {"width": REALISTIC_SCREEN[0], "height": REALISTIC_SCREEN[1]},
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

        context: BrowserContext | None = None
        try:
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
        except BaseException:
            # A context created here has no page yet, so release() can never
            # reach it: without this it stays in _active_contexts forever AND
            # stays open in the browser. Drop the bookkeeping first (cheap, can
            # not fail) and only then attempt the close, which is an await that
            # a pending cancellation will abort immediately.
            if context is not None:
                if context in self._active_contexts:
                    self._active_contexts.remove(context)
                try:
                    await context.close()
                except BaseException:  # pragma: no cover — best effort
                    pass
            raise

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
