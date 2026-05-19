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

# RentCafe SecureCafe online-leasing availability page. The host is the
# ``securecafe.com`` PMS subdomain (sometimes ``rentcafe.com`` for the same
# product); the canonical SSR path ends in ``availableunits.aspx``. The
# page renders ``<tr class="AvailUnitRow">`` rows per real apartment.
_RENTCAFE_PORTAL_RE = re.compile(
    r"""https?://[a-z0-9][a-z0-9-]*\.(?:securecafe|rentcafe)\.com"""
    r"""/[^\s"'<>]*availableunits\.aspx[^\s"'<>]*""",
    re.IGNORECASE,
)

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
      /\.(securecafe|rentcafe)\.com\/[^"' <>]*availableunits\.aspx/i.test(s)
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


async def _fetch(page: Page, url: str) -> str:
    """Fetch *url* in-session via ``page.evaluate``. Never raises — '' on failure."""
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
        log.debug("PMS-portal fetch failed url=%s err=%s", url, exc)
        return ""
    return body if isinstance(body, str) else ""


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
    if not callable(evaluate):
        return []

    # 1. Cheap path — portal URL already on the current page.
    candidates: list[str] = []
    try:
        live = await evaluate(_LIVE_PORTAL_SRC_JS)
        if isinstance(live, list):
            candidates = [u for u in live if isinstance(u, str) and u]
    except Exception as exc:
        log.debug("PMS-portal live scan failed err=%s", exc)

    # 2. Probe well-known sub-paths; pull the portal URL out of each body.
    #    First hit wins (probing order is by observed frequency).
    if not candidates:
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
    for src in candidates:
        kind = _classify(src)
        if not kind:
            continue
        html = await _fetch(page, src)
        units = _parse_portal_html(kind, html, src)
        if units:
            return units
    return []
