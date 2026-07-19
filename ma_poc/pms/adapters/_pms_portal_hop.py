"""Shared PMS-portal-hop recovery for floorplan-only extractions.

2026-05-19 deep-probe finding: of the 52 ground-truthed 0%-uid bootstrap79
properties, ~20 have unit-level inventory in a known PMS portal one nav-hop
deep from the marketing site (ResMan ``Portal/Applicants/Availability``,
RentCafe ``SecureCafe`` ``availableunits.aspx``, Entrata ``ProspectPortal``)
but jugnu's marketing-site tiers (TIER_3_DOM / TIER_2_JSONLD / TIER_MERGED)
never follow the portal anchor. This is a pure routing gap — the per-PMS
SSR parsers already exist (``parse_resman_unittypes`` /
``parse_securecafe_availableunits``); they just never get the portal HTML
to chew on.

Mirrors the AppFolio-embed recovery pattern (see ``_appfolio_embed.py``):

  1. Cheap path — scan the live page for a portal URL (iframe ``src`` or
     anchor ``href``).
  2. Probe a tight set of well-known sub-paths under the current origin;
     pull the portal URL out of each body.
  3. Fetch the portal page in-session via ``page.evaluate`` (reuses the
     page's own origin/cookies — same trick the Entrata-endpoint and
     AppFolio-embed probes use), then hand the HTML to the existing
     tier-1 PMS SSR parser. First non-empty wins.

PMS coverage (v1):
  - ResMan:  ``*.myresman.com/Portal/Applicants/Availability?a=…&p=…``
             → ``resman._extract_unittypes`` + ``resman.parse_resman_unittypes``
  - RentCafe SecureCafe: ``*.securecafe.com/.../availableunits.aspx``
             → ``rentcafe.parse_securecafe_availableunits``

Not yet covered (TODO):
  - Entrata ProspectPortal — unit-level data is an XHR fragment
    (``view_unit_spaces``) reachable via a per-floorplan request, not a
    top-level HTML page; needs a more elaborate fetch loop.
  - RealPage Online-Leasing (``onlineleasing.realpage.com``) — workflow JSON
    over multiple POSTs; ``parse_realpage_oll_workflow`` takes a dict, not
    HTML.

Verified-live ground-truth sites (2026-05-19 probe; will succeed once
wired): cobblestonephx (ResMan), mansionsattimberland (ResMan),
liveatemerald (ResMan), mansionsatsunsetridge (ResMan), forge65 (RentCafe),
somersetdm (RentCafe). 9 ResMan + 4 RentCafe of the 23 unit-level-reachable
0%-uid sites.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING
from urllib.parse import urlparse, urlunparse

from ma_poc.pms.adapters.rentcafe import parse_securecafe_availableunits
from ma_poc.pms.adapters.resman import (
    _extract_unittypes as _extract_resman_unittypes,
)
from ma_poc.pms.adapters.resman import (
    parse_resman_unittypes,
)

if TYPE_CHECKING:
    from playwright.async_api import Page

    from ma_poc.pms.adapters.base import AdapterContext

log = logging.getLogger(__name__)

# ResMan availability portal: per-property URL under the property-management
# company's ``*.myresman.com`` tenant subdomain. ``a=`` (account id) and
# ``p=`` (property guid) are required query params; the page embeds
# ``var unitTypes = [...]`` which ``_extract_resman_unittypes`` walks.
_RESMAN_PORTAL_RE = re.compile(
    r"""https?://[a-z0-9][a-z0-9-]*\.myresman\.com/Portal/Applicants/Availability"""
    r"""\?a=\d+&p=[a-f0-9-]+""",
    re.IGNORECASE,
)

# RentCafe SecureCafe online-leasing portal. The host is the
# ``securecafe.com`` PMS subdomain (sometimes ``rentcafe.com`` for the
# same product). Real marketing pages link to ANY of the onlineleasing
# entry points — ``guestlogin.aspx``, ``floorplans.aspx``,
# ``availableunits.aspx`` — depending on the "Apply Now"/"Floor Plans"/
# "Availability" anchor label. Match the slug-root broadly; the recovery
# transforms whatever entry it captured into the canonical
# ``/availableunits.aspx`` path before fetching (the SSR table we parse).
#
# 2026-05-19: broadened from the prior ``availableunits.aspx``-only
# match, which missed sweetwaterfl / parkviewapartmenthomes /
# parkerhouse — all three link to ``floorplans.aspx`` or
# ``guestlogin.aspx`` from their marketing site.
_RENTCAFE_PORTAL_RE = re.compile(
    r"""https?://[a-z0-9][a-z0-9-]*\.(?:securecafe|rentcafe)\.com"""
    r"""/onlineleasing/[a-z0-9_-]+(?:/[a-z0-9_.-]*)?""",
    re.IGNORECASE,
)
# Canonical data-bearing path for the SecureCafe SSR parser.
_RENTCAFE_SLUG_RE = re.compile(
    r"https?://[a-z0-9][a-z0-9-]*\.(?:securecafe|rentcafe)\.com/onlineleasing/([a-z0-9_-]+)",
    re.IGNORECASE,
)


def _to_rentcafe_availableunits(url: str) -> str:
    """Transform any matched SecureCafe onlineleasing URL to the
    ``…/availableunits.aspx`` form that ``parse_securecafe_availableunits``
    expects.
    """
    try:
        p = urlparse(url)
    except Exception:
        return url
    m = _RENTCAFE_SLUG_RE.search(url)
    if not m or not p.scheme or not p.netloc:
        return url
    slug = m.group(1)
    return urlunparse((p.scheme, p.netloc, f"/onlineleasing/{slug}/availableunits.aspx", "", "", ""))

# Sub-paths a marketing site uses for the page that links/embeds the PMS
# portal. Ordered by observed frequency in the 2026-05-19 deep probe.
# Kept tight — every entry was seen on a real failed-case property.
_PORTAL_SUBPATHS: tuple[str, ...] = (
    "/floorplans",
    "/floorplans/",
    "/floor-plans",
    "/floor-plans/",
    "/availability",
    "/apartments",
    "/units",
    "/apply",
    "/lease-online",
    "/lease",
    "/available-rentals",
    "/floorplans.php",            # observed on cobblestonephx
    "/floorplans.aspx",
)

# JS run on the live page: harvest any PMS portal URL already present in
# ``iframe`` ``src`` or ``a`` ``href``. The cheap path — fires when the
# marketing site links/embeds the portal on the page we're already on.
# Note the same-anchor heuristic catches anchors hidden behind onclick
# handlers as long as the canonical ``href`` is set (very common).
_LIVE_PORTAL_SRC_JS = r"""
() => {
  const out = [];
  document.querySelectorAll('iframe, a').forEach((el) => {
    const s = (el.src || el.href || '');
    if (
      /\.myresman\.com\/Portal\/Applicants\/Availability/i.test(s) ||
      /\.(securecafe|rentcafe)\.com\/onlineleasing\/[a-z0-9_-]+/i.test(s)
    ) {
      out.push(s);
    }
  });
  return out;
}
"""


def _origin(page: Page, ctx: AdapterContext) -> str:
    """``scheme://host`` for sub-path probing — prefer the settled page URL."""
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
    """Fetch *url* in-session via ``page.evaluate``. Returns ``(status, body)``.

    Status is 0 on network error / unavailable evaluate. Body is ``''`` on
    any non-2xx. Dict-shape JS is the new wire format; string-shape result
    keeps existing ``evaluate`` mocks (which return body strings keyed by
    URL) working as ``(200, body)`` / ``(0, '')``.
    """
    evaluate = getattr(page, "evaluate", None)
    if not callable(evaluate):
        # page=None (production dispatch): cheap curl_cffi GET fallback so the
        # sub-path portal probing still works. Task #37 Track 1.
        from ma_poc.pms.adapters._probe import probe_fetch_status

        return await probe_fetch_status(url)
    try:
        result = await evaluate(
            "(u) => fetch(u, {credentials: 'include'})"
            ".then(r => r.text().then(b => ({status: r.status, body: r.ok ? b : ''})))"
            ".catch(() => ({status: 0, body: ''}))",
            url,
        )
    except Exception as exc:  # pragma: no cover — network/SDK variance
        log.debug("PMS-portal fetch failed url=%s err=%s", url, exc)
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


def _classify(portal_url: str) -> str:
    """Return ``'resman'`` | ``'rentcafe'`` | ``''`` (unrecognised)."""
    if _RESMAN_PORTAL_RE.search(portal_url):
        return "resman"
    if _RENTCAFE_PORTAL_RE.search(portal_url):
        return "rentcafe"
    return ""


def _parse_portal_html(kind: str, html: str, source_url: str) -> list[dict]:
    """Dispatch to the existing tier-1 PMS SSR parser. Never raises."""
    if not html:
        return []
    try:
        if kind == "resman":
            data = _extract_resman_unittypes(html)
            if not data:
                return []
            return parse_resman_unittypes(data, source_url)
        if kind == "rentcafe":
            return parse_securecafe_availableunits(html, source_url)
    except Exception as exc:  # pragma: no cover — parser internal failure
        log.debug("PMS-portal parse failed kind=%s err=%s", kind, exc)
    return []


async def recover_pms_portal(
    page: Page,
    ctx: AdapterContext,
) -> list[dict]:
    """Find a known PMS portal URL on / one hop from the page, parse it.

    Returns SSR-parsed unit dicts (one row per real apartment), or ``[]``
    when no recognised portal is discoverable (so a genuinely no-portal
    site is unaffected). Never raises.
    """
    evaluate = getattr(page, "evaluate", None)
    # No longer a hard page-gate: under page=None, step 1 scans the already-
    # fetched RENDER body for the portal URL and the sub-path probes fall back
    # to curl_cffi. Task #37 Track 1. Live-page path unchanged when present.
    from ma_poc.pms.adapters._probe import body_html_from_ctx

    # 1. Cheap path — portal URL already on the page (live) or in the body.
    candidates: list[str] = []
    if callable(evaluate):
        try:
            live = await evaluate(_LIVE_PORTAL_SRC_JS)
            if isinstance(live, list):
                candidates = [u for u in live if isinstance(u, str) and u]
        except Exception as exc:
            log.debug("PMS-portal live scan failed err=%s", exc)
    else:
        body = body_html_from_ctx(ctx)
        if body:
            found = _RESMAN_PORTAL_RE.findall(body) + _RENTCAFE_PORTAL_RE.findall(body)
            candidates = list(dict.fromkeys(u for u in found if isinstance(u, str) and u))

    # 2. Probe well-known sub-paths; pull the portal URL out of each body.
    #    First hit wins (probing order is by observed frequency). PAGE-ONLY:
    #    at page=None a blanket curl_cffi probe on every 0-unit property is a
    #    cost/latency risk; the body scan (step 1) covers the in-body case.
    if not candidates and callable(evaluate):
        origin = _origin(page, ctx)
        if origin:
            for path in _PORTAL_SUBPATHS:
                html = await _fetch(page, origin + path)
                if not html:
                    continue
                m = _RESMAN_PORTAL_RE.search(html) or _RENTCAFE_PORTAL_RE.search(
                    html
                )
                if m:
                    candidates = [m.group(0)]
                    break

    # 3. Fetch each portal candidate and parse with its tier-1 parser.
    #    First non-empty parse wins. Walking the list (vs first-only) is
    #    cheap and makes us resilient to a stale anchor pointing at an
    #    expired ``p=<guid>``.
    #    A 401/403/429/503 here is recorded as a bot-block — the most
    #    common cause is SecureCafe ``availableunits.aspx`` DataDome on
    #    bare httpx, which the production stack (residential proxy +
    #    Camoufox + cookie-mint reuse) typically flips to a 200.
    from ma_poc.pms.adapters._universal_recovery import is_bot_block, mark_blocked

    for src in candidates:
        kind = _classify(src)
        if not kind:
            continue
        # For SecureCafe the captured entry can be any onlineleasing URL
        # (guestlogin.aspx / floorplans.aspx / etc.). Canonicalize to the
        # availableunits.aspx form before fetching — that's the SSR
        # table parse_securecafe_availableunits expects.
        fetch_url = _to_rentcafe_availableunits(src) if kind == "rentcafe" else src
        status, html = await _fetch_with_status(page, fetch_url)
        if is_bot_block(status):
            mark_blocked(ctx, f"pms_portal_hop:{kind}", fetch_url, status)
        units = _parse_portal_html(kind, html, fetch_url)
        if units:
            return units
    return []
