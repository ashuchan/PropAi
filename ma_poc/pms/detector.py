"""
Offline PMS detector.

Research log
------------
Web sources consulted:
  - https://www.realpage.com/products/onesite (verified: onlineleasing.realpage.com is the hosted-portal subdomain pattern; URL segment "#k=" is the apartment-key fragment, accessed 2026-04-17)
  - https://www.yardi.com/products/rentcafe/ (verified: RentCafe is Yardi's marketing portal; legacy vanity sites backed by RentCafe historically use .aspx paths, accessed 2026-04-17)
  - https://www.entrata.com/resident-experience (verified: Entrata property sites expose /Apartments/module/ and ``commoncf.entrata.com`` widget endpoints, accessed 2026-04-17)
  - https://www.appfolio.com/property-manager/online-leasing (verified: listing URLs use ``{slug}.appfolio.com/listings/...``, accessed 2026-04-17)
  - https://www.avaloncommunities.com (single-REIT custom stack; confirmed from raw captures)
Real payloads inspected (under data/runs/2026-04-15/raw_api/):
  - 254976 (San Artes, Mark-Taylor mgmt) — vanity domain, no direct PMS host captured; supports mgmt-prior fallback
  - 12060, 238166, 239499, 256856, 260116, 26617, 268836, 282036, 283726, 35593, 93977 — sightmap.com captures observed
  - 238997 — avaloncommunities.com captures
  - 293707 — api.ws.realpage.com captures (RealPage OLL API endpoint)
OneSite URL samples (from property_reports/):
  - https://9216254.onlineleasing.realpage.com/
  - https://8756399.onlineleasing.realpage.com/#k=44781
Key findings:
  - The property CSV contains only vanity domains — no direct PMS URLs. Offline
    detection from URL alone is therefore weak for most rows; mgmt-company
    priors and HTML markers are load-bearing.
  - OneSite numeric subdomain is a strong fingerprint where present; 3+ distinct
    IDs (8756399, 9216254, and the generic handoff-doc example) validate the
    pattern ``^(?P<id>\\d{3,9})\\.onlineleasing\\.realpage\\.com``.
  - ``commoncf.entrata.com`` is an Entrata widget-CDN host — its presence in
    intercepted API bodies is a strong Entrata signal, but that's a Phase 3
    (adapter) concern, not offline detection.
  - RentCafe vanity URLs historically expose ``.aspx`` paths. We treat this as
    a 0.70-confidence heuristic, not a definitive match.
"""

from __future__ import annotations

import re
import typing as t
import urllib.parse
from dataclasses import dataclass, field
from typing import Literal

PmsName = Literal[
    "rentcafe",
    "entrata",
    "appfolio",
    "onesite",
    "sightmap",
    "realpage_oll",
    "avalonbay",
    "amli",
    "funnel",
    "touchtour",
    "spherexx",
    "knock",
    "g5",
    "resman",
    "apts247",
    "repli360",
    "essex",
    "maac",
    "irvine",
    "cortland",
    "equity",
    "rentmanager",
    "rentvision",
    "residentservices365",
    "encoreskyline_template",
    "aspensquare",
    "squarespace_nopms",
    "wix_nopms",
    "custom",
    "unknown",
]

Strategy = Literal[
    "api_first",
    "jsonld_first",
    "dom_first",
    "portal_hop",
    "syndication_only",
    "cascade",
]

_STRATEGY_BY_PMS: dict[str, Strategy] = {
    "rentcafe": "jsonld_first",
    "entrata": "api_first",
    "appfolio": "api_first",
    "onesite": "api_first",
    "sightmap": "api_first",
    "realpage_oll": "portal_hop",
    "avalonbay": "api_first",
    "amli": "api_first",
    "funnel": "api_first",
    "touchtour": "cascade",
    "spherexx": "api_first",
    "knock": "api_first",
    "g5": "api_first",
    "resman": "api_first",
    "apts247": "api_first",
    "repli360": "api_first",
    "essex": "api_first",
    "maac": "api_first",
    "irvine": "api_first",
    "cortland": "api_first",
    "equity": "api_first",
    "rentmanager": "api_first",
    "rentvision": "dom_first",
    "residentservices365": "dom_first",
    "encoreskyline_template": "dom_first",
    "aspensquare": "dom_first",
    "squarespace_nopms": "syndication_only",
    "wix_nopms": "syndication_only",
    "custom": "cascade",
    "unknown": "cascade",
}

# Management-company → typical PMS priors. Lowercase keys; matched with strip.
# Sources: CLAUDE.md + claude_refactor.md handoff notes. Each entry's rationale
# is in the trailing comment so future maintainers can see provenance.
MGMT_TO_PMS_PRIOR: dict[str, PmsName] = {
    "mark-taylor": "entrata",  # Handoff: Mark-Taylor is an Entrata only shop
    "mark taylor": "entrata",  # Same, alt spelling
    "lindsey management": "rentcafe",  # Handoff: Lindsey is Yardi/RentCafe
    "avalonbay communities": "avalonbay",  # AvalonBay properties use the REIT's custom stack
    # 2026-04-20: live-fetch of windsorcommunities.com showed Apply buttons
    # pointing at nestiolistings.com/api/v2/onlineleasing-link — Windsor runs
    # on Funnel (Nestio), not RentCafe, despite mgmt priors suggesting Yardi.
    "windsor communities": "funnel",
    "windsor property management": "funnel",
    # Ovation Property Management operates the 3 Vegas properties (Summer
    # Winds / Madera / Positano) on *.mytouchtour.com — DOM-only TouchTour.
    "ovation property management": "touchtour",
}

# Host-suffix patterns that are definitive. First match wins.
_HOST_FINGERPRINTS: list[tuple[re.Pattern[str], PmsName, float, str]] = [
    (
        re.compile(r"^\d{3,9}\.onlineleasing\.realpage\.com$"),
        "onesite",
        0.95,
        "host matches OneSite numeric-prefix pattern",
    ),
    (re.compile(r"(?:^|\.)rentcafe\.com$"), "rentcafe", 0.95, "host ends in rentcafe.com"),
    (re.compile(r"(?:^|\.)sightmap\.com$"), "sightmap", 0.95, "host ends in sightmap.com"),
    (re.compile(r"(?:^|\.)avaloncommunities\.com$"), "avalonbay", 0.95, "host ends in avaloncommunities.com"),
    # AMLI must beat entrata/sightmap — AMLI HTML embeds Entrata's
    # commoncf widget URLs and SightMap iframe markers in its lease
    # portal section. Without this guard the detector mis-routes to
    # those adapters, which return empty, and falls into the LLM
    # cascade. Single REIT, single host, definitive match.
    (
        re.compile(r"(?:^|\.)amli\.com$"),
        "amli",
        0.95,
        "host ends in amli.com (AMLI Residential)",
    ),
    # Essex Property Trust — single REIT, single host. Next.js/Vercel
    # marketing app; per-unit data via the same-origin
    # /api/properties/{id}/units/{id}/availability JSON (browser-
    # intercept, Vercel-bot-gated). Detector-tagged "rentcafe" before
    # this, but the public securecafe portal is login-only so it fell to
    # LLM/no_body_short_circuit in prod (0 Tier-1).
    (
        re.compile(r"(?:^|\.)essexapartmenthomes\.com$"),
        "essex",
        0.95,
        "host ends in essexapartmenthomes.com (Essex Property Trust)",
    ),
    # MAAC (Mid-America Apartment Communities) — single REIT, single
    # host. Next.js app; per-unit rent via public same-origin
    # /api/properties/{ULID}/units/available/ (ULID = propertyIntegrationID
    # in community HTML). Detector-tagged generic before this -> floorplan
    # rows with rent=null in prod (0 genuine Tier-1).
    (
        re.compile(r"(?:^|\.)maac\.com$"),
        "maac",
        0.95,
        "host ends in maac.com (Mid-America Apartment Communities)",
    ),
    (
        re.compile(r"(?:^|\.)irvinecompanyapartments\.com$"),
        "irvine",
        0.95,
        "host ends in irvinecompanyapartments.com (Irvine Company)",
    ),
    (
        re.compile(r"(?:^|\.)cortland\.com$"),
        "cortland",
        0.95,
        "host ends in cortland.com (Cortland)",
    ),
    (
        re.compile(r"(?:^|\.)equityapartments\.com$"),
        "equity",
        0.95,
        "host ends in equityapartments.com (Equity Residential)",
    ),
    (re.compile(r"(?:^|\.)entrata\.com$"), "entrata", 0.95, "host ends in entrata.com"),
    (re.compile(r"(?:^|\.)appfolio\.com$"), "appfolio", 0.95, "host ends in appfolio.com"),
    # RealPage portals that are NOT the OneSite OLL subdomain shape — e.g.
    # portal.realpage.com, api.ws.realpage.com. Lower confidence because the
    # RealPage domain covers multiple products.
    (
        re.compile(r"(?:^|\.)realpage\.com$"),
        "realpage_oll",
        0.80,
        "host ends in realpage.com (non-OneSite RealPage product)",
    ),
    # Funnel / Nestio — the listings API always serves from nestiolistings.com
    # regardless of customer marketing domain.
    (
        re.compile(r"(?:^|\.)nestiolistings\.com$"),
        "funnel",
        0.95,
        "host ends in nestiolistings.com (Funnel/Nestio listings API)",
    ),
    # TouchTour / Ovation — proprietary multifamily platform. Customer
    # domains look like ``<market>.mytouchtour.com`` or ``liveovation.com``
    # (the Ovation parent portfolio site). liveovation.com is lower-confidence
    # because it's the parent brand portal, which may link out to multiple
    # PMSes depending on the property.
    (
        re.compile(r"(?:^|\.)mytouchtour\.com$"),
        "touchtour",
        0.95,
        "host ends in mytouchtour.com (TouchTour platform)",
    ),
    (
        re.compile(r"(?:^|\.)liveovation\.com$"),
        "touchtour",
        0.85,
        "host ends in liveovation.com (Ovation parent portfolio)",
    ),
    # Aspen Square Management — multi-property operator on its own
    # Strapi CMS at aspensquare.com. Community pages at
    # /apartments/{st}/{city}/{community}/; unit roster at
    # /floor-plans/{plan-slug}/ on the same domain.
    (
        re.compile(r"(?:^|\.)aspensquare\.com$"),
        "aspensquare",
        0.95,
        "host ends in aspensquare.com (Aspen Square Management operator)",
    ),
    # Apts247 / RentDynamics — the JS widget + media assets serve from
    # apts247.info. The data API itself is same-origin on the property's
    # own domain, so this host match only fires when a captured request
    # hit the apts247 CDN; the HTML marker (below) is the primary path.
    (
        re.compile(r"(?:^|\.)apts247\.info$"),
        "apts247",
        0.95,
        "host ends in apts247.info (Apts247/RentDynamics)",
    ),
]

# Entrata's tenant login form lives at /Apartments/module/application_authentication/
# and is linked from many vanity marketing sites that have no real Entrata
# integration. Match any /Apartments/module/<widget>/ EXCEPT that auth path.
# Negative lookahead lets the auth subpath fall through to other detection.
_ENTRATA_REAL_MODULE_RE = re.compile(
    r"/apartments/module/(?!application_authentication\b)[a-z][a-z0-9_]*/",
    re.IGNORECASE,
)

_ONESITE_CLIENT_ID_RE = re.compile(r"^(?P<id>\d{3,9})\.onlineleasing\.realpage\.com$")
_APPFOLIO_CLIENT_ID_RE = re.compile(r"^(?P<id>[a-z0-9-]+)\.appfolio\.com$")
_RENTCAFE_CLIENT_ID_RE = re.compile(r"^(?P<id>[a-z0-9-]+)\.rentcafe\.com$")
# Entrata property IDs are embedded in the URL path (handoff: entrata.py:1480)
_ENTRATA_PATH_ID_RE = re.compile(r"/(?P<id>\d{3,8})(?:/|$)")

# Fix 5: URL-embedded account-id patterns for 5 PMS platforms
_RENTCAFE_PROPERTY_ID_RE = re.compile(r"propertyId(?:\[\])?=(\d+)", re.IGNORECASE)
_ENTRATA_PROPERTY_ID_RE = re.compile(r"property\[id\]=(\d+)|/Apartments/(\d+)/", re.IGNORECASE)
_SIGHTMAP_CLIENT_KEY_RE = re.compile(r"sightmap\.com/app/api/v1/([a-z0-9_-]+)/", re.IGNORECASE)
_APPFOLIO_SUBDOMAIN_RE = re.compile(r"https?://([a-z0-9-]+)\.appfolio\.com", re.IGNORECASE)
_AVALONBAY_SLUG_RE = re.compile(r"avaloncommunities\.com/[a-z]{2}/[^/]+/([a-z0-9-]+)", re.IGNORECASE)


def _client_account_id_from_url(url: str, pms: str) -> str | None:
    """Extract a stable client/property account ID from a URL for 5 PMS platforms.

    Used when host-based extraction (_client_id_for) can't extract an ID —
    e.g. when the PMS was detected via HTML markers rather than the primary host.
    Returns None if no stable ID is found.
    """
    if not url:
        return None
    if pms == "rentcafe":
        m = _RENTCAFE_PROPERTY_ID_RE.search(url)
        return m.group(1) if m else None
    if pms == "entrata":
        m = _ENTRATA_PROPERTY_ID_RE.search(url)
        if m:
            return m.group(1) or m.group(2)
        return None
    if pms == "sightmap":
        m = _SIGHTMAP_CLIENT_KEY_RE.search(url)
        return m.group(1) if m else None
    if pms == "appfolio":
        m = _APPFOLIO_SUBDOMAIN_RE.search(url)
        if m and m.group(1) not in {"www", "", "app"}:
            return m.group(1)
        return None
    if pms == "avalonbay":
        m = _AVALONBAY_SLUG_RE.search(url)
        return m.group(1) if m else None
    return None


@dataclass
class DetectedPMS:
    pms: PmsName
    confidence: float
    evidence: list[str] = field(default_factory=list)
    pms_client_account_id: str | None = None
    recommended_strategy: Strategy = "cascade"


def _empty_result(reason: str = "no signal") -> DetectedPMS:
    return DetectedPMS(
        pms="unknown",
        confidence=0.0,
        evidence=[reason],
        pms_client_account_id=None,
        recommended_strategy=_STRATEGY_BY_PMS["unknown"],
    )


def _parse_host(url: str) -> str | None:
    if not isinstance(url, str) or not url.strip():
        return None
    try:
        parsed = urllib.parse.urlparse(url.strip())
    except (ValueError, TypeError):
        return None
    host = (parsed.hostname or "").lower()
    return host or None


_KNOWN_LITERALS: frozenset[str] = frozenset(
    {
        "rentcafe",
        "entrata",
        "appfolio",
        "onesite",
        "sightmap",
        "realpage_oll",
        "avalonbay",
        "squarespace_nopms",
        "wix_nopms",
    }
)


def _lookup_csv_pms_override(csv_row: dict[str, object] | None) -> tuple[PmsName, float, str] | None:
    """CSV may carry an explicit ``pms_platform`` hint. Values that match a
    known literal are trusted at high confidence; values that do not match a
    literal signal a custom platform we lack an adapter for.
    """
    if not csv_row:
        return None
    for key in ("pms_platform", "PMS Platform", "pms"):
        raw = csv_row.get(key)
        if not isinstance(raw, str):
            continue
        normalized = raw.strip().lower()
        if not normalized:
            continue
        if normalized in _KNOWN_LITERALS:
            return t.cast(PmsName, normalized), 0.95, f"csv.pms_platform={normalized!r}"
        return "custom", 0.75, f"csv.pms_platform={normalized!r} (no adapter — custom)"
    return None


def _lookup_mgmt_prior(csv_row: dict[str, object] | None) -> tuple[PmsName, str] | None:
    if not csv_row:
        return None
    for key in ("Management Company", "management_company", "Mgmt Company", "mgmt"):
        raw = csv_row.get(key)
        if not isinstance(raw, str):
            continue
        normalized = raw.strip().lower()
        if not normalized:
            continue
        pms = MGMT_TO_PMS_PRIOR.get(normalized)
        if pms is not None:
            return pms, f"mgmt-prior: {raw.strip()!r} -> {pms}"
    return None


def _client_id_for(pms: PmsName, host: str, path: str) -> str | None:
    if pms == "onesite":
        m = _ONESITE_CLIENT_ID_RE.match(host)
        return m.group("id") if m else None
    if pms == "appfolio":
        m = _APPFOLIO_CLIENT_ID_RE.match(host)
        # Reject generic subdomains that are not client slugs
        if m and m.group("id") not in {"www", "", "app"}:
            return m.group("id")
        return None
    if pms == "rentcafe":
        m = _RENTCAFE_CLIENT_ID_RE.match(host)
        if m and m.group("id") not in {"www", "cdngeneralcf", "resource", "cdn"}:
            return m.group("id")
        return None
    if pms == "entrata":
        m = _ENTRATA_PATH_ID_RE.search(path or "")
        return m.group("id") if m else None
    return None


def _detect_host(url: str) -> tuple[PmsName, float, list[str], str | None] | None:
    host = _parse_host(url)
    if not host:
        return None
    try:
        path = urllib.parse.urlparse(url).path or ""
    except (ValueError, TypeError):
        path = ""
    for pattern, pms, confidence, reason in _HOST_FINGERPRINTS:
        if pattern.search(host):
            return pms, confidence, [f"{reason} ({host})"], _client_id_for(pms, host, path)
    return None


def _detect_url_extension(url: str) -> tuple[PmsName, float, list[str]] | None:
    """``.aspx`` paths on non-Microsoft vanity domains are a weak RentCafe/Yardi signal.

    Confidence is intentionally low (0.40) so that a single corroborating
    HTML marker from ``_detect_html_markers`` is enough to promote the
    verdict to the canonical 0.80 level — while preventing misrouting when
    no HTML evidence is found (e.g. management-portal ASPX apps like
    adaraportal.yottareal.com that have no RentCafe markers in the page).
    """
    try:
        parsed = urllib.parse.urlparse(url)
    except (ValueError, TypeError):
        return None
    path = (parsed.path or "").lower()
    host = (parsed.hostname or "").lower()
    if not path.endswith(".aspx"):
        return None
    # Known non-RentCafe ASPX domains — management portals, SaaS dashboards, etc.
    _ASPX_SKIP_HOSTS: tuple[str, ...] = (
        "microsoft.com", "live.com", "sharepoint.com",
        "yottareal.com",   # multi-tenant management portal
    )
    if any(host.endswith(skip) for skip in _ASPX_SKIP_HOSTS):
        return None
    # Low confidence — must be corroborated by at least one HTML fingerprint
    # from _detect_html_markers to route to RentCafe adapter. Without HTML
    # evidence this falls back to GenericAdapter.
    return "rentcafe", 0.40, [f".aspx path on vanity host ({host}) — weak RentCafe/Yardi heuristic (needs HTML corroboration)"]


def _detect_html_markers(page_html: str) -> tuple[PmsName, float, list[str]] | None:
    h = page_html.lower()
    # F0.3 (2026-05-09): three-pass priority order so a real PMS portal
    # reachable from a Squarespace/Wix marketing shell (e.g. 123taylor.com →
    # ``onlineleasing.realpage.com``) isn't demoted to ``syndication_only``.
    #
    # Pass 1 — STRONG PMS markers: definitive portal/widget evidence that
    # cannot be an incidental CDN asset or marketing link. These beat
    # Wix/Squarespace shells because a real leasing path is the source of
    # truth even when the surrounding marketing site is built on a no-PMS
    # platform.
    if "onlineleasing.realpage.com" in h:
        return "onesite", 0.85, ["OneSite portal marker in HTML (onlineleasing.realpage.com)"]
    # RealPage OLL (Online Leasing) wizard — the "Category-D" cluster
    # (~187 props). Vanity marketing sites hop to ``leasing.realpage.com``
    # / embed an ``rp-leasing-widget`` / link ``<property>/content/apply#k=``
    # whose ``#k=`` key bootstraps the stateful ``RP.Leasing.AppService``
    # appstate session. None of these markers can appear on a real OneSite
    # numeric-subdomain site, so this is checked AFTER the OneSite marker
    # above and does not regress it. Routed to ``realpage_oll`` whose
    # adapter intercepts the OLL.SearchFloorPlan PUT response.
    if (
        "leasing.realpage.com" in h
        or "rp-leasing-widget" in h
        or "rp.leasing.appservice" in h
        or "/content/apply#k=" in h
    ):
        return (
            "realpage_oll",
            0.85,
            ["RealPage OLL wizard marker in HTML (leasing.realpage.com / "
             "rp-leasing-widget / RP.Leasing.AppService / /content/apply#k=)"],
        )
    # Entrata routing. ``/Apartments/module/`` alone is too broad — Entrata
    # exposes a generic tenant login form at
    # ``/Apartments/module/application_authentication/`` that many vanity
    # multifamily marketing sites link to even when their actual unit data
    # lives elsewhere (Jonah Digital / ProspectPortal / etc.). Require a
    # widget-specific subpath OR the dedicated Entrata host markers.
    # 2026-05-13 probe (4 of 4 false-positive sites — Foxchase, Muse ATL,
    # Laurel Crossing, livemuseatl): only path present was
    # application_authentication; no Entrata API ever fires.
    has_entrata_widget_path = bool(
        _ENTRATA_REAL_MODULE_RE.search(h)
    )
    # ``.prospectportal.com`` is Entrata's ProspectPortal product host
    # (definitive Entrata, not an incidental CDN asset). Marketing
    # shells that hop to <sub>.prospectportal.com route to EntrataAdapter
    # whose _probe_prospectportal handles the check_availability surface.
    if (
        has_entrata_widget_path
        or "entrata-widget" in h
        or "commoncf.entrata.com" in h
        or ".prospectportal.com" in h
    ):
        return (
            "entrata",
            0.85,
            [
                "Entrata widget marker in HTML (/Apartments/module/"
                "<widget>/ / entrata-widget / commoncf.entrata.com / "
                ".prospectportal.com)"
            ],
        )
    # AppFolio embedded as an iframe to the tenant subdomain
    # (``{tenant}.appfolio.com/listings``) is a definitive leasing path —
    # commonly embedded one nav-hop deep on a Wix/Squarespace marketing
    # shell. Strong marker so it beats the Wix/Squarespace pass-2 demotion
    # (same rationale as onlineleasing.realpage.com / commoncf.entrata.com
    # above). Bare ``appfolio.com`` stays a pass-3 weak marker.
    if ".appfolio.com/listings" in h:
        return (
            "appfolio",
            0.85,
            ["AppFolio listings-iframe marker in HTML (.appfolio.com/listings)"],
        )
    # G5 (g5marketingcloud) Vue-SPA marketing sites. The g5dxm.com CDN /
    # g5marketingcloud asset hosts and the ``g5-c-{client}`` URN appear
    # site-wide incl. landing, so detection fires before the floor-plans
    # hop. Unit data is in the SPA's Apollo cache (see G5Adapter); these
    # sites carry no other PMS.
    if "g5marketingcloud" in h or "g5dxm.com" in h or "g5-c-" in h:
        return (
            "g5",
            0.85,
            ["G5 marketing-cloud marker in HTML (g5marketingcloud / g5dxm.com / g5-c- URN)"],
        )
    # 365 ResidentServices (Apollo / collapsar theme) — a self-hosted CMS
    # used by a small operator cluster. Asset host fingerprint is stable
    # site-wide (used for plan-card images and theme css), so detection
    # fires on landing before any hop. SSR plan grid at
    # /Marketing/FloorPlans (see Residentservices365Adapter).
    if "365residentservices.com" in h:
        return (
            "residentservices365",
            0.85,
            ["365 ResidentServices marker in HTML (365residentservices.com)"],
        )
    # RentVision is a multifamily marketing-site CMS whose ``/floorplans``
    # page is full plan-level SSR DOM. The platform credit appears
    # site-wide including the landing page, so detection fires before any
    # hop. These sites carry no other PMS.
    if (
        "created by rentvision" in h
        or "powered by rentvision" in h
        or "rentvision.com" in h
    ):
        return (
            "rentvision",
            0.85,
            ["RentVision CMS marker in HTML (created/powered by RentVision / rentvision.com)"],
        )
    # Encoreskyline-template marketing family driven by the Jonah Digital /
    # MeetElise widget. Per-plan /floorplans/{slug}/ pages render real
    # apartment-level rows only after a Check-Availability JS click;
    # adapter handles the per-plan interaction. 2026-05-19 deep probe —
    # encoreskyline.com / geneseepointe.com / highlineaustin.com verified.
    if (
        "jonahwidget" in h
        or "jonahdigital" in h
        or "meetelise" in h
    ):
        return (
            "encoreskyline_template",
            0.85,
            [
                "Jonah Digital / MeetElise widget marker in HTML "
                "(JonahWidget / jonahdigital / meetelise)"
            ],
        )
    if (
        "nestiolistings.com" in h
        or "nestio_" in h
        or "data-nestio-" in h
        or "funnelleasing.com" in h  # catches integrations/apply/bh subdomains
        or "funnel-gen-ai-chat" in h
        or "integrations.nestio.com" in h  # Nestio contact-widget = Funnel
    ):
        return (
            "funnel",
            0.90,
            [
                "Funnel/Nestio marker in HTML "
                "(nestiolistings.com / NESTIO_* / data-nestio-* / "
                "funnelleasing.com / funnel-gen-ai-chat / integrations.nestio.com)"
            ],
        )
    if "mytouchtour.com" in h:
        return "touchtour", 0.85, ["TouchTour portal marker in HTML (mytouchtour.com)"]
    # Spherexx Presentation Software ("Convert") — Leaflet-based interactive
    # building site-map widget. Identified by the ssploader.js script tag
    # and/or window.sspcfg config. Confirmed via 2026-05-13 deep probe of
    # henryonthepark.com — /api/unit returns a 176KB array of structured
    # unit data (ID/Name/Sqft/Bed/Bath/Price/FloorplanID/FloorplanName).
    if (
        "presentation.spherexx.app" in h
        or "ssploader.js" in h
        or "sspcfg" in h
    ):
        return (
            "spherexx",
            0.90,
            ["Spherexx marker in HTML (presentation.spherexx.app / ssploader.js / sspcfg)"],
        )
    # Knock / Doorway — marketing-led PMS widget. Identified by the
    # doorway.knck.io script tag and/or knockDoorway.init() call.
    # Public Doorway API serves full unit data: 2026-05-13 probe of
    # liveatcalista.com returned 53 units (incl. availableOn, price).
    if "doorway.knck.io" in h or "knockdoorway" in h:
        return (
            "knock",
            0.90,
            ["Knock/Doorway marker in HTML (doorway.knck.io / knockDoorway)"],
        )
    # G5 Marketing Cloud — multifamily CMS used by Morgan Properties, Aimco,
    # Bell Partners, ZRS, JMG, BH. Identified by ``inventory.g5marketingcloud``
    # (the GraphQL endpoint) or the ``g5-cl-...`` URN slug referenced in
    # asset CDN paths. 2026-05-13 probe of kings-manor-apartments returned
    # 21 unit-level records (incl. availabilityDate, prices).
    # ``g5dxm.com`` (themes.g5dxm.com, widgets.g5dxm.com) is the same
    # vendor's theme + widget CDN — observed on 173 properties in the
    # 2026-05-13 survey, many at Tier 3/4 today.
    if (
        "inventory.g5marketingcloud" in h
        or "g5marketingcloud" in h
        or "g5-cl-" in h
        or "g5dxm.com" in h
        or "dnn506yrbagrg.cloudfront.net" in h
    ):
        # ``dnn506yrbagrg.cloudfront.net`` is G5's primary CDN — per teammate
        # 2026-05-13 deep-dive (C1.G5/RealPage SSR sub-class), 73 of 244 generic
        # Tier-1-API failures had this CDN as their script source but no
        # G5/RealPage fingerprint fired because the detector didn't recognise
        # it. Adding it routes those sites to the G5 GraphQL adapter (which
        # then probes ``inventory.g5marketingcloud.com`` via the locationUrn
        # extracted from CDN paths).
        return (
            "g5",
            0.90,
            ["G5 Marketing Cloud marker in HTML "
             "(inventory.g5marketingcloud / g5-cl- / g5dxm.com / dnn506yrbagrg.cloudfront.net)"],
        )

    # securecafe online-leasing portal — definitive RentCafe/Yardi. A
    # ``<sub>.securecafe.com/onlineleasing/`` reference is the resident-
    # facing leasing portal and is never an incidental CDN asset. The
    # detector's host regex only catches ``*.rentcafe.com`` hosts, so
    # marketing sites that link/iframe to their securecafe portal
    # (2026-05-17 canary: liveatcivicsquare.com and a large slice of the
    # "other" no-fingerprint pool) were misrouted to generic/LLM. Routing
    # to ``rentcafe`` engages the iter-4 securecafe availableunits.aspx
    # unit-level probe.
    if "securecafe.com/onlineleasing" in h or ".securecafe.com" in h:
        return (
            "rentcafe",
            0.90,
            ["RentCafe securecafe online-leasing portal marker in HTML "
             "(<sub>.securecafe.com/onlineleasing)"],
        )

    # ResMan — public availability portal at <client>.myresman.com/Portal/
    # Applicants/Availability?a=&p=, linked from the marketing /floorplans/
    # page. 2026-05-17 canary 842-pool deep-probe: 67+ sites the detector
    # missed (fell to LLM/floorplan). Portal is NOT Cloudflare-fronted, so
    # the ResMan adapter recovers Tier-1 unit-level even proxy-less.
    if "myresman.com" in h or "/portal/applicants/availability" in h:
        return (
            "resman",
            0.90,
            ["ResMan marker in HTML "
             "(myresman.com / Portal/Applicants/Availability)"],
        )

    # Apts247 / RentDynamics — JS leasing widget (static2.apts247.info /
    # media.apts247.info) with a SAME-ORIGIN /api/v1/floorplans/?api_key=
    # data API. 2026-05-17 canary Chrome-MCP no-signature deep-probe: a
    # ≥4-site cluster invisible to static curl_cffi (inventory is fetched
    # client-side). api_key is in the homepage HTML ⇒ deterministic Tier-1.
    if "apts247" in h or "rentdynamics.com" in h:
        return (
            "apts247",
            0.90,
            ["Apts247/RentDynamics marker in HTML "
             "(apts247.info widget / same-origin /api/v1/ data API)"],
        )

    # Repli360 / rrac popup family (royce-like caf_v2 cluster — 158 sites
    # across the 5K, ~0 real units in prod, falls to TIER_4_LLM floorplan-
    # level). The JS-rendered "View Details" anchors call
    # ``getUnitListByFloor(this,'<fp>',<tt>,<site_id>)`` against the
    # no-auth POST app.repli360.com/admin/getUnitListByFloor data API.
    # Markers appear post-render (the detector re-runs with page_html).
    if (
        "app.repli360.com" in h
        or "getunitlistbyfloor(" in h
        or "rrac_listavailableunit" in h
    ):
        return (
            "repli360",
            0.90,
            ["Repli360/rrac marker in HTML "
             "(app.repli360.com / getUnitListByFloor / rrac_listAvailableUnit)"],
        )

    # RentManager / iLoveLeasing — the marketing shell embeds the JS-only
    # iLoveLeasing widget (www.iloveleasing.com/pub/widget/js/luv.js) but
    # the real unit feed is the no-auth, no-bot-wall server-side
    # ``<eid>.ua.rentmanager.com/Search_Result`` endpoint (the full URL is
    # usually present verbatim in the static HTML). ``<eid>.twa/owa
    # .rentmanager.com`` and ``cdn.rentmanager.com`` are the same vendor's
    # tenant-web-access / CDN hosts. 2026-05-18 server-side curl verified
    # (high.ua.rentmanager.com → unit 2600 @ $4,971). Routed to
    # RentManagerAdapter whose extract() probes Search_Result.
    if (
        ".ua.rentmanager.com" in h
        or ".twa.rentmanager.com" in h
        or "cdn.rentmanager.com" in h
        or "iloveleasing.com" in h
    ):
        return (
            "rentmanager",
            0.90,
            ["RentManager/iLoveLeasing marker in HTML "
             "(.ua.rentmanager.com / .twa.rentmanager.com / "
             "cdn.rentmanager.com / iloveleasing.com)"],
        )

    # Pass 2 — Wix/Squarespace platform giveaway scripts. These are strong
    # "not-a-PMS" signals when no strong PMS marker appeared in pass 1.
    if "static.parastorage.com" in h or "wix.com" in h:
        return "wix_nopms", 0.85, ["Wix script/platform marker in HTML"]
    if "squarespace.com" in h:
        return "squarespace_nopms", 0.85, ["Squarespace script/platform marker in HTML"]

    # Pass 3 — WEAK PMS markers: substrings that could be incidental CDN
    # asset references or marketing links rather than actual portal
    # endpoints. Run after Wix/Squarespace so a ``cdngeneralcf.rentcafe.com``
    # image on a Squarespace site doesn't misroute to RentCafe — see
    # ``test_no_rentcafe_false_positive_on_squarespace_with_cdn_asset``.
    if "entrata.com" in h:
        return "entrata", 0.80, ["Entrata marker in HTML (entrata.com)"]
    if "rentcafe" in h or "yardi" in h:
        return "rentcafe", 0.80, ["RentCafe/Yardi marker in HTML"]
    if "sightmap.com" in h:
        return "sightmap", 0.80, ["SightMap iframe/script marker in HTML"]
    if ".appfolio.com" in h:
        return "appfolio", 0.80, ["AppFolio marker in HTML"]
    if "liveovation.com" in h:
        return "touchtour", 0.80, ["Ovation portfolio marker in HTML (liveovation.com)"]
    return None


# Signal helpers for DETECTOR_SIGNALS telemetry ---------------------------

_SCRIPT_SRC_RE = re.compile(r"<script[^>]+src=[\"']([^\"']+)[\"']", re.IGNORECASE)
_IFRAME_SRC_RE = re.compile(r"<iframe[^>]+src=[\"']([^\"']+)[\"']", re.IGNORECASE)
_META_GEN_RE = re.compile(
    r"<meta[^>]+name=[\"']generator[\"'][^>]+content=[\"']([^\"']+)[\"']",
    re.IGNORECASE,
)
_META_APP_RE = re.compile(
    r"<meta[^>]+name=[\"']application-name[\"'][^>]+content=[\"']([^\"']+)[\"']",
    re.IGNORECASE,
)

# Fingerprints we check against HTML, keyed by PMS/platform label. The booleans
# returned in ``fingerprints_matched`` correspond 1-1 to the keys here.
#
# The ``marketing_*`` labels below are NOT PMS platforms — they are
# marketing-stack / lead-capture vendors (Knock, Hyly, Market Apartments) that
# commonly sit on top of vanity domains. Detection returning these signals the
# report that the site is a custom CMS + external lead-gen; the data still
# lives in the HTML (or behind a portal link) so the extraction path is the
# GenericAdapter HTML/LLM cascade — but knowing the stack is present means
# future work can add dedicated portal resolvers for these vendors.
_HTML_FINGERPRINTS: dict[str, tuple[str, ...]] = {
    "entrata": ("entrata.com", "/apartments/module/", "entrata-widget"),
    "rentcafe": ("rentcafe", "yardi"),
    "sightmap": ("sightmap.com",),
    "appfolio": (".appfolio.com",),
    "onesite": ("onlineleasing.realpage.com",),
    "wix": ("static.parastorage.com", "wix.com"),
    "squarespace": ("squarespace.com",),
    "realpage": ("api.ws.realpage.com", "realpage.com"),
    "realpage_oll": (
        "leasing.realpage.com",
        "rp-leasing-widget",
        "rp.leasing.appservice",
        "/content/apply#k=",
    ),
    "avalonbay": ("avaloncommunities.com",),
    "amli": ("amli.com",),
    "funnel": ("nestiolistings.com", "nestio_", "data-nestio-"),
    "g5": ("g5marketingcloud", "g5dxm.com", "g5-c-"),
    "rentvision": ("created by rentvision", "powered by rentvision", "rentvision.com"),
    "residentservices365": ("365residentservices.com",),
    "encoreskyline_template": ("jonahwidget", "jonahdigital", "meetelise"),
    "aspensquare": ("aspensquare.com", "static.aspensquare.com"),
    "touchtour": ("mytouchtour.com", "liveovation.com"),
    "spherexx": ("presentation.spherexx.app", "ssploader.js", "sspcfg"),
    "rentmanager": (
        ".ua.rentmanager.com",
        ".twa.rentmanager.com",
        "cdn.rentmanager.com",
        "iloveleasing.com",
    ),
    # Marketing / lead-capture stacks — observed in 10-property roll-up
    # (doorway.knck.io, cdn-media.hy.ly, chat.hyly.ai, marketapts.com).
    "marketing_knock": ("doorway.knck.io", "knockrentals.com"),
    "marketing_hyly": ("hy.ly", "hyly.ai"),
    "marketing_marketapts": ("marketapts.com",),
}


def _unique_hosts(urls: list[str], limit: int = 10) -> list[str]:
    """Return up to ``limit`` unique hosts (deduped, in order)."""
    seen: set[str] = set()
    hosts: list[str] = []
    for u in urls:
        try:
            h = urllib.parse.urlparse(u).hostname or u
        except Exception:
            h = u
        if h and h not in seen:
            seen.add(h)
            hosts.append(h)
        if len(hosts) >= limit:
            break
    return hosts


def collect_detector_signals(
    url: str,
    csv_row: dict[str, object] | None,
    page_html: str | None,
) -> dict[str, t.Any]:
    """Collect the raw signals ``detect_pms`` examines, without classifying.

    Returned dict is emitted as ``EventKind.DETECTOR_SIGNALS`` and rendered in
    the per-property report so when detection fails we can see *what* the
    detector saw — script hosts, iframes, meta tags, matched fingerprints,
    and whether the CSV mgmt-company prior fired.
    """
    try:
        parsed = urllib.parse.urlparse(url or "")
    except Exception:
        parsed = urllib.parse.urlparse("")
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").lower()
    aspx_detected = path.endswith(".aspx") and not host.endswith(
        (
            "microsoft.com",
            "live.com",
            "sharepoint.com",
        )
    )

    script_srcs: list[str] = []
    iframe_srcs: list[str] = []
    meta_generator: str | None = None
    meta_application_name: str | None = None
    body_bytes = 0
    fingerprints_matched: list[str] = []

    if isinstance(page_html, str) and page_html:
        body_bytes = len(page_html.encode("utf-8", errors="ignore"))
        h = page_html.lower()
        for label, needles in _HTML_FINGERPRINTS.items():
            if any(n in h for n in needles):
                fingerprints_matched.append(label)
        script_srcs = _SCRIPT_SRC_RE.findall(page_html)
        iframe_srcs = _IFRAME_SRC_RE.findall(page_html)
        m = _META_GEN_RE.search(page_html)
        if m:
            meta_generator = m.group(1)[:120]
        m = _META_APP_RE.search(page_html)
        if m:
            meta_application_name = m.group(1)[:120]

    mgmt_raw: str | None = None
    mgmt_prior_matched = False
    if csv_row:
        for key in ("Management Company", "management_company", "Mgmt Company", "mgmt"):
            raw = csv_row.get(key)
            if isinstance(raw, str) and raw.strip():
                mgmt_raw = raw.strip()
                if mgmt_raw.lower() in MGMT_TO_PMS_PRIOR:
                    mgmt_prior_matched = True
                break

    return {
        "url_host": host or None,
        "url_path": path or None,
        "aspx_detected": aspx_detected,
        "body_bytes": body_bytes,
        "fingerprints_checked": list(_HTML_FINGERPRINTS.keys()),
        "fingerprints_matched": fingerprints_matched,
        "script_srcs_sample": _unique_hosts(script_srcs, limit=10),
        "iframe_srcs_sample": _unique_hosts(iframe_srcs, limit=5),
        "meta_generator": meta_generator,
        "meta_application_name": meta_application_name,
        "csv_mgmt_company": mgmt_raw,
        "csv_mgmt_prior_matched": mgmt_prior_matched,
    }


def confirm_detection(
    initial: DetectedPMS,
    api_responses: list[dict[str, t.Any]] | None,
) -> DetectedPMS:
    """After page load, verify the URL-based detection against captured bodies.

    Demotion rule (P3 — innocent until proven guilty): downgrade
    ``initial.pms`` to ``unknown`` **only when there is positive evidence
    that the URL detection was wrong**. Absence of confirmation is not
    disconfirmation.

    Algorithm:

      1. If ``initial.pms == 'unknown'``: no-op (nothing to demote to).
      2. If ``api_responses`` is empty: preserve. F0.2 from 2026-05-09 —
         change-detector GETs, blocked pages, and timed-out fetches all
         produce zero captures, which would otherwise demote every detected
         PMS and starve real portals.
      3. If the initial adapter exposes ``matches_response_body``:
         Phase 1 — iterate responses; if any body matches the initial
         adapter's shape, preserve immediately. Phase 2 — if no body
         matched the initial adapter, iterate all *other* registered
         adapters' body-shape checkers; if any captured body positively
         matches a different adapter, that is the negative evidence
         needed to demote. The evidence string names the specific
         cross-match for operator triage.
      4. If the initial adapter doesn't expose ``matches_response_body``
         (DOM-only parsers like TouchTour): preserve — there's no body
         shape to assert against.

    This rule resolves the tension between two historical failure modes:

      * eb18889 (2026-04-20) — Windsor/Mark-Taylor/Vegas misrouting:
        URL said RentCafe, captured bodies were positively Funnel-shaped.
        Phase 2 still catches this case via the cross-match. ✓
      * Bug C (2026-05-11) — 558 RentCafe-fingerprinted properties had
        all captures be third-party noise (CDN/analytics/captcha). Phase
        2 finds no positive cross-match → preserves. ✓

    See ``docs/2026_05_11_regressions_fix_design.md`` (Bug C / P3) for the
    full design history.
    """
    # "unknown" is already the fallthrough — nothing to demote to.
    if initial.pms == "unknown":
        return initial

    responses = api_responses or []
    if not responses:
        # F0.2: absence of captures ≠ disconfirmation. Preserve URL detection.
        return initial

    # Local import to avoid a circular adapters→detector→registry dep at
    # import time. Same pattern was already in use for ``get_adapter``;
    # ``all_adapters`` joins it for the Phase 2 cross-match scan.
    try:
        from ma_poc.pms.adapters.registry import all_adapters, get_adapter
    except Exception:
        return initial

    try:
        adapter = get_adapter(initial.pms)
    except Exception:
        return initial

    checker = getattr(adapter, "matches_response_body", None)
    if checker is None or not callable(checker):
        # Adapter didn't opt in — preserve URL-based detection.
        return initial

    # ── Phase 1: does any body match the initial adapter's shape? ───────────
    for resp in responses:
        body = resp.get("body") if isinstance(resp, dict) else None
        try:
            if checker(body):
                return initial
        except Exception:
            # A misbehaving checker must not demote or crash the pipeline.
            continue

    # ── Phase 2 (Bug C — P3): require positive cross-match to demote ────────
    # No body matched the initial adapter. That alone is not evidence the
    # URL detection was wrong — bodies may just be noise (analytics, CDN,
    # captcha). Demote only when a captured body positively matches a
    # *different* adapter's body-shape predicate.
    try:
        other_adapters = all_adapters()
    except Exception:
        # If the registry can't enumerate, fail-safe: preserve.
        return initial

    cross_match: tuple[str, int] | None = None  # (matched_pms_name, response_idx)
    for other in other_adapters:
        other_name = getattr(other, "pms_name", "")
        # Skip the initial adapter (already checked in Phase 1) and the
        # generic fallback (it has no body-shape and accepts anything).
        if not other_name or other_name == initial.pms or other_name == "generic":
            continue
        other_checker = getattr(other, "matches_response_body", None)
        if not callable(other_checker):
            continue
        for idx, resp in enumerate(responses):
            body = resp.get("body") if isinstance(resp, dict) else None
            try:
                if other_checker(body):
                    cross_match = (other_name, idx)
                    break
            except Exception:
                continue
        if cross_match is not None:
            break

    if cross_match is None:
        # No positive cross-match — bodies are noise, not evidence. P3 says
        # preserve. This is the 2026-05-11 Bug C recovery path.
        return initial

    # Positive cross-match — URL detection contradicted by a body that
    # belongs to a known different PMS. Demote with the specific match
    # named so operators can triage "why did this property route to
    # generic?" via the evidence trail rather than the responses list.
    matched_pms, matched_idx = cross_match
    return DetectedPMS(
        pms="unknown",
        confidence=0.0,
        evidence=list(initial.evidence)
        + [
            f"demoted_from_{initial.pms}:"
            f"response_{matched_idx}_matches_{matched_pms}_body_shape"
        ],
        pms_client_account_id=None,
        recommended_strategy=_STRATEGY_BY_PMS["unknown"],
    )


def detect_pms(
    url: str,
    csv_row: dict[str, object] | None = None,
    page_html: str | None = None,
) -> DetectedPMS:
    """Detect the PMS backing a property URL using offline signals only.

    Signal priority (first confident hit wins):
      1. URL host fingerprints
      2. URL extension heuristic (``.aspx``)
      3. HTML platform-giveaway scripts + PMS-specific markers
      4. CSV management-company priors

    Never raises; bad inputs return ``DetectedPMS(pms="unknown", confidence=0.0)``.
    """
    try:
        return _detect_pms_impl(url, csv_row, page_html)
    except Exception as exc:  # defensive: detector must never crash the pipeline
        return DetectedPMS(
            pms="unknown",
            confidence=0.0,
            evidence=[f"detector-internal-error: {type(exc).__name__}"],
            recommended_strategy=_STRATEGY_BY_PMS["unknown"],
        )


def _detect_pms_impl(
    url: str,
    csv_row: dict[str, object] | None,
    page_html: str | None,
) -> DetectedPMS:
    evidence: list[str] = []

    # 0. Explicit CSV override (highest-trust signal — the human told us)
    override = _lookup_csv_pms_override(csv_row)
    if override is not None:
        pms, conf, reason = override
        return DetectedPMS(
            pms=pms,
            confidence=conf,
            evidence=[reason],
            recommended_strategy=_STRATEGY_BY_PMS[pms],
        )

    host_hit = _detect_host(url) if isinstance(url, str) else None
    if host_hit is not None:
        pms, confidence, host_evidence, client_id = host_hit
        return DetectedPMS(
            pms=pms,
            confidence=confidence,
            evidence=host_evidence,
            pms_client_account_id=client_id,
            recommended_strategy=_STRATEGY_BY_PMS[pms],
        )

    # 2. URL extension heuristic
    ext_hit = _detect_url_extension(url) if isinstance(url, str) else None

    # 3. HTML markers (optional input)
    html_hit = _detect_html_markers(page_html) if isinstance(page_html, str) and page_html else None

    # 4. CSV management-company prior
    mgmt_hit = _lookup_mgmt_prior(csv_row)

    # Rank candidates by confidence; combine evidence from all matching signals.
    candidates: list[tuple[PmsName, float, list[str]]] = []
    if html_hit is not None:
        candidates.append(html_hit)
    if ext_hit is not None:
        candidates.append(ext_hit)
    if mgmt_hit is not None:
        pms, reason = mgmt_hit
        candidates.append((pms, 0.70, [reason]))

    if not candidates:
        return _empty_result("no URL/HTML/mgmt signal")

    # Consensus boost: if two independent signals agree, bump confidence.
    pms_votes: dict[PmsName, float] = {}
    pms_evidence: dict[PmsName, list[str]] = {}
    for pms, conf, reasons in candidates:
        pms_votes[pms] = pms_votes.get(pms, 0.0) + conf
        pms_evidence.setdefault(pms, []).extend(reasons)

    best_pms = max(pms_votes, key=lambda k: pms_votes[k])
    # Base confidence is the max single-signal confidence for the winning PMS;
    # two agreeing signals bump toward (but not past) 0.95.
    agreeing = [c for (p, c, _) in candidates if p == best_pms]
    base = max(agreeing)
    combined = min(0.95, base + 0.10 * (len(agreeing) - 1))
    evidence = pms_evidence[best_pms]

    client_id = _client_account_id_from_url(url, best_pms) if isinstance(url, str) else None

    return DetectedPMS(
        pms=best_pms,
        confidence=combined,
        evidence=evidence,
        pms_client_account_id=client_id,
        recommended_strategy=_STRATEGY_BY_PMS[best_pms],
    )
