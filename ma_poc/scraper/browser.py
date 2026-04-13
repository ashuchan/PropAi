"""
Playwright browser session management.

Acceptance criteria (CLAUDE.md PR-01 / browser.py — critical rules):
- One Playwright Chromium browser per property per scrape cycle.
  Never share contexts across properties.
- Register network interception handler BEFORE page.goto() (Tier 1 needs it).
- Rotate user-agent from a list of 10 realistic desktop Chrome strings.
  Never use the Playwright default headless UA.
- Viewport 1920x1080 (vision screenshot consistency).
- Wait strategy: wait_until="networkidle" + asyncio.sleep(2.0).
- On any exception save partial HTML if available, log FAILED, return — never raise.
- await context.close() in a finally block. Never call browser.close() — it
  destroys all concurrent sessions' contexts.
- Save raw HTML + screenshot to data/raw_html/{property_id}/{date}.html and
  data/screenshots/{property_id}/{date}.png after page load.
"""
from __future__ import annotations

import asyncio
import os
import random
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

from scraper.proxy_manager import ProxyManager

# Playwright is heavy and unavailable in some test environments. Import lazily.
try:
    from playwright.async_api import (
        Browser,
        BrowserContext,
        Page,
        Playwright,
        Response,
        async_playwright,
    )

    PLAYWRIGHT_AVAILABLE = True
except ImportError:  # pragma: no cover - import-only fallback
    PLAYWRIGHT_AVAILABLE = False
    Browser = BrowserContext = Page = Playwright = Response = Any  # type: ignore[assignment,misc]
    async_playwright = None  # type: ignore[assignment]


USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
]


@dataclass
class InterceptedResponse:
    """An XHR/fetch response captured during page load (Tier 1 input)."""

    url: str
    method: str
    status: int
    content_type: str
    body: bytes


@dataclass
class BrowserSession:
    """
    Per-property browser session. Owns its own context — never shared.

    intercepted_api_responses is INSTANCE-SCOPED (bug-hunt #2). Each session
    has its own list; concurrent sessions cannot pollute each other.
    """

    property_id: str
    url: str
    pms_platform: Optional[str] = None
    page: Optional[Any] = None  # playwright Page
    html: Optional[str] = None
    screenshot_path: Optional[Path] = None
    raw_html_path: Optional[Path] = None
    page_load_ms: Optional[int] = None
    failure_reason: Optional[str] = None
    proxy_used: bool = False
    intercepted_api_responses: list[InterceptedResponse] = field(default_factory=list)


# Link text / href patterns that indicate a pricing or availability page.
_AVAIL_LINK_SELECTORS = [
    # Try clicking a nav link whose text matches these patterns
    'a:has-text("Floor Plans")',
    'a:has-text("Floorplans")',
    'a:has-text("Floor plans")',
    'a:has-text("Availability")',
    'a:has-text("Apartments")',
    'a:has-text("Pricing")',
    'a:has-text("Units")',
    'a:has-text("Rent")',
    'a:has-text("View All")',
    # Common href patterns
    'a[href*="floorplan"]',
    'a[href*="floor-plan"]',
    'a[href*="availability"]',
    'a[href*="pricing"]',
    'a[href*="apartments"]',
    'a[href*="/units"]',
    'a[href*="availableunits"]',
]


async def _navigate_to_availability(page: Any, original_url: str, timeout_ms: int) -> None:
    """
    If the current page looks like a homepage (no unit/pricing content),
    try to find and click a link to the availability or floor plans page.
    """
    # Quick check: does the page already have unit-like content?
    has_units = await page.locator(
        ".unitContainer, .pricingWrapper, .unit-card, .unit-row, "
        ".entrata-unit-row, .js-listing-card, .listing-unit-detail-table, "
        "table.units, .floor-plan, .floorplan, [data-unit], .available-unit"
    ).count()
    if has_units > 0:
        return  # Already on a page with unit data

    # Try each selector until one navigates us somewhere new
    for sel in _AVAIL_LINK_SELECTORS:
        try:
            link = page.locator(sel).first
            if await link.count() == 0:
                continue
            # Check it's visible and clickable
            if not await link.is_visible():
                continue
            href = await link.get_attribute("href")
            # Skip anchors that just point to the same page
            if href and href.startswith("#"):
                continue
            await link.click()
            await page.wait_for_load_state("networkidle", timeout=timeout_ms)
            await asyncio.sleep(1.5)
            return
        except Exception:
            continue


class BrowserFleet:
    """
    Owns ONE Playwright instance + ONE shared Chromium browser. Issues a fresh
    BrowserContext per property (so cookies/storage do not leak across sessions).

    Concurrency cap is enforced by the caller via asyncio.Semaphore (fleet.py).
    """

    def __init__(
        self,
        proxy_manager: Optional[ProxyManager] = None,
        data_dir: Optional[Path] = None,
        headless: bool = True,
    ) -> None:
        self.proxy_manager = proxy_manager or ProxyManager()
        self.data_dir = Path(data_dir if data_dir is not None else os.getenv("DATA_DIR", "./data"))
        self.headless = headless
        self._playwright: Optional[Any] = None
        self._browser: Optional[Any] = None
        self._lock = asyncio.Lock()
        self._open_contexts: set[Any] = set()

    async def start(self) -> None:
        if self._playwright is not None:
            return
        if not PLAYWRIGHT_AVAILABLE or async_playwright is None:
            raise RuntimeError("playwright is not installed; pip install playwright && playwright install chromium")
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=self.headless)

    async def stop(self) -> None:
        # Close all open contexts first so their pages get a clean shutdown.
        # This prevents "Target page, context or browser has been closed" and
        # "Future exception was never retrieved" from orphaned navigations.
        for ctx in list(self._open_contexts):
            try:
                await ctx.close()
            except Exception:
                pass
        self._open_contexts.clear()

        # Only call browser.close() at fleet teardown — NEVER mid-run.
        # Bug-hunt #2: never call browser.close() per-session.
        async with self._lock:
            if self._browser is not None:
                try:
                    await self._browser.close()
                except Exception:
                    pass
                self._browser = None
            if self._playwright is not None:
                try:
                    await self._playwright.stop()
                except Exception:
                    pass
                self._playwright = None

    async def scrape(
        self,
        property_id: str,
        url: str,
        pms_platform: Optional[str] = None,
        timeout_ms: Optional[int] = None,
    ) -> BrowserSession:
        """
        Run one scrape: open context → register interceptor → goto → settle →
        capture HTML + screenshot. Always closes context in finally.
        Never raises — failures are recorded on the session.
        """
        if self._browser is None:
            await self.start()
        assert self._browser is not None

        timeout_ms = timeout_ms or int(os.getenv("PAGE_LOAD_TIMEOUT_MS", "30000"))
        session = BrowserSession(property_id=property_id, url=url, pms_platform=pms_platform)

        proxy_cfg = self.proxy_manager.proxy_config_for(url)
        session.proxy_used = proxy_cfg is not None

        context_kwargs: dict[str, Any] = {
            "viewport": {"width": 1920, "height": 1080},
            "user_agent": random.choice(USER_AGENTS),
            "ignore_https_errors": True,
        }
        if proxy_cfg is not None:
            context_kwargs["proxy"] = proxy_cfg

        context: Optional[Any] = None
        _on_response: Any = None  # defined below; needed for cleanup in finally

        try:
            context = await self._browser.new_context(**context_kwargs)
            self._open_contexts.add(context)
            page = await context.new_page()
            session.page = page

            # Register interceptor BEFORE goto so Tier 1 sees the initial XHR/fetch wave.
            async def _on_response(response: Any) -> None:  # pragma: no cover - needs live browser
                try:
                    req = response.request
                    method = req.method
                    rtype = req.resource_type
                    if rtype not in ("xhr", "fetch"):
                        return
                    ct = (response.headers or {}).get("content-type", "")
                    body: bytes
                    try:
                        body = await response.body()
                    except Exception:
                        body = b""
                    session.intercepted_api_responses.append(
                        InterceptedResponse(
                            url=response.url,
                            method=method,
                            status=response.status,
                            content_type=ct,
                            body=body,
                        )
                    )
                except Exception:
                    return

            page.on("response", _on_response)

            t0 = datetime.now(timezone.utc)
            try:
                await page.goto(url, wait_until="networkidle", timeout=timeout_ms)
            except Exception as exc:
                session.failure_reason = f"goto_failed: {exc}"
                # Try to capture whatever rendered
            await asyncio.sleep(2.0)  # SPA settle

            # Try to navigate to the pricing/availability subpage if we landed
            # on a homepage with no unit data.
            try:
                await _navigate_to_availability(page, url, timeout_ms)
            except Exception:
                pass  # best-effort; continue with whatever page we have

            session.page_load_ms = int((datetime.now(timezone.utc) - t0).total_seconds() * 1000)

            try:
                session.html = await page.content()
            except Exception as exc:
                session.failure_reason = (session.failure_reason or "") + f"; content_failed: {exc}"

            today = date.today().isoformat()
            session.raw_html_path = self.data_dir / "raw_html" / property_id / f"{today}.html"
            session.screenshot_path = self.data_dir / "screenshots" / property_id / f"{today}.png"
            session.raw_html_path.parent.mkdir(parents=True, exist_ok=True)
            session.screenshot_path.parent.mkdir(parents=True, exist_ok=True)

            if session.html:
                session.raw_html_path.write_text(session.html, encoding="utf-8", errors="replace")
            try:
                # Bug-hunt #7: screenshot AFTER networkidle + sleep
                await page.screenshot(path=str(session.screenshot_path), full_page=True)
            except Exception as exc:
                session.failure_reason = (session.failure_reason or "") + f"; screenshot_failed: {exc}"

            self.proxy_manager.record(url, success=session.failure_reason is None)
            return session
        except Exception as exc:
            session.failure_reason = f"unhandled: {exc}"
            self.proxy_manager.record(url, success=False)
            return session
        finally:
            # Detach response handler before closing to prevent
            # "Future exception was never retrieved" on pending callbacks.
            if session.page is not None and _on_response is not None:
                try:
                    session.page.remove_listener("response", _on_response)
                except Exception:
                    pass
            # Explicitly close the page first — this flushes pending navigation
            # futures cleanly instead of letting them error when the context dies.
            if session.page is not None:
                try:
                    await session.page.close()
                except Exception:
                    pass
            session.page = None
            # Bug-hunt #1: always close the context.
            if context is not None:
                try:
                    await context.close()
                except Exception:
                    pass
                self._open_contexts.discard(context)
