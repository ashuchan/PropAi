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

# Shard_10 fix (2026-05-17): wrap every awaitable inside ``acquire()`` in a
# host-level ``asyncio.wait_for`` so a wedged Chromium IPC cannot park the
# caller for the full per-property 600s wallclock. The shard_84 fix from
# 2026-05-16 hardened ``page.goto`` (fetch/fetcher.py:707) and
# ``context.close`` (browser_pool.release, line 195), but the wedge had
# migrated UP into ``_semaphore.acquire`` / ``browser.new_context`` /
# ``context.new_page`` — none of which were timeout-wrapped. Shard 10 of
# 2026-05-17 had 22/50 PIDs hit the per-property wallclock with body_bytes=0,
# 10 of them double-wedged (entry + hop) for ~63 min wallclock each.
#
# 30s is a conservative cap: legitimate Chromium acquire takes <1s in
# steady state; even under pool contention with 8 concurrent slots,
# spinning up a new context+page rarely exceeds 5s. A 30s cap gives 30×
# headroom over the observed legitimate ceiling while limiting wedge
# blast radius to 5% of the per-property budget.
ACQUIRE_TIMEOUT_MS = _resolve_int_env("BROWSER_POOL_ACQUIRE_TIMEOUT_MS", 30_000)


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

        Raises:
            asyncio.TimeoutError: If any step (semaphore, browser launch,
                context creation, page creation) exceeds
                ``ACQUIRE_TIMEOUT_MS``. The semaphore slot is released
                on timeout so the pool does not leak slots; the caller is
                expected to treat the timeout as a TRANSIENT fetch failure.
        """
        timeout_s = ACQUIRE_TIMEOUT_MS / 1000.0
        # Step 1 — semaphore. Timeout-wrapped because a previous wedged
        # acquire on a degraded Chromium child can hold a slot until the
        # per-property wallclock fires (600s). With ``timeout_s`` we cap
        # the wait to a fraction of that, then the caller can fail-fast
        # the fetch as TRANSIENT and free its own slot.
        try:
            await asyncio.wait_for(self._semaphore.acquire(), timeout=timeout_s)
        except TimeoutError:
            log.warning(
                "browser_pool.acquire: semaphore.acquire() exceeded %.1fs — "
                "pool starved (all %d slots held). Failing fast.",
                timeout_s, self._max_contexts,
            )
            raise

        # From here on, the semaphore is HELD. Every error path below must
        # release it before re-raising; otherwise a downstream wedge leaks
        # a pool slot permanently.
        try:
            # Step 2 — browser launch (cached after first call).
            browser = await asyncio.wait_for(
                self._ensure_browser(), timeout=timeout_s,
            )

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

            # Step 3 — new context. Can wedge when the browser process is
            # alive but the IPC channel is degraded.
            context = await asyncio.wait_for(
                browser.new_context(**context_opts),  # type: ignore[arg-type]
                timeout=timeout_s,
            )
            self._active_contexts.append(context)

            # Step 4 — new page. Same wedge surface as step 3. On timeout
            # the context is registered in ``_active_contexts`` so the
            # close-path can still reach it; abandon it from the list so
            # we don't leak a dangling reference.
            try:
                page = await asyncio.wait_for(
                    context.new_page(), timeout=timeout_s,
                )
            except TimeoutError:
                log.warning(
                    "browser_pool.acquire: context.new_page() exceeded %.1fs — "
                    "abandoning wedged context.",
                    timeout_s,
                )
                try:
                    self._active_contexts.remove(context)
                except ValueError:
                    pass
                raise

            page.set_default_timeout(DEFAULT_PAGE_TIMEOUT_MS)
            page.set_default_navigation_timeout(DEFAULT_NAV_TIMEOUT_MS)
            return page
        except BaseException:
            # Any failure (TimeoutError, Playwright error, asyncio cancel)
            # must release the semaphore so the slot count stays accurate.
            # release() is sync and never raises.
            self._semaphore.release()
            raise

    async def release(self, page: Page) -> None:
        """Release a page and close its context.

        Args:
            page: The page to release.

        RC-A (2026-05-15 PM): wrap ``context.close()`` in
        ``asyncio.wait_for(timeout=10s)``. If a Chromium renderer is wedged
        (dead IPC, hung CF JS challenge in the page's event loop),
        ``context.close()`` can park forever waiting on a websocket reply
        that never arrives. The pre-fix code's ``except Exception`` did NOT
        catch that hang — the semaphore was held indefinitely and every
        subsequent ``acquire()`` on the same shard blocked silently,
        emitting only ``fetch.started`` for downstream PIDs. Shard 64 on
        2026-05-15 wedged 29 of 50 PIDs through exactly this mechanism.
        On timeout we forget the dead context (the OS-level Chromium child
        will be reaped when the worker process exits or the browser
        restarts) and release the semaphore so subsequent fetches proceed.

        Shard_84 follow-up (2026-05-16): in addition to abandoning the
        wedged context, FORCE A FULL BROWSER RESTART after the first
        timeout. Shard_84 on 2026-05-16 had 26/50 PIDs wedge at the
        ``page.goto`` step (renderer IPC unresponsive) — that meant every
        new ``acquire()`` on the same ``_browser`` instance re-hit the
        degraded Chromium process and re-wedged. By closing the parent
        browser and resetting ``self._browser = None``, the next
        ``_ensure_browser()`` launches a fresh Chromium child with a fresh
        IPC channel — turning a shard-killing wedge into a one-PID cost.
        """
        context = page.context
        force_restart = False
        try:
            try:
                await asyncio.wait_for(context.close(), timeout=10.0)
            except asyncio.TimeoutError:
                log.warning(
                    "browser_pool.release: context.close() exceeded 10s — "
                    "abandoning the wedged Chromium context AND restarting "
                    "the browser to avoid further IPC-dead acquires."
                )
                force_restart = True
            except Exception as exc:
                log.warning("Error releasing browser context: %s", exc)
            if context in self._active_contexts:
                try:
                    self._active_contexts.remove(context)
                except ValueError:
                    pass
        finally:
            self._semaphore.release()

        # Shard_84 fix (2026-05-16): browser-level restart on detected
        # wedge. Done outside the try/finally so the semaphore is released
        # FIRST — restart can itself take a few seconds, and we don't want
        # downstream acquires waiting on the broken pool.
        if force_restart:
            try:
                async with self._lock:
                    # Re-check inside the lock — another release() may have
                    # already restarted concurrently.
                    if self._browser is not None:
                        try:
                            await asyncio.wait_for(
                                self._browser.close(), timeout=5.0
                            )
                        except Exception as exc:
                            log.warning(
                                "browser_pool: force-restart close failed: %s — "
                                "abandoning the parent browser; next acquire "
                                "will launch a fresh one.",
                                exc,
                            )
                        self._browser = None
                        # Clear remaining stale context refs; OS will reap
                        # the children.
                        self._active_contexts.clear()
            except Exception as exc:
                log.warning("browser_pool: force-restart failed: %s", exc)

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
