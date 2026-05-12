"""camoufox browser pool — Firefox-based alternative to patchright for CF bypass.

camoufox (https://camoufox.com) is a patched Firefox build designed to evade
bot detection. CF's scoring algorithm weights Chrome headless more aggressively
than Firefox, so camoufox can sometimes pass CF challenges that patchright fails.

Key differences from patchright:
  - Firefox engine (Gecko vs Blink) — different JA3/TLS fingerprint, different
    navigator.* properties, different rendering behavior
  - camoufox patches hundreds of Firefox fingerprinting surfaces: canvas, WebGL,
    AudioContext, fonts, screen resolution, hardware concurrency, timezone, locale
  - CF treats Firefox headless traffic as lower risk than Chrome headless because
    Firefox is used far less for automated scraping

Install:
    pip install camoufox[geoip]
    camoufox fetch  # downloads Firefox binary

Usage:
    ENABLE_CAMOUFOX=true  (env var to opt in)

Limitations:
  - camoufox is slower than patchright (~5-8s vs ~2-3s per page load)
  - Not all Playwright API methods work identically
  - Only available on Linux/macOS (Windows support is experimental)
  - Only helps for CF JS challenges — WAF blocks still need residential proxy

See: https://github.com/daijro/camoufox
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)

ENABLE_CAMOUFOX = os.getenv("ENABLE_CAMOUFOX", "false").lower() == "true"


def is_available() -> bool:
    """Check if camoufox is installed."""
    try:
        import camoufox  # noqa: F401
        return True
    except ImportError:
        return False


class CamoufoxPool:
    """Firefox browser pool using camoufox for CF bypass.

    Drop-in replacement for BrowserContextPool when ENABLE_CAMOUFOX=true.
    Falls back to BrowserContextPool (patchright) if camoufox is not installed.
    """

    def __init__(self, max_contexts: int = 1) -> None:
        self._max_contexts = max_contexts
        self._semaphore = asyncio.Semaphore(max_contexts)
        self._browser: Any = None
        self._lock = asyncio.Lock()
        self._fallback: Any = None  # BrowserContextPool fallback

        if not is_available():
            log.warning(
                "camoufox not installed — falling back to patchright. "
                "Install with: pip install camoufox[geoip] && camoufox fetch"
            )
            from .browser_pool import BrowserContextPool
            self._fallback = BrowserContextPool(max_contexts)

    async def acquire(self, identity: Any, proxy: Any = None) -> Any:
        if self._fallback is not None:
            return await self._fallback.acquire(identity, proxy)

        await self._semaphore.acquire()
        await self._ensure_browser(identity)
        context_opts: dict[str, Any] = {
            "os": _platform_to_camoufox_os(identity.platform),
            "locale": identity.accept_language.split(",")[0],
            # camoufox handles UA, viewport, timezone, canvas/WebGL automatically
            # based on OS fingerprint — no need to set them manually
        }
        if proxy is not None:
            from .proxy.base import ProxyConfig
            if isinstance(proxy, ProxyConfig):
                pw_proxy = proxy.to_playwright()
                if pw_proxy:
                    context_opts["proxy"] = pw_proxy
            elif isinstance(proxy, str) and proxy:
                context_opts["proxy"] = {"server": proxy}

        try:
            context = await self._browser.new_context(**context_opts)  # type: ignore[attr-defined]
            page = await context.new_page()
            return page
        except Exception as exc:
            self._semaphore.release()
            raise exc

    async def _ensure_browser(self, identity: Any) -> None:
        if self._browser is not None:
            return
        async with self._lock:
            if self._browser is not None:
                return
            from camoufox.async_api import AsyncCamoufox  # type: ignore[import]
            cf = AsyncCamoufox(
                headless=True,
                os=_platform_to_camoufox_os(identity.platform),
            )
            self._browser = await cf.__aenter__()
            log.info("Launched camoufox Firefox browser")

    async def release(self, page: Any) -> None:
        if self._fallback is not None:
            return await self._fallback.release(page)
        try:
            ctx = page.context
            await ctx.close()
        except Exception as exc:
            log.warning("camoufox release error: %s", exc)
        finally:
            self._semaphore.release()

    async def close(self) -> None:
        if self._fallback is not None:
            return await self._fallback.close()
        if self._browser:
            try:
                from camoufox.async_api import AsyncCamoufox  # type: ignore[import]
                await AsyncCamoufox.__aexit__(self._browser, None, None, None)
            except Exception:
                pass
            self._browser = None
        log.info("camoufox pool closed")


def _platform_to_camoufox_os(platform: str) -> str:
    """Map our Identity.platform string to camoufox's OS parameter."""
    mapping = {"Windows": "windows", "macOS": "macos", "Linux": "linux"}
    return mapping.get(platform, "linux")


def get_browser_pool(max_contexts: int = 1) -> Any:
    """Return CamoufoxPool when ENABLE_CAMOUFOX=true, BrowserContextPool otherwise."""
    if ENABLE_CAMOUFOX and is_available():
        log.info("Using camoufox Firefox pool for CF bypass")
        return CamoufoxPool(max_contexts)
    from .browser_pool import BrowserContextPool
    return BrowserContextPool(max_contexts)
