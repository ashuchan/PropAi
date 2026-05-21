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
# Real-world variants captured by the regex above include:
#   {tenant}.appfolio.com/listings                 ← listings SSR index (what we want)
#   {tenant}.appfolio.com/listings?orderable=true  ← listings SSR with filters
#   {tenant}.appfolio.com/listings/detail/{uuid}   ← single-listing detail
#   {tenant}.appfolio.com/listings/showings/new?listable_uid=...  ← "schedule a
#       showing" form — 200 OK but no listings markup, must NOT be fetched
# Canonicalize: strip path after ``/listings`` so we always hit the SSR index.
# 2026-05-19 v2: 100-sample validation surfaced 3 sites with showings/new
# anchor links (no real iframe), where the parser correctly returned 0 units
# but the fetch wasted a round-trip. Canonical URL = ``{scheme}://{host}/listings``.
_APPFOLIO_LISTINGS_HOST_RE = re.compile(
    r"https?://[a-z0-9][a-z0-9-]*\.appfolio\.com/listings",
    re.IGNORECASE,
)


def _to_appfolio_listings_root(url: str) -> str:
    """Strip any path after ``/listings`` (e.g. ``/showings/new``) so we
    fetch the listings SSR index, not a request-a-tour form.
    """
    m = _APPFOLIO_LISTINGS_HOST_RE.match(url)
    return m.group(0) if m else url

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
# 2026-05-20 fallback (probe finding from feature_fail_1429 grind):
# Wix shells often have *no* ``/listings`` iframe anywhere — only an
# auth link like ``{tenant}.appfolio.com/connect/users/sign_in`` (or
# /request_access) in the footer. Same tenant subdomain, different path.
# This scan harvests ANY ``*.appfolio.com/*`` URL from anchors as well as
# iframes/scripts. The Python side extracts the tenant slug and
# constructs the canonical ``https://{tenant}.appfolio.com/listings``.
# Verified live on aptsedenprairie / aptslindenpark / rentdwp (the top
# three SYNDICATION_ONLY_WIX→AppFolio cases worth 712 strict units).
# Sentinel ``tenant-scan`` in the source lets test FakePages dispatch.
_LIVE_APPFOLIO_TENANT_JS = r"""
() => {
  // tenant-scan: any *.appfolio.com URL on the page (anchors + frames)
  const out = [];
  document.querySelectorAll('a, iframe, script').forEach((el) => {
    const s = el.href || el.src || '';
    if (/\.appfolio\.com\//i.test(s)) out.push(s);
  });
  return out;
}
"""

# Extract the tenant subdomain from any ``*.appfolio.com/*`` URL.
# Captures ``bendermanagement`` from
# ``https://bendermanagement.appfolio.com/connect/users/sign_in``.
_APPFOLIO_TENANT_HOST_RE = re.compile(
    r"https?://([a-z0-9][a-z0-9-]*)\.appfolio\.com/",
    re.IGNORECASE,
)


def _tenant_listings_url(any_appfolio_url: str) -> str | None:
    """Extract tenant slug from any AppFolio URL and synthesize the
    canonical ``https://{tenant}.appfolio.com/listings`` root. Returns
    ``None`` if the URL doesn't match the tenant-host pattern.
    """
    m = _APPFOLIO_TENANT_HOST_RE.match(any_appfolio_url)
    if not m:
        return None
    tenant = m.group(1).lower()
    return f"https://{tenant}.appfolio.com/listings"


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


async def _fetch_with_status(page: Page, url: str) -> tuple[int, str]:
    """Fetch *url* in-session via page.evaluate. Returns ``(status, body)``.

    Status is 0 when the network call raised (DNS error, refused, etc.) or
    the page object can't evaluate JS. Body is ``''`` on any non-2xx so
    callers don't accidentally parse a bot-wall HTML payload.

    The dict-shape JS is the new wire format. The string-shape fallback
    keeps existing test ``evaluate`` mocks (which return body strings
    keyed by URL) working unchanged — they're treated as ``(200, body)``
    when the body is non-empty, ``(0, '')`` when empty.
    """
    evaluate = getattr(page, "evaluate", None)
    if not callable(evaluate):
        return 0, ""
    try:
        result = await evaluate(
            "(u) => fetch(u, {credentials: 'include'})"
            ".then(r => r.text().then(b => ({status: r.status, body: r.ok ? b : ''})))"
            ".catch(() => ({status: 0, body: ''}))",
            url,
        )
    except Exception as exc:  # pragma: no cover — network/SDK variance
        log.debug("AppFolio-embed fetch failed url=%s err=%s", url, exc)
        return 0, ""
    if isinstance(result, dict):
        try:
            status = int(result.get("status") or 0)
        except (TypeError, ValueError):
            status = 0
        body = result.get("body")
        return status, body if isinstance(body, str) else ""
    if isinstance(result, str):
        return (200, result) if result else (0, "")
    return 0, ""


async def _fetch(page: Page, url: str) -> str:
    """Body-only convenience wrapper for callers that don't need status."""
    _, body = await _fetch_with_status(page, url)
    return body


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

    # 2.5. Tenant-only fallback (2026-05-20). If steps 1+2 produced no
    # listings URL, scan the live page for ANY ``*.appfolio.com/*``
    # reference (auth/login/dashboard etc.) and synthesize the canonical
    # ``{tenant}.appfolio.com/listings`` URL from the host. Covers
    # SYNDICATION_ONLY_WIX shells whose only AppFolio breadcrumb is a
    # /connect/users/sign_in or /request_access anchor. Dedup by URL.
    if not iframe_urls:
        try:
            tenants = await evaluate(_LIVE_APPFOLIO_TENANT_JS)
        except Exception as exc:
            log.debug("AppFolio-embed tenant scan failed err=%s", exc)
            tenants = None
        if isinstance(tenants, list):
            seen: set[str] = set()
            for u in tenants:
                if not isinstance(u, str):
                    continue
                listings = _tenant_listings_url(u)
                if listings and listings not in seen:
                    seen.add(listings)
                    iframe_urls.append(listings)

    # 3. Fetch the AppFolio listings page itself and run the existing SSR
    #    parser. First non-empty wins. A 401/403/429/503 here is recorded
    #    as a bot-block (the production stack — residential proxy +
    #    Camoufox + cookie-mint reuse — may flip the same probe to a hit).
    from ma_poc.pms.adapters._universal_recovery import is_bot_block, mark_blocked

    for src in iframe_urls:
        src = _to_appfolio_listings_root(src)
        status, html = await _fetch_with_status(page, src)
        if is_bot_block(status):
            mark_blocked(ctx, "appfolio_embed", src, status)
        if not html:
            continue
        units = parse_appfolio_listings_ssr(html, src)
        if units:
            return units
    return []
