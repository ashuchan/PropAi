"""Shared AppFolio-embed recovery for syndication (Wix/Squarespace) shells.

2026-05-19 deep-probe finding: the second-largest recoverable failed-case
cluster (~26 properties) is Wix/Squarespace marketing shells that the
detector dead-ends as ``SYNDICATION_ONLY_*`` even though the real unit data
is one labelled-nav hop deep, embedded as a **cross-origin iframe** to the
AppFolio tenant subdomain:

    <iframe src="https://{tenant}.appfolio.com/listings?..."> ...

Verified live on brooksidejohnsoncreek.com (Squarespace) → ``/listings``
sub-page → ``illumepm.appfolio.com/listings`` iframe = 69 ``data-listing-id``
blocks / 1097 ``js-listing-*`` nodes — exactly what
``parse_appfolio_listings_ssr`` already extracts. The capability exists; the
gap is purely routing. This module is the self-contained recovery: scan the
live page, else probe the well-known sub-paths, find the AppFolio iframe,
fetch it in-session, and hand the HTML to the existing SSR parser.

The fetch runs through ``page.evaluate`` so the page's own origin/session is
reused (same pattern as the Entrata known-endpoint probe).
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING
from urllib.parse import urlparse, urlunparse

from ma_poc.pms.adapters.appfolio import parse_appfolio_listings_ssr

if TYPE_CHECKING:
    from playwright.async_api import Page

    from ma_poc.pms.adapters.base import AdapterContext

log = logging.getLogger(__name__)

# AppFolio tenant-subdomain listings iframe/script, e.g.
# https://illumepm.appfolio.com/listings?... — the host is the tenant slug,
# never bare ``appfolio.com``. ``/listings`` is the canonical SSR path.
_APPFOLIO_IFRAME_RE = re.compile(
    r"""https?://[a-z0-9][a-z0-9-]*\.appfolio\.com/listings[^\s"'<>]*""",
    re.IGNORECASE,
)

# Sub-paths a marketing shell uses for the page that embeds the AppFolio
# widget. Ordered by observed frequency in the 2026-05-19 probe. Kept tight
# — every entry was seen on a real failed-case property.
_APPFOLIO_EMBED_SUBPATHS: tuple[str, ...] = (
    "/listings",
    "/availability",
    "/available-units",
    "/availableunits",
    "/properties-for-rent",
    "/available-rentals",
    "/floorplans",
    "/floor-plans",
    "/apartments",
    "/rentals",
)

# JS run on the live page: harvest any AppFolio iframe/script URL already
# present (the cheap path — fires when the shell embeds the widget on the
# page we're already on).
_LIVE_APPFOLIO_SRC_JS = r"""
() => {
  const out = [];
  document.querySelectorAll('iframe, script').forEach((el) => {
    const s = el.src || '';
    if (/\.appfolio\.com\/listings/i.test(s)) out.push(s);
  });
  return out;
}
"""


def _origin(page: Page, ctx: AdapterContext) -> str:
    """scheme://host for sub-path probing — prefer the settled page URL."""
    candidate = ""
    try:
        candidate = page.url or ""
    except Exception:
        candidate = ""
    if not candidate:
        candidate = getattr(ctx, "base_url", "") or ""
    try:
        p = urlparse(candidate)
    except Exception:
        return ""
    if not p.scheme or not p.netloc:
        return ""
    return urlunparse((p.scheme, p.netloc, "", "", "", ""))


async def _fetch(page: Page, url: str) -> str:
    """Fetch *url* in-session via page.evaluate. Never raises — '' on failure."""
    evaluate = getattr(page, "evaluate", None)
    if not callable(evaluate):
        return ""
    try:
        body = await evaluate(
            "(u) => fetch(u, {credentials: 'include'})"
            ".then(r => r.ok ? r.text() : '').catch(() => '')",
            url,
        )
    except Exception as exc:  # pragma: no cover — network/SDK variance
        log.debug("AppFolio-embed fetch failed url=%s err=%s", url, exc)
        return ""
    return body if isinstance(body, str) else ""


async def recover_appfolio_embed(
    page: Page,
    ctx: AdapterContext,
) -> list[dict[str, str]]:
    """Find an embedded AppFolio listings widget and parse it.

    Returns SSR-parsed unit dicts, or ``[]`` when no AppFolio embed is
    discoverable (so a genuine no-PMS Wix/Squarespace site is unaffected).
    Never raises.
    """
    evaluate = getattr(page, "evaluate", None)
    if not callable(evaluate):
        return []

    # 1. Cheap path — AppFolio iframe already on the current page.
    iframe_urls: list[str] = []
    try:
        live = await evaluate(_LIVE_APPFOLIO_SRC_JS)
        if isinstance(live, list):
            iframe_urls = [u for u in live if isinstance(u, str) and u]
    except Exception as exc:
        log.debug("AppFolio-embed live scan failed err=%s", exc)

    # 2. Probe the well-known sub-paths; pull the iframe src out of each.
    if not iframe_urls:
        origin = _origin(page, ctx)
        if origin:
            for path in _APPFOLIO_EMBED_SUBPATHS:
                html = await _fetch(page, origin + path)
                if not html:
                    continue
                m = _APPFOLIO_IFRAME_RE.search(html)
                if m:
                    iframe_urls = [m.group(0)]
                    break

    # 3. Fetch the AppFolio listings page itself and run the existing SSR
    #    parser. First non-empty wins.
    for src in iframe_urls:
        html = await _fetch(page, src)
        if not html:
            continue
        units = parse_appfolio_listings_ssr(html, src)
        if units:
            return units
    return []
