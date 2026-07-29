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
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Literal

from ma_poc.config.feature_flags import (
    enable_camden_adapter,
    enable_onsite_apply_adapter,
    enable_venterra_adapter,
)
from ma_poc.pms.appfolio_urls import is_listings_index_url

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
    "reinhold",
    "rentmanager",
    "rentvision",
    "rentaladdress",
    "residentservices365",
    "encoreskyline_template",
    "aspensquare",
    "edificecms",
    "fortresstech",
    "thinkreside",
    "marketapts",
    "rentcafe_unit_roster",
    "imt_spaces",
    "realpage_cws",
    "rentcafe_layout_tab",
    "wix_floor_plans",
    "equity_apartments",
    "generic_plan_text",
    "squarespace_nopms",
    "wix_nopms",
    "onsite_apply",
    "venterra",
    "camden",
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
    "reinhold": "dom_first",
    "rentmanager": "api_first",
    "rentvision": "dom_first",
    "rentaladdress": "dom_first",
    "residentservices365": "dom_first",
    "encoreskyline_template": "dom_first",
    "aspensquare": "dom_first",
    "edificecms": "api_first",
    "fortresstech": "dom_first",
    "thinkreside": "dom_first",
    "marketapts": "dom_first",
    "rentcafe_unit_roster": "dom_first",
    "imt_spaces": "dom_first",
    "realpage_cws": "dom_first",
    "rentcafe_layout_tab": "dom_first",
    "wix_floor_plans": "dom_first",
    "equity_apartments": "dom_first",
    "generic_plan_text": "dom_first",
    "squarespace_nopms": "syndication_only",
    "wix_nopms": "syndication_only",
    "onsite_apply": "portal_hop",
    "venterra": "dom_first",
    "camden": "dom_first",
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
    (
        # OneSite "welcomehome" portal (2026-07-18): siteId lives in the query
        # (property.onesite.realpage.com/welcomehome?siteId=NNN), not the host.
        re.compile(r"^property\.onesite\.realpage\.com$"),
        "onesite",
        0.95,
        "host matches OneSite welcomehome portal",
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
    # Entrata "Prospect Portal" websites are served from
    # ``*.prospectportal.com`` (canonical) — and vanity domains 302 into
    # this host. The floor-plan grid is SSR DOM; the EntrataAdapter has a
    # Prospect Portal DOM fallback for exactly these.
    (
        re.compile(r"(?:^|\.)prospectportal\.com$"),
        "entrata",
        0.9,
        "host ends in prospectportal.com (Entrata Prospect Portal website)",
    ),
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
# A non-www AppFolio tenant subdomain followed by a leasing-system path
# (``{slug}.appfolio.com/listings`` | ``/connect`` | ``/apply``) is a
# definitive signal that AppFolio is the property's leasing backend — as
# opposed to the bare ``appfolio.com`` marketing domain. Used to outrank a
# co-resident marketing widget (apts247 / knock chat) that would otherwise
# win routing at 0.90.
_APPFOLIO_TENANT_PATH_RE = re.compile(
    r"([a-z0-9][a-z0-9-]*)\.appfolio\.com/(?:listings|connect|apply)",
    re.IGNORECASE,
)

# 2026-07-28 — the regex above answers "is AppFolio the leasing backend", and
# that answer is still yes for every path it matches. What it does NOT answer
# is "does this page point at a published inventory surface". ``/connect`` is
# the resident portal (the footer "Pay Rent" / "Resident Login" button that
# AppFolio's own site template ships on every page), ``/apply`` is an
# application link, and ``/listings/showings/new`` etc. are per-unit actions —
# all of them identify the management-company ACCOUNT. Only the listings index
# is an inventory surface, and that question is answered in exactly one place:
# :func:`ma_poc.pms.appfolio_urls.is_listings_index_url`.
#
# Scheme-agnostic because a body carries the reference in several shapes:
# ``https://t.appfolio.com/listings``, protocol-relative ``//t.appfolio.com/
# listings``, JSON-escaped ``https:\/\/t.appfolio.com\/listings``, and bare
# ``t.appfolio.com/listings`` inside an embed config. Matches are canonicalised
# to ``https://<match>`` before being handed to the shared predicate so the
# rule itself is never restated here.
_APPFOLIO_TENANT_REF_RE = re.compile(
    r"(?<![a-z0-9.-])([a-z0-9][a-z0-9-]*)\.appfolio\.com(?:/[^\s\"'<>\\)]*)?",
    re.IGNORECASE,
)


def _appfolio_body_has_listings_index(h: str) -> bool:
    """True when *h* references an AppFolio tenant's listings INDEX.

    ``h`` is the lower-cased page body. Slash-escaping is normalised first
    (same trick as :func:`_detect_sightmap_embed`) so a JSON-embedded URL is
    graded like a plain one.
    """
    if ".appfolio.com" not in h:
        return False
    h_norm = h.replace("\\/", "/")
    for m in _APPFOLIO_TENANT_REF_RE.finditer(h_norm):
        if is_listings_index_url(f"https://{m.group(0)}"):
            return True
    return False


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
    # On-Site.com (2026-07-18, flag-gated Surface C): a property whose own URL
    # IS the on-site.com apply/online_app3 portal. Handled before the static
    # fingerprint list so the flag gate is respected (the list is import-time).
    if enable_onsite_apply_adapter() and host in ("on-site.com", "www.on-site.com"):
        if "/apply/property" in path or "/web/online_app3" in path:
            return "onsite_apply", 0.95, [f"host is On-Site.com leasing portal ({host})"], None
    # Camden REIT (2026-07-19, flag-gated gap #14) — camdenliving.com is a
    # proprietary Next.js site with no PMS host fingerprint; route it to the
    # camden adapter (parses suggestedFloorPlans from __NEXT_DATA__).
    if enable_camden_adapter() and (host == "camdenliving.com" or host.endswith(".camdenliving.com")):
        return "camden", 0.95, [f"host is Camden REIT ({host})"], None
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


# 2026-07-12: a real SightMap embed URL in ANY escaping (literal, ``\/``,
# ``/``) — e.g. the IMT WordPress theme ships
# ``"sightmap_embed_url":"https:\/\/sightmap.com\/embed\/{code}"`` which the
# literal ``"sightmap.com/embed/" in h`` check missed. The captured {code}
# is the GUARD: the Entrata cookie-consent text (``"token":"engrain_sightmap"``
# … "enables Engrain's SightMap") and empty Engrain configs
# (``engrainedUrl: ''``) mention SightMap but carry NO code, so they
# correctly do NOT route to the (would-be-dead) SightMap adapter.
_SIGHTMAP_EMBED_CODE_RE = re.compile(
    r"sightmap\.com/embed/([a-zA-Z0-9_-]{4,32})", re.IGNORECASE
)


def _detect_sightmap_embed(h: str) -> bool:
    """True iff *h* carries a real ``sightmap.com/embed/{code}`` URL in any
    slash escaping. Requiring a captured code is the false-positive guard."""
    if "sightmap" not in h.lower():
        return False
    h_norm = (
        h.replace("\\u002f", "/").replace("\\u002F", "/").replace("\\/", "/")
    )
    return bool(_SIGHTMAP_EMBED_CODE_RE.search(h_norm))


def _iter_html_markers(page_html: str) -> Iterator[tuple[PmsName, float, list[str]]]:
    h = page_html.lower()
    # 2026-05-20 fix (feature_fail_1429 cluster #3): G5 marketing-cloud
    # markers (g5marketingcloud / g5dxm.com / g5-c- / g5-cl- /
    # dnn506yrbagrg.cloudfront.net) are often co-resident with a real
    # PMS widget — usually Knock/Doorway, sometimes RentCafe SecureCafe
    # or ResMan. In that case G5's page-level URN is a company-level
    # URN (e.g. ``g5-cl-...-lincoln-property-company-...``) and the
    # G5 adapter bails empty (``TIER_1_API_G5_EMPTY`` / ``_NO_URN``)
    # because the property-specific URN isn't on the page. Gate both
    # G5 branches below on the absence of these competing-PMS markers
    # so the real PMS detection wins.
    #
    # Live-verified 2026-05-20 against 6 Bucket-A worklist properties
    # (cluster G5+TIER_1_API): altaaptstarga / avonleatributary /
    # beechmeadowaptsin / unionthompson / 6thandalderapartments /
    # liveatone55lofts — every one had G5 markers AND Knock/Doorway
    # markers; none had securecafe/resman/rentcafe. The dominant
    # co-resident pattern is Knock, not RentCafe. The 2026-05-19
    # session memory of "hasRentCafe: true on Flatiron" was a recall
    # error — not present in the real fetched HTML.
    #
    # Order note: Knock at line ~676 already fires before the G5
    # *strong* branch at ~690 but AFTER the G5 *weak* branch at ~567.
    # Without this gate the weak branch wins. With it, Knock and the
    # later PMS-portal branches get a chance.
    _has_competing_pms_for_g5 = (
        # Knock / Doorway widget — the dominant co-resident PMS in
        # this cluster (≥6 of 6 Bucket-A props sampled 2026-05-20).
        "doorway.knck.io" in h
        or "knockdoorway" in h
        # RentCafe SecureCafe portal anchors (Flatiron pattern).
        or ".securecafe.com" in h
        or "securecafe.com/onlineleasing" in h
        # ResMan portal anchors.
        or "myresman.com" in h
        or "/portal/applicants/availability" in h
    )
    # F0.3 (2026-05-09): three-pass priority order so a real PMS portal
    # reachable from a Squarespace/Wix marketing shell (e.g. 123taylor.com →
    # ``onlineleasing.realpage.com``) isn't demoted to ``syndication_only``.
    #
    # Pass 1 — STRONG PMS markers: definitive portal/widget evidence that
    # cannot be an incidental CDN asset or marketing link. These beat
    # Wix/Squarespace shells because a real leasing path is the source of
    # truth even when the surrounding marketing site is built on a no-PMS
    # platform.
    # 2026-05-22 bucket-B grind: yielded STRONG (0.92) — ``onlineleasing
    # .realpage.com`` is the definitive OneSite leasing portal and cannot be
    # incidental. It must outrank a co-resident funnel/knock/g5 CHAT widget
    # (those fire at 0.90): the 9-property RealPage-OLL SPA cohort all
    # misrouted to funnel/g5/knock/onesite because the chat widget won.
    # Same rationale as the AppFolio tenant-portal and Repli360 strong-host
    # fixes.
    #
    # 2026-05-25 (canary 1ef1060 regr#3): Knock-gate the 0.92. The bucket-B
    # elevation overcorrected on 62 Knock-PMS properties (e.g. The Maya
    # at livethemaya.com, Park West at parkwestfortworth.com) where Knock
    # IS the real leasing system and ``onlineleasing.realpage.com`` is
    # just an APPLY link target (``<a href="https://8946105.onlineleasing
    # .realpage.com/#k=84971">apply</a>``). Flipping these to OneSite
    # dropped them from real per-unit data (Knock Doorway API: 177 units
    # on The Maya, 132 on Park West) to plan-level inference (OneSite
    # workflow: 13 / 4 ``inferred_*`` rows). 1,786 units total lost across
    # the cohort.
    #
    # The discriminator: when ``doorway.knck.io`` or ``knockDoorway`` are
    # also present, Knock is almost always the real PMS — those markers
    # only ship from a working Knock-Doorway install. The 9 RealPage-OLL
    # SPA cohort the elevation originally fixed do NOT have Knock
    # fingerprints (RealPage-OLL is the real leasing system, no Knock
    # co-resident). Demote OneSite to 0.85 (below Knock's 0.90) in the
    # Knock+OneSite co-resident case so Knock wins the route.
    _has_knock_marker = "doorway.knck.io" in h or "knockdoorway" in h
    # 2026-07-18: also match the OneSite "welcomehome" portal link
    # (property.onesite.realpage.com/welcomehome?siteId=NNN) — a distinct
    # OneSite surface whose siteId is parsed from the query by onesite.py.
    if "onlineleasing.realpage.com" in h or "onesite.realpage.com/welcomehome" in h:
        if _has_knock_marker:
            yield (
                "onesite",
                0.85,
                ["OneSite portal link present but Knock fingerprint co-resident "
                 "— OneSite is the apply-link target, Knock is the real PMS"],
            )
        else:
            yield "onesite", 0.92, ["OneSite portal marker in HTML (onlineleasing / welcomehome)"]

    # On-Site.com (on-site.com) — DISTINCT from RealPage OneSite above. The
    # marketing page links to ``on-site.com/apply/property/{id}`` (or
    # ``/web/online_app3?property_id={id}``), whose "online_app3" React shell
    # embeds a full UNIT-LEVEL roster as a props island (live-verified 2026-07-18
    # on 3 props). Fires at 0.91 — above co-resident knock/entrata (0.90) — only
    # for the specific apply/online_app3 path (never a bare on-site.com CDN ref),
    # since that path with a property id is an unambiguous leasing-system signal
    # and the surface carries real per-unit data. Flag-gated (default off) so the
    # default config is unchanged and a canary measures the recovery; when off,
    # these props route exactly as they do today.
    if enable_onsite_apply_adapter() and (
        "on-site.com/apply/property" in h
        or "on-site.com/web/online_app3" in h
        # backslash/entity-escaped hrefs (JSON-embedded links)
        or "on-site.com\\/apply\\/property" in h
        or "on-site.com\\/web\\/online_app3" in h
    ):
        yield (
            "onsite_apply",
            0.91,
            ["On-Site.com leasing-portal marker in HTML "
             "(on-site.com/apply/property or /web/online_app3)"],
        )
    # Venterra Living in-house (2026-07-19, flag-gated gap #4). The marketing
    # page SSRs a static ``var vt_units = [...]`` island (proper JSON) + links to
    # ``online.venterraliving.com/eOnlineLease``. Definitive Venterra + static
    # unit-level, so fires at 0.92 to beat a co-resident SightMap embed (these
    # props were mis-routed to SightMap + needs_render). The ``vt_units`` token
    # is Venterra's own JS var — it cannot false-fire on a non-Venterra site.
    if enable_venterra_adapter() and (
        "vt_units" in h
        or "online.venterraliving.com/eonlinelease" in h
        or "venterraliving.com/eonlinelease" in h
    ):
        yield (
            "venterra",
            0.92,
            ["Venterra in-house marker in HTML (vt_units island / eOnlineLease)"],
        )
    # RealPage OLL (Online Leasing) wizard — the "Category-D" cluster
    # (~187 props). Vanity marketing sites hop to ``leasing.realpage.com``
    # / embed an ``rp-leasing-widget`` / link ``<property>/content/apply#k=``
    # whose ``#k=`` key bootstraps the stateful ``RP.Leasing.AppService``
    # appstate session. None of these markers can appear on a real OneSite
    # numeric-subdomain site, so this is checked AFTER the OneSite marker
    # above and does not regress it. Routed to ``realpage_oll`` whose
    # adapter intercepts the OLL.SearchFloorPlan PUT response.
    if (
        # 2026-05-25 (canary 1ef1060 regr#3): the bare substring
        # ``leasing.realpage.com`` ALSO matches ``onlineleasing
        # .realpage.com`` (the OneSite portal subdomain), causing the
        # realpage_oll matcher to fire on every OneSite property —
        # including those with co-resident Knock fingerprints. URL
        # boundary (``://leasing.realpage.com``) restricts to the
        # standalone ``leasing.realpage.com`` host that realpage_oll
        # actually targets. The 9 RealPage-OLL SPA cohort that motivated
        # this matcher use ``leasing.realpage.com/`` as their actual
        # leasing host — they keep matching via this stricter check.
        "://leasing.realpage.com" in h
        or "rp-leasing-widget" in h
        or "rp.leasing.appservice" in h
        or "/content/apply#k=" in h
        # 2026-05-22 bucket-B grind: the OLL widget's static assets load
        # from ``cs-cdn.realpage.com/OLL/``. On G5-Marketing-Cloud-built
        # marketing sites the only RealPage signal in the HTML is this
        # asset host (verified on fmgnj.com/.../westgate-village — a G5
        # CMS site whose actual leasing system is RealPage OLL). Without
        # it the detector picked g5 (the marketing layer) and the G5
        # GraphQL inventory call 404'd because G5 is not the PMS here.
        or "cs-cdn.realpage.com/oll" in h
    ):
        # 2026-05-25 (canary 1ef1060 regr#3): Knock-gate the 0.92 the
        # same way as the OneSite matcher above. Knock fingerprints are
        # rare on real RealPage-OLL pages, so a co-resident Knock signal
        # almost always means Knock is the real PMS and RealPage OLL is
        # the apply-flow target.
        if _has_knock_marker:
            yield (
                "realpage_oll",
                0.85,
                ["RealPage OLL marker present but Knock fingerprint co-resident "
                 "— Knock is the real PMS"],
            )
        else:
            yield (
                "realpage_oll",
                # STRONG (0.92) — definitive leasing-wizard markers, must beat a
                # co-resident chat widget (funnel/knock at 0.90). See the
                # onlineleasing.realpage.com note above.
                0.92,
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
    _has_entrata_widget = (
        has_entrata_widget_path
        or "entrata-widget" in h
        or "commoncf.entrata.com" in h
        or ".prospectportal.com" in h
    )
    # Ported from main 78516c3 (2026-05-15 — "Added fixes for jsonld and few
    # pms"). Multi-PMS routing: when a page contains BOTH an Entrata-widget
    # marker AND a SightMap embed iframe (sightmap.com/embed/), the unit data
    # lives in the SightMap iframe — the Entrata widget on the same page is
    # typically a contact/amenities/photo module that doesn't carry units.
    # Promote SightMap to STRONG so it beats the Entrata-widget path that
    # would otherwise lock the SightMap adapter out.
    # Real cases: PID 16139 chaseknollsapts.com observed 2026-05-14 (Entrata
    # STRONG won, SightMap iframe with 9 units never got read); Morgan
    # Properties cluster (28 props observed 2026-05-19 — canary fell to
    # TIER_3_DOM on every one, prod caught SightMap).
    # Note: bare ``sightmap.com`` substring is NOT enough — could be a CDN
    # asset or analytics link. ``sightmap.com/embed/`` is iframe-specific.
    # 2026-07-12: also match escaped forms (\/, /) so the IMT
    # WordPress ``sightmap_embed_url`` JSON blob routes here; the code
    # requirement guards against the Entrata cookie-text / empty-config
    # false positives. See _detect_sightmap_embed.
    _has_sightmap_embed = _detect_sightmap_embed(h)
    if _has_sightmap_embed and _has_entrata_widget:
        yield (
            "sightmap",
            # 0.93 (2026-05-22): the SightMap embed iframe is the definitive
            # unit-data widget and must remain the strongest content marker —
            # above the RealPage hosted-portal markers (0.92), which carry a
            # *link* to a portal rather than the data itself.
            0.93,
            [
                "SightMap embed iframe + Entrata widget both present — "
                "SightMap carries unit data, routed there"
            ],
        )
    if _has_sightmap_embed:
        yield (
            "sightmap",
            0.93,
            ["SightMap embed iframe in HTML (sightmap.com/embed/)"],
        )
    # 2026-05-20: Engrain widget signal. RealPage's interactive-map vendor
    # (Engrain) loads the SightMap iframe dynamically post-JS, so static
    # HTML often lacks ``sightmap.com/embed/``. But the server-rendered HTML
    # carries paired ``data-unit``/``data-floorplan`` attributes (the
    # Engrain hydration placeholders) AND a ``realpage.com`` script load.
    # When BOTH appear in static HTML, route to sightmap so its iframe-
    # fallback discovery (cluster #5 broadening) fires; if it can't reach
    # the embed code, empty-exit retry routes to the next candidate.
    #
    # Verified live (2026-05-20 TIER_3_DOM ALL_fail probe, 7 of 25 props):
    # Sawmill Station, Headwaters Autumn Hall, Stadia Med Main, Delwyn,
    # Broadstone SoBro, Millennium River Oaks, Soleste Seaside. All
    # currently misroute to TIER_3_DOM_GENERIC and emit synthetic UIDs.
    # See project_tier3_dom_recovery_2026-05-20.md for the bucket analysis.
    _has_engrain_widget = (
        "data-unit" in h
        and "data-floorplan" in h
        and "realpage.com" in h
    )
    if _has_engrain_widget and not _has_sightmap_embed:
        yield (
            "sightmap",
            0.88,
            [
                "Engrain widget signal in HTML (data-unit + data-floorplan "
                "paired attrs + realpage.com script — SightMap iframe loads "
                "dynamically post-JS)"
            ],
        )
    # ``.prospectportal.com`` is Entrata's ProspectPortal product host
    # (definitive Entrata, not an incidental CDN asset). Marketing
    # shells that hop to <sub>.prospectportal.com route to EntrataAdapter
    # whose _probe_prospectportal handles the check_availability surface.
    if _has_entrata_widget:
        yield (
            "entrata",
            0.85,
            [
                "Entrata widget marker in HTML (/Apartments/module/"
                "<widget>/ / entrata-widget / commoncf.entrata.com / "
                ".prospectportal.com)"
            ],
        )
    # AppFolio tenant subdomain + leasing-system path
    # (``{tenant}.appfolio.com/listings`` | ``/connect`` | ``/apply``) is a
    # definitive signal that AppFolio is the property's leasing backend —
    # commonly embedded one nav-hop deep on a Wix/Squarespace marketing
    # shell, or alongside a co-resident marketing widget. Yielded STRONG
    # (0.92) so it beats both the Wix/Squarespace pass-2 demotion AND a
    # co-resident widget marker (apts247 / knock chat) that fires at 0.90
    # earlier in this function — same rationale as the Repli360 strong-host
    # fix above. 2026-05-22 probe: watermillpark / highlandpark (real
    # ``investorsmgmt.appfolio.com`` portal + an apts247 widget overlay),
    # maplegardens (``gcmultifamily.appfolio.com``) and legacyridge
    # (``cornerstoneresmgmt.appfolio.com``) all carried a genuine AppFolio
    # tenant portal yet misrouted to the overlay widget / a fallback DOM
    # tier and lost ~300 units each. Bare ``appfolio.com`` /
    # ``www.appfolio.com`` (the marketing domain, not a tenant instance)
    # stays a pass-3 weak 0.80 marker.
    #
    # 2026-07-28 — this branch now GRADES its evidence, and the grade is
    # visible in ``evidence`` so the two feeders and any later audit speak the
    # same language as
    # :func:`ma_poc.pms.appfolio_urls.is_listings_index_url`. A listings INDEX
    # reference is the operator saying "this page's inventory is served from
    # there"; a ``/connect`` resident-portal button, an ``/apply`` link or a
    # per-unit ``/listings/...`` deep link says only "this management company
    # uses AppFolio".
    #
    # The CONFIDENCE deliberately does NOT move, and that is a measured
    # decision, not an omission. Demoting the tenant-only branch to 0.85 was
    # implemented and swept over all 4,097 saved landing bodies of run
    # 2026-07-27-full-0d54ca7: 25 properties changed winning PMS
    # (appfolio → apts247 ×17, spherexx ×2, resman ×2, knock ×2, g5, and
    # wix_floor_plans) and 215 dropped 0.92 → 0.85 without changing PMS. Six of
    # the 25 already ran on the competing adapter in the real run, so the live
    # cost is 19 properties re-routed OFF ``AppFolioAdapter``. Eighteen of
    # those 19 are currently served by the adapter's ADDRESS-FILTERED vanity /
    # API paths (Watermill Park 4 units, Highland Park 3, The Current 65,
    # McClaren West End 37, Legacy Ridge 16, …) — 182 correctly scoped rows
    # that would be gambled on whether apts247 / knock / resman / g5 return
    # anything, which cannot be established offline. The only one of the 19
    # carrying a roster leak is Williams Gateway (pid 76334, 210 rows from
    # ``chamberlin.appfolio.com``), and that leak is closed by the
    # ``normalize_appfolio_url`` fix instead, at no coverage cost. The
    # 2026-05-22 cohort this branch was written for (watermillpark /
    # highlandpark on ``investorsmgmt``) sits squarely in the 18.
    #
    # So: tenant-only evidence is labelled, not demoted. AppFolio remains the
    # right ROUTE — the adapter's address filter is what makes it safe — and
    # the "this property's inventory lives here" upgrade is blocked in the
    # resolver, which is where it was actually being performed.
    _appfolio_tenant_slugs = {
        s for s in _APPFOLIO_TENANT_PATH_RE.findall(h) if s and s != "www"
    }
    if _appfolio_tenant_slugs and _appfolio_body_has_listings_index(h):
        yield (
            "appfolio",
            0.92,
            [
                "AppFolio tenant-portal marker in HTML "
                f"({sorted(_appfolio_tenant_slugs)[0]}.appfolio.com/"
                "{listings,connect,apply} — definitive leasing backend, "
                "outranks co-resident marketing widgets)"
            ],
        )
    elif _appfolio_tenant_slugs:
        yield (
            "appfolio",
            0.92,
            [
                "AppFolio tenant-only marker in HTML "
                f"({sorted(_appfolio_tenant_slugs)[0]}.appfolio.com — "
                "resident-portal/application/per-unit path only, no listings "
                "index: identifies the management account, NOT this property's "
                "inventory surface; scope via the adapter's address filter)"
            ],
        )
    elif ".appfolio.com/listings" in h:
        yield (
            "appfolio",
            0.85,
            ["AppFolio listings-iframe marker in HTML (.appfolio.com/listings)"],
        )
    # G5 (g5marketingcloud) Vue-SPA marketing sites. The g5dxm.com CDN /
    # g5marketingcloud asset hosts and the ``g5-c-{client}`` URN appear
    # site-wide incl. landing, so detection fires before the floor-plans
    # hop. Unit data is in the SPA's Apollo cache (see G5Adapter); most
    # sites carry no other PMS — but when a competing PMS marker (see
    # ``_has_competing_pms_for_g5`` above) is present, the G5 page-URN
    # is typically company-level and won't extract; fall through to
    # the real PMS branch below.
    if (
        "g5marketingcloud" in h or "g5dxm.com" in h or "g5-c-" in h
    ) and not _has_competing_pms_for_g5:
        yield (
            "g5",
            0.85,
            ["G5 marketing-cloud marker in HTML (g5marketingcloud / g5dxm.com / g5-c- URN)"],
        )
    # 2026-05-25 (canary 1ef1060 regr#15): Reinhold Residential's custom
    # Divi-child WordPress theme ships ``rr-unit-block`` SSR tables on
    # /availability/ with full per-unit rent + date data inline. The
    # ``rr-pricing-toggle`` + ``rr-unit-block`` class pair is unique to
    # this operator (rr- = Reinhold Residential). Sister sites verified
    # live: chocolateworks-living, shadyside-living, sharplesworks-living,
    # trinityrow-living, waterfront2-living. Routed to ReinholdAdapter.
    # Apply-button links go to securecafeapplicant.com (NOT .securecafe.com
    # so the generic RentCafe-securecafe marker below does NOT collide).
    if "rr-unit-block" in h and "rr-pricing-toggle" in h:
        yield (
            "reinhold",
            0.92,
            ["Reinhold Residential marker in HTML "
             "(rr-unit-block + rr-pricing-toggle classes)"],
        )
    # 365 ResidentServices (Apollo / collapsar theme) — a self-hosted CMS
    # used by a small operator cluster. Asset host fingerprint is stable
    # site-wide (used for plan-card images and theme css), so detection
    # fires on landing before any hop. SSR plan grid at
    # /Marketing/FloorPlans (see Residentservices365Adapter).
    if "365residentservices.com" in h:
        yield (
            "residentservices365",
            0.85,
            ["365 ResidentServices marker in HTML (365residentservices.com)"],
        )
    # RentVision is a multifamily marketing-site CMS whose ``/floorplans``
    # page is full plan-level SSR DOM. The platform credit appears
    # site-wide including the landing page, so detection fires before any
    # hop. These sites carry no other PMS.
    _has_rentvision_marker = (
        "created by rentvision" in h
        or "powered by rentvision" in h
        or "rentvision.com" in h
    )
    if _has_rentvision_marker:
        yield (
            "rentvision",
            0.85,
            ["RentVision CMS marker in HTML (created/powered by RentVision / rentvision.com)"],
        )
    # 2026-05-25 (canary 1ef1060 regr#8): RentalAddress.com is a small-
    # operator marketing CMS (cedarridgeapts.rentaladdress.com signature)
    # serving SSR /floor_plans pages with deterministic ``.floor_plan_list``
    # + ``.{field} value`` div pairs (square_feet, bedrooms, bathrooms,
    # rent, deposit). Pre-fix the generic plan-text parser saw "598" sqft
    # as part of the plan name string ("598 Bedroom / 1 Bath"). Fires on
    # the canonical hostname; sister sites on the same platform recover
    # for free.
    if "rentaladdress.com" in h or "floor_plan_list" in h:
        yield (
            "rentaladdress",
            0.88,
            ["RentalAddress.com CMS marker in HTML "
             "(rentaladdress.com host / .floor_plan_list container)"],
        )
    # 2026-05-25 (canary 1ef1060 regr#14): FortressTech is a Next.js-based
    # leasing-portal vendor that serves unit-level data via an iframe SSR
    # hydration payload. Operator marketing sites are typically Squarespace
    # shells (PRG Property Resources Group portfolio) that embed
    # ``<iframe src=…(availability|embed).fortresstech.io/unit-availability/
    # {orgId}/{propertyId}/…>``. 2026-07-19 (gap #6): many marketing pages carry
    # ONLY the ``portal.fortresstech.io/{orgId}/{propertyId}/register`` link (no
    # embed iframe) — previously excluded as "auth-only", but that URL carries
    # the same org/property id pair, and the adapter now builds the SSR
    # availability URL from it (fortresstech_availability_url). So route on any
    # ``portal.`` id-pair reference too. Detector fires on the host substring;
    # the adapter refetches the SSR availability URL and parses ``__next_f``.
    if (
        "availability.fortresstech.io/unit-availability" in h
        or "embed.fortresstech.io/unit-availability" in h
        or "portal.fortresstech.io/" in h
    ):
        yield (
            "fortresstech",
            0.90,
            [
                "FortressTech marker in HTML "
                "((availability|embed).fortresstech.io/unit-availability or "
                "portal.fortresstech.io/{orgId}/{propertyId})"
            ],
        )
    # Encoreskyline-template marketing family driven by the Jonah Digital /
    # MeetElise widget. Per-plan /floorplans/{slug}/ pages render real
    # apartment-level rows only after a Check-Availability JS click;
    # adapter handles the per-plan interaction. 2026-05-19 deep probe —
    # encoreskyline.com / geneseepointe.com / highlineaustin.com verified.
    #
    # 2026-05-20 fix (feature_fail_1429 cluster #2): Jonah / MeetElise is
    # sometimes a chat-widget bolt-on on top of a real PMS (RentCafe
    # SecureCafe, Entrata, ResMan, RealPage). In that case the
    # encoreskyline_template adapter has no way to extract — it expects
    # the Jonah-driven /floorplans/{slug}/ layout — and returns
    # NOT_ENCORESKYLINE_TEMPLATE / ENCORESKYLINE_NO_PLAN_LINKS, leaving
    # the property at 0 units. Gate the Jonah branch on the absence of
    # competing PMS markers so the real PMS detection below wins.
    # Live-verified: ardencebloom.com (pid 238181) — meetelise + securecafe,
    # main TIER_1_API_RENTCAFE captures 182 strict units.
    _has_competing_pms_marker = (
        ".securecafe.com" in h
        or "securecafe.com/onlineleasing" in h
        or "myresman.com/portal/applicants/availability" in h
        or "commoncf.entrata.com" in h
        or "/apartments/module/" in h
        or "entrata-widget" in h
        or ".prospectportal.com" in h
        or "onlineleasing.realpage.com" in h
        or ".appfolio.com/listings" in h
    )
    # 2026-07-12: Jonah's Knock integration (``JonahWidget.knock({init:[...]})``)
    # is a real Knock backend, not the generic encoreskyline template. Route it
    # to the knock adapter (which resolves the community_id → Doorway Tier-1
    # units) at a confidence that outranks the encoreskyline yield. The bare
    # ``jonahwidget`` / ``meetelise`` chat-widget markers (no ``.knock``) stay
    # on encoreskyline_template.
    if "jonahwidget.knock" in h and not _has_competing_pms_marker:
        yield (
            "knock",
            0.90,
            ["Jonah Knock integration (JonahWidget.knock init) → Knock Doorway"],
        )
    elif (
        "jonahwidget" in h
        or "jonahdigital" in h
        or "meetelise" in h
    ) and not _has_competing_pms_marker:
        yield (
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
        yield (
            "funnel",
            0.90,
            [
                "Funnel/Nestio marker in HTML "
                "(nestiolistings.com / NESTIO_* / data-nestio-* / "
                "funnelleasing.com / funnel-gen-ai-chat / integrations.nestio.com)"
            ],
        )
    if "mytouchtour.com" in h:
        yield "touchtour", 0.85, ["TouchTour portal marker in HTML (mytouchtour.com)"]
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
        yield (
            "spherexx",
            0.90,
            ["Spherexx marker in HTML (presentation.spherexx.app / ssploader.js / sspcfg)"],
        )
    # Knock / Doorway — marketing-led PMS widget. Identified by the
    # doorway.knck.io script tag and/or knockDoorway.init() call.
    # Public Doorway API serves full unit data: 2026-05-13 probe of
    # liveatcalista.com returned 53 units (incl. availableOn, price).
    if "doorway.knck.io" in h or "knockdoorway" in h:
        # 2026-07-11 audit: a Knock CHAT widget (knockDoorway.openContact /
        # openScheduling) is often co-resident with a real ResMan
        # Availability portal (regaliabellaterra et al). Knock is chat-only
        # there; ResMan owns the unit data. knock@0.90 tied resman@0.90 and
        # won on yield order → TIER_1_KNOCK_API returned 0 units. Demote
        # knock below resman, scoped tightly to the ResMan availability DATA
        # path so genuine Knock-data sites (liveatcalista, livethemaya —
        # knockDoorway present, no ResMan portal) are untouched.
        if _has_rentvision_marker:
            # A Knock widget can be a contact/scheduling integration on a
            # RentVision marketing site. When RentVision's public detail
            # pages expose a unit-number/rent roster, prefer that roster over
            # treating the widget as the property inventory source. Loch
            # Raven Village is the verified fixture (2026-07-27).
            yield (
                "knock",
                0.80,
                ["Knock/Doorway marker present but RentVision public roster "
                 "co-resident — RentVision detail pages own the unit data"],
            )
        elif "/portal/applicants/availability" in h:
            yield (
                "knock",
                0.85,
                ["Knock/Doorway chat marker present but ResMan Availability "
                 "portal co-resident — ResMan owns the unit data"],
            )
        else:
            yield (
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
    ) and not _has_competing_pms_for_g5:
        # ``dnn506yrbagrg.cloudfront.net`` is G5's primary CDN — per teammate
        # 2026-05-13 deep-dive (C1.G5/RealPage SSR sub-class), 73 of 244 generic
        # Tier-1-API failures had this CDN as their script source but no
        # G5/RealPage fingerprint fired because the detector didn't recognise
        # it. Adding it routes those sites to the G5 GraphQL adapter (which
        # then probes ``inventory.g5marketingcloud.com`` via the locationUrn
        # extracted from CDN paths).
        # 2026-05-20: gated on ``_has_competing_pms_for_g5`` so co-resident
        # SecureCafe/ResMan/hasRentCafe pages route to the real PMS adapter
        # instead of bailing on a company-level G5 URN.
        yield (
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
    #
    # 2026-05-21 grind600 finding: ``securecafenet.com`` (Yardi
    # resident-services alt domain) routes the same way — 100 of 600
    # random-sample properties used the .net domain via
    # ``<sub>.securecafenet.com/residentservices/<slug>/userlogin``. The
    # underlying Yardi tenant exposes the same ``onlineleasing/<slug>/
    # availableunits.aspx`` endpoint; rentcafe adapter handles both.
    _has_securecafe_portal = (
        "securecafe.com/onlineleasing" in h
        or ".securecafe.com" in h
        or "securecafenet.com/residentservices" in h
        or ".securecafenet.com" in h
    )
    if _has_securecafe_portal:
        # A public UnitID + FloorPlanID link is stronger than a generic
        # SecureCafe resident/apply link: it identifies a current inventory
        # row on this property's leasing route.  It must outrank a co-resident
        # Knock chat widget so the RentCafe adapter reaches the real units.
        # Requiring all three markers keeps normal Knock+SecureCafe apply-link
        # pages on their existing tie/next-candidate behaviour.
        if "floorplanid=" in h and "unitid=" in h:
            yield (
                "rentcafe",
                0.93,
                ["RentCafe SecureCafe public UnitID + FloorPlanID inventory link"],
            )
            return
        yield (
            "rentcafe",
            0.90,
            ["RentCafe securecafe(net) portal marker in HTML "
             "(<sub>.securecafe[net].com/{online,resident}leasing)"],
        )

    # ResMan — public availability portal at <client>.myresman.com/Portal/
    # Applicants/Availability?a=&p=, linked from the marketing /floorplans/
    # page. 2026-05-17 canary 842-pool deep-probe: 67+ sites the detector
    # missed (fell to LLM/floorplan). Portal is NOT Cloudflare-fronted, so
    # the ResMan adapter recovers Tier-1 unit-level even proxy-less.
    #
    # 2026-05-25 (user-flagged via Encore on Mustang pid 9186 + Regency
    # Grove): the resman marker fires on properties where ResMan is the
    # apply-flow target (``*.myresman.com/Portal/Applicants/New/...``
    # registration link) while the actual leasing data is on the apts247
    # ``/api/v1/floorplans/?api_key=`` API. Co-resident with apts247
    # markers, resman wins yield-order at the tied 0.90 and the resman
    # adapter then fails to find a portal URL (the registration link
    # isn't a portal). Fix: when apts247 markers are also present,
    # demote resman to 0.85 so apts247 wins. Same pattern as the
    # 2026-05-25 Knock-vs-OneSite gate.
    # Edifice CMS (Hexagon IT Solutions) — multifamily marketing CMS
    # whose own public REST API at edificecms.com/myresman/public/api/
    # front/{floorplans,units} serves UUID-keyed unit-level data. The
    # CMS routes apply-flow traffic to ResMan portals so a co-resident
    # ``myresman.com`` marker is the norm, not the exception — every
    # Edifice site we've sampled embeds a ResMan apply link. Fire
    # BEFORE the ResMan branch below so the canonical Edifice data
    # path wins at 0.92 (beats ResMan's 0.90). Live-verified 2026-05-25
    # on cobblestonephx.com (canary 1ef1060 regression #10) — 13
    # floorplans, multi-unit roster per plan.
    _has_edificecms_marker = (
        "edificecms.com" in h
        or "/edi-assets/" in h
        or "builder_live" in h  # the inline ``var BUILDER_LIVE =`` config blob
    )
    if _has_edificecms_marker:
        yield (
            "edificecms",
            0.92,
            ["Edifice CMS marker in HTML "
             "(edificecms.com / /edi-assets/ / BUILDER_LIVE)"],
        )

    # ThinkRESIDE / Resite Multi Family Marketing — DOM-only adapter
    # for the Resite CMS family. Plan list (``<li data-beds>`` on
    # /floorplans OR ``<div class="floorplan-item">`` on home) + per-
    # plan ``fp-availability-list`` table render the unit roster
    # inline; ``api.thinkresite.dev`` is a Walk-Score-style POI feed
    # not unit data. Live-verified 2026-05-25 against Orchard Ridge
    # (liveatorchardridge.com), Indy Flats (indyflatsapts.com), and
    # Deer Run (liveatdeerrunapts.com) — all 3 fell to TIER_MERGED_
    # CROSS_PAGE / generic plan-text with n_full=0 pre-fix.
    #
    # Fires at 0.87: above co-resident chat widgets (MeetElise / Jonah
    # encoreskyline_template at 0.85; bare RentCafe / AppFolio
    # substring at 0.80) so the SSR Resite plan list wins when both
    # are present. Sits below Knock Doorway (0.90) and RealPage OLL /
    # Edifice CMS (0.92), which remain the real PMS when their
    # fingerprints appear (Orchard Ridge embeds Doorway → Knock wins).
    if (
        "thinkresite.dev" in h
        or "themes.thinkresite.cloud" in h
        or "media.thinkresite.cloud" in h
        or "resite-themes.nyc3.digitaloceanspaces.com" in h
        or "resiteimages.nyc3.cdn.digitaloceanspaces.com" in h
        or "resite multi family marketing" in h
    ):
        yield (
            "thinkreside",
            0.87,
            ["ThinkRESIDE / Resite Multi Family Marketing marker in HTML "
             "(thinkresite.dev / themes.thinkresite.cloud / "
             "resite-themes.nyc3.digitaloceanspaces.com / "
             "Resite Multi Family Marketing)"],
        )

    _has_apts247_marker = "apts247" in h or "rentdynamics.com" in h
    if "myresman.com" in h or "/portal/applicants/availability" in h:
        if _has_apts247_marker:
            yield (
                "resman",
                0.85,
                ["ResMan marker present but apts247 co-resident — "
                 "apts247 is the data API, ResMan is the apply-flow target"],
            )
        elif _has_edificecms_marker:
            # Same co-resident pattern: Edifice CMS owns the data API,
            # ResMan owns the apply link. Demote ResMan so Edifice wins.
            yield (
                "resman",
                0.85,
                ["ResMan marker present but edificecms co-resident — "
                "Edifice owns the data API, ResMan is the apply-flow target"],
            )
        elif _has_rentvision_marker:
            # RentVision detail pages publish the actual unit roster inline
            # (unit number + asking rent + availability).  Their sites often
            # link a ResMan portal only from Apply/Check Availability.  The
            # old 0.90 ResMan priority hid the working RentVision adapter and
            # left the root page to generic plan-card parsing (Cypress Grove
            # and Loch Raven, 2026-07-27).  Prefer the public detail table;
            # the RentVision adapter follows every plan URL and emits only
            # rows with a real displayed unit number.
            yield (
                "resman",
                0.80,
                ["ResMan marker present but RentVision co-resident — "
                 "RentVision detail pages own the public unit roster"],
            )
        else:
            yield (
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
    if _has_apts247_marker:
        if "imtresidential.com" in h:
            # 2026-07-11 adapter audit: IMT's own portfolio pages embed a
            # ``rentdynamics_key`` config value (RentDynamics is their
            # pricing SDK), which trips this marker and outranked
            # imt_spaces (0.90 > 0.85) — imtgallery421 routed to apts247
            # and extracted 0 units while the page carries 185 rent
            # strings. Same co-resident pattern as resman/edificecms
            # above: demote so the IMT rule wins.
            yield (
                "apts247",
                0.80,
                ["apts247/RentDynamics marker present but "
                 "imtresidential.com co-resident — IMT Spaces owns the "
                 "page; RentDynamics is only their pricing SDK"],
            )
        else:
            yield (
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
    #
    # 2026-05-21 HAR validation: thebelmontbyreside.com and
    # liveattrailpoint.com have ALL the Repli360 markers (app.repli360.com,
    # getUnitListByFloor, rrac-website-script, rracFloorplan_{id} classes)
    # BUT they ALSO embed a Funnel/Nestio chat widget at the same site,
    # which yields a 0.90 "funnel" marker EARLIER in this function. Since
    # ties are broken by first-yielded, Funnel wins routing — and the
    # FunnelAdapter has no extraction path for these sites → they fall
    # to LLM. Fix: when the strong ``app.repli360.com`` host marker is
    # present (which a co-resident chat widget would NOT produce),
    # yield at 0.92 to outrank Funnel and route to RepliAdapter where
    # extraction works.
    has_strong_repli360 = "app.repli360.com" in h
    has_weak_repli360 = (
        "getunitlistbyfloor(" in h
        or "rrac_listavailableunit" in h
    )
    if has_strong_repli360:
        yield (
            "repli360",
            0.92,
            ["Repli360/rrac marker in HTML — strong host signal "
             "(app.repli360.com present, outranks co-resident funnel/"
             "knock chat widgets that fire at 0.90)"],
        )
    elif has_weak_repli360:
        yield (
            "repli360",
            0.90,
            ["Repli360/rrac marker in HTML "
             "(getUnitListByFloor / rrac_listAvailableUnit)"],
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
        yield (
            "rentmanager",
            0.90,
            ["RentManager/iLoveLeasing marker in HTML "
             "(.ua.rentmanager.com / .twa.rentmanager.com / "
             "cdn.rentmanager.com / iloveleasing.com)"],
        )

    # Pass 2 — Wix/Squarespace platform giveaway scripts. These are strong
    # "not-a-PMS" signals when no strong PMS marker appeared in pass 1.
    if "static.parastorage.com" in h or "wix.com" in h:
        # 2026-05-21 HAR validation: some Wix-hosted multifamily sites
        # embed plan-level data via a templated "Starting at $X" text
        # pattern (e.g. www.liveatarcos.com — 3 plan cards with
        # ``{Plan Title} {Code} {Beds} | {Baths} | {Sqft} Sq. Ft. $deposit
        # Starting at $rent``). The WixFloorPlansAdapter extracts these;
        # otherwise the generic WixNoPmsAdapter fallback handles bare
        # marketing brochures.
        if "starting at $" in h:
            yield (
                "wix_floor_plans",
                0.88,
                ["Wix host marker + 'Starting at $' plan-card text pattern"],
            )
        else:
            yield "wix_nopms", 0.85, ["Wix script/platform marker in HTML"]
    if "squarespace.com" in h:
        yield "squarespace_nopms", 0.85, ["Squarespace script/platform marker in HTML"]

    # Pass 3 — WEAK PMS markers: substrings that could be incidental CDN
    # asset references or marketing links rather than actual portal
    # endpoints. Run after Wix/Squarespace so a ``cdngeneralcf.rentcafe.com``
    # image on a Squarespace site doesn't misroute to RentCafe — see
    # ``test_no_rentcafe_false_positive_on_squarespace_with_cdn_asset``.
    if "entrata.com" in h:
        yield "entrata", 0.80, ["Entrata marker in HTML (entrata.com)"]
    if "rentcafe" in h or "yardi" in h:
        yield "rentcafe", 0.80, ["RentCafe/Yardi marker in HTML"]
    if "sightmap.com" in h:
        yield "sightmap", 0.80, ["SightMap iframe/script marker in HTML"]
    if ".appfolio.com" in h:
        yield "appfolio", 0.80, ["AppFolio marker in HTML"]
    if "liveovation.com" in h:
        yield "touchtour", 0.80, ["Ovation portfolio marker in HTML (liveovation.com)"]
    # Market Apartments CMS — vendor-templated marketing site. Strong
    # signal: ``assets.marketapts.com`` (asset CDN), ``api.marketapts.com``
    # (widget-config API), ``marketapts.com/js/schedule`` (scheduler
    # widget), or the footer "Powered by MarketApts" string. 2026-05-21
    # grind600: 13 properties originally tagged GoPrisma (residents-only
    # SPA portal) + 4 marketapts-tagged failures all share this CMS. The
    # unit-level data is server-rendered on the property's own
    # ``/floorplans`` page (Template A: inline .floorplan-unit-single
    # rows) or one click away on ``/unit/{plan-slug}`` (Template B:
    # .unit-table-row drill). Pre-fix: ``marketing_marketapts`` was a
    # signal-only label with no adapter, so these sites landed in
    # ``t2_llm_only``. The MarketAptsAdapter consumes this routing.
    if (
        "assets.marketapts.com" in h
        or "api.marketapts.com" in h
        or "marketapts.com/js/schedule" in h
        or "powered by marketapts" in h
    ):
        yield (
            "marketapts",
            0.80,
            ["Market Apartments CMS marker in HTML "
             "(assets.marketapts.com / api.marketapts.com / "
             "marketapts.com/js/schedule / Powered by MarketApts)"],
        )
    # 2026-05-21 HAR validation: sylispm.com is a real Market Apartments
    # site that uses ``www.marketapts.com/images/apartments/`` and
    # ``www.marketapts.com/iframes/fee-guide`` instead of the canonical
    # ``assets.marketapts.com`` asset host, so the strict signal above
    # misses it. The fee-guide / iframeResizer assets ALSO appear on
    # G5 sites (bellavista, lavina) that just embed Market Apartments'
    # fee-guide widget — so the weak host alone would false-fire.
    #
    # Combined rule: the weak host counts as a strong MA signal IFF a
    # MarketAptsAdapter template selector is ALSO present in the same
    # HTML body. G5/RentCafe co-residents with a fee-guide widget don't
    # ship Market Apartments template DOM, so this discriminates cleanly.
    _ma_weak_host = (
        "marketapts.com/iframes/" in h
        or "marketapts.com/images/apartments/" in h
    )
    _ma_template_present = (
        "floorplan-unit-single" in h  # Template A discriminator
        or "floorplan-item" in h       # Template B
        or "floorplan-container" in h  # Template C
        or "floor-plans-block" in h    # Template D
        or "floorplan-name" in h       # Template E
    )
    if _ma_weak_host and _ma_template_present:
        yield (
            "marketapts",
            0.80,
            ["Market Apartments CMS (combined-host + selector) marker — "
             "weak marketapts.com host (iframes/ or images/apartments/) "
             "co-occurs with a template selector; discriminates real MA "
             "from G5 fee-guide embeds (which lack the template selectors)"],
        )
    # 2026-05-21 HAR validation: RentCafe "modern" theme with inline
    # ``.floorplan-block`` + ``.par-units`` + ``.unit-container``.
    # Confirmed live on www.thefrankestate.com (9 plans, 11 units) and
    # www.somaresidences.com (6 plans, 9 units). Distinct from the
    # SecureCafe portal-hop variant most RentCafe sites use; these sites
    # publish unit inventory inline in same-origin DOM. Routed to the
    # dedicated RentCafeUnitRosterAdapter at 0.85 (higher than the
    # weak rentcafe substring marker at 0.80 so this wins when both
    # apply).
    if (
        "floorplan-block" in h
        and "par-units" in h
        and "unit-container" in h
    ):
        yield (
            "rentcafe_unit_roster",
            0.85,
            ["RentCafe modern-theme marker in HTML "
             "(.floorplan-block + .par-units + .unit-container all present)"],
        )
    # 2026-05-21 HAR validation: IMT Residential portfolio uses a custom
    # CMS branded "Spaces" with article.spaces-plan elements carrying
    # data-spaces-* attributes (rent, sqft, bed, bath, soonest-available
    # date, plan name). ~25 IMT properties fleet-wide. Confirmed live on
    # www.imtresidential.com/properties/imt-sorrento-valley/apartments/
    # (57 spaces-plan articles).
    #
    # Combined-signal rule: a real IMT site has BOTH the host marker
    # (imtresidential.com) AND/OR the spaces-community class prefix +
    # the spaces-plan article shape. Both conditions are tight enough
    # that either alone would route correctly; combined ensures
    # discrimination against incidental "spaces" word matches in
    # unrelated HTML.
    if (
        ("imtresidential.com" in h or "spaces-community-" in h)
        and "spaces-plan" in h
        and "data-spaces-plan" in h
    ):
        yield (
            "imt_spaces",
            0.85,
            ["IMT 'Spaces' theme marker in HTML "
             "(imtresidential.com host or spaces-community-* class) + "
             "article.spaces-plan with data-spaces-plan attr"],
        )
    elif "imtresidential.com" in h:
        # 2026-07-11 adapter audit: IMT vanity landing pages (e.g.
        # /imtgallery421/) do NOT statically embed the spaces-plan
        # articles — those live on /properties/{slug}/apartments/.
        # The host marker alone is unambiguous (IMT's own portfolio
        # domain), so route to imt_spaces anyway; the adapter's
        # static-subpage fallback hops to the apartments page.
        yield (
            "imt_spaces",
            0.85,
            ["imtresidential.com host marker (IMT portfolio domain; "
             "plans live on /properties/{slug}/apartments/ subpage)"],
        )
    # 2026-05-21 HAR validation: RealPage CWS (Community Website Solution)
    # ships the RPFP (RealPage Floor Plans) widget that renders plan
    # cards client-side. Verified live on www.liveatpenthouse.com and
    # www.liveatshadowglen.com — both /Floor-Plans.aspx, both load
    # cs-cdn.realpage.com/CWS/{numeric}/ assets and render
    # ``.rpfp-container > .rpfp-cards > .rpfp-card`` plan-level data.
    # Distinct from RealPage OLL (leasing.realpage.com BPM workflow)
    # which is a different product handled by RealPageOllAdapter.
    if (
        "cs-cdn.realpage.com/cws" in h
        or ("rpfp-container" in h and "rpfp-card" in h)
        or ("floorplans-widget" in h and "rpfp-" in h)
    ):
        yield (
            "realpage_cws",
            # STRONG (0.92) — the CWS RPFP widget is definitive; must beat a
            # co-resident chat widget (funnel/knock at 0.90).
            0.92,
            ["RealPage CWS RPFP widget marker in HTML "
             "(cs-cdn.realpage.com/CWS/ OR .rpfp-container + .rpfp-card)"],
        )
    # 2026-05-21 HAR validation: RentCafe "layout-tab" theme — bedroom-tab
    # listing on /floorplans with per-plan drill at /floorplans/{slug}.
    # Verified live on www.tudorplaceapts.com (drill /floorplans/two-bedrooms
    # has .unit-container × 12 with "#840_09 900 $1,765.00" rows) and
    # www.campobassoapts.com (drill /floorplans/studio has "Apartment: #
    # F_205 Starting at: $1,425.00" text format). Same class signature
    # ``.page-content-floorplans.floorplans-layout-tab`` confirmed on
    # both. RentCafeLayoutTabAdapter follows the per-plan drills and
    # parses both tabular and text-style unit rows.
    if (
        "page-content-floorplans" in h
        and "floorplans-layout-tab" in h
    ):
        # 2026-07-11 audit: bumped 0.86 → 0.92 so the SPECIFIC layout-tab
        # theme marker outranks the generic `rentcafe`@0.90 (and the
        # co-resident securecafe marker). Previously the generic RentCafe
        # adapter won, SHAPE_REJECTED, and the layout-tab adapter was only
        # reachable via the fragile Path-B retry chain — Tudor Place stayed
        # 0-units. RentCafeLayoutTabAdapter now extracts code-only via drill
        # hops; if it fails, the retry still falls back to rentcafe.
        yield (
            "rentcafe_layout_tab",
            0.92,
            ["RentCafe layout-tab theme marker in HTML "
             "(.page-content-floorplans + .floorplans-layout-tab)"],
        )
    # 2026-05-21 HAR validation: Equity Apartments REIT (~300 properties)
    # uses a custom Angular-based platform at equityapartments.com/{city}/
    # {neighborhood}/{slug}. Unit-level data renders inline in
    # ``.unit-expanded-card`` elements with text like:
    # ``$1,660 ... 0 Bed / 1 Bath 488 sq. ft. / Floor 7 Available 5/27/2026``.
    # Unit number is in the ``/UnitFees/{prop_id}/{building_id}/{unit_id}``
    # href on each card.
    if (
        "equityapartments.com" in h
        or "media.equityapartments.com" in h
        or ("unit-expanded-card" in h and "/unitfees/" in h)
    ):
        yield (
            "equity_apartments",
            0.88,
            ["Equity Apartments REIT marker in HTML "
             "(equityapartments.com host OR .unit-expanded-card + /UnitFees/ pattern)"],
        )
    # 2026-05-21 HAR validation: bespoke / custom-CMS multifamily sites
    # without any known PMS framework embed plan-level info in body
    # text using common multifamily conventions. The GenericPlanText
    # adapter is a last-resort fallback that regex-extracts repeating
    # "{Beds} Bedroom / {Baths} Bath ... $rent" patterns.
    #
    # Yielded at 0.55 — every recognized PMS / CMS signal beats this.
    # Only routes when no stronger adapter detected. The adapter's
    # own ≥2-distinct-rows guard kills single-noise false fires
    # (e.g. an amenity blurb saying "1 bedroom homes available for
    # $500/month").
    #
    # Verified extracts on: colonialcourtapts.com/floor-plans (Drupal),
    # stargatewest.com/tucson-rental-floor-plans/ (custom WP),
    # www.countryvillageapthomes.com (slick carousel). Correctly emits
    # nothing on wildwoodmanorapts.com and princetonmanagement.com
    # (both truly-empty marketing pages).
    if re.search(r"\d+\s*(?:bedroom|bdrm)\w*", h, re.IGNORECASE) and "$" in h:
        yield (
            "generic_plan_text",
            0.55,
            ["bedroom/bath + $ text pattern present in body "
             "(last-resort plan-level extractor; gated by ≥2-row parser threshold)"],
        )


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
    "entrata": (
        "entrata.com",
        "/apartments/module/",
        "entrata-widget",
        "prospectportal.com",
        "prospect_portal",
        "floorplan_overview",
    ),
    "rentcafe": ("rentcafe", "yardi"),
    "sightmap": ("sightmap.com",),
    "appfolio": (".appfolio.com",),
    "rentvision": ("created by rentvision", "powered by rentvision", "rentvision.com"),
    "resman": ("implicity.myresman.com", "myresman.com"),
    "g5": ("g5marketingcloud", "g5dxm.com", "g5-c-"),
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
    "reinhold": ("rr-unit-block", "rr-pricing-toggle", "reinholdresidential.com"),
    "residentservices365": ("365residentservices.com",),
    "encoreskyline_template": ("jonahwidget", "jonahdigital", "meetelise"),
    "aspensquare": ("aspensquare.com", "static.aspensquare.com"),
    # Edifice CMS (Hexagon IT Solutions) — multifamily marketing CMS.
    # Public same-vendor REST API at edificecms.com/myresman/public/api/
    # front/{floorplans,units} serves UUID-keyed unit-level data.
    # Live-verified 2026-05-25 on cobblestonephx.com.
    "edificecms": (
        "edificecms.com",
        "assets.edificecms.com",
        "/edi-assets/",
        "builder_live",
    ),
    "fortresstech": (
        "availability.fortresstech.io/unit-availability",
        "embed.fortresstech.io/unit-availability",
    ),
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
    # 2026-05-21 grind600: MarketAptsAdapter consumes this fingerprint —
    # the CMS hosts the unit-level data on /floorplans (Template A inline
    # rows) or /unit/{plan-slug} (Template B drill). Distinct from the
    # ``marketing_marketapts`` signal-only label above.
    "marketapts": (
        "assets.marketapts.com",
        "api.marketapts.com",
        "marketapts.com/js/schedule",
        "Powered by MarketApts",
    ),
}



def _detect_html_markers(
    page_html: str,
) -> tuple[PmsName, float, list[str]] | None:
    """Pick the highest-confidence marker yielded by :func:`_iter_html_markers`.

    Path B Piece 2 (2026-05-20): the HTML-marker scan is now a generator
    that yields ALL matching PMS candidates in detector pass order.
    The orchestrator uses ``detect_pms_candidates`` to find the NEXT
    PMS to try when the first adapter returns an empty exit.

    Selection rule (2026-05-20 detector-ranking fix): yields are NOT
    confidence-ordered — OneSite/RealPage at 0.85 are checked early in
    the function while SightMap at 0.90 is checked later. Returning the
    first yield made OneSite "win" over a higher-confidence SightMap
    embed on the same page (e.g. ``solesteseaside.com/floorplans/`` —
    OneSite portal marker + SightMap iframe both present; SightMap is
    the actual unit-data source). Fix: materialize all yields, return
    the one with the highest confidence. Ties broken by detector pass
    order (first-yielded wins) so the legacy single-signal contract is
    preserved.
    """
    matches = list(_iter_html_markers(page_html))
    if not matches:
        return None
    best_idx = 0
    best_conf = matches[0][1]
    for i in range(1, len(matches)):
        if matches[i][1] > best_conf:
            best_conf = matches[i][1]
            best_idx = i
    return matches[best_idx]

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


# ─────────────────────────────────────────────────────────────────────
# Path B Piece 2 (2026-05-20): ranked PMS candidates.
#
# When an adapter dispatched by ``detect_pms`` returns an empty-exit
# label (see :mod:`ma_poc.pms.empty_exit`), the orchestrator wants to
# retry with the *next* most likely PMS. ``detect_pms_candidates``
# returns up to ``max_candidates`` PMSs in detector priority order,
# skipping any already in ``exclude`` (typically: the PMS that just
# returned empty).
#
# Contract: ``detect_pms_candidates(url, csv_row, html)[0]`` is the
# same PMS that ``detect_pms(url, csv_row, html)`` would return,
# unless that PMS is in ``exclude``. This is the back-compat guarantee
# — adding a single-element guard around `next(candidates, None)` is
# equivalent to today's single-result API.
# ─────────────────────────────────────────────────────────────────────


def detect_pms_candidates(
    url: str,
    csv_row: dict[str, object] | None = None,
    page_html: str | None = None,
    exclude: set[str] | frozenset[str] | None = None,
    max_candidates: int = 4,
) -> list[DetectedPMS]:
    """Return up to *max_candidates* PMSs in detector priority order.

    Used by the Path B orchestrator-retry mechanism: when the first
    adapter returns an empty-exit label, the orchestrator calls this
    with ``exclude={previous_pms}`` to find the next candidate to try.

    Sources are exhausted in the same priority order as ``detect_pms``:
    CSV override > host fingerprint > URL extension > HTML markers
    (yielded in detector pass order) > CSV management prior. Each
    distinct PMS appears at most once (first occurrence wins; later
    sources for the same PMS just append evidence). The ``unknown``
    PMS is never included in the candidate list.

    Never raises; bad inputs return ``[]``.
    """
    excl: set[str] = set(exclude) if exclude else set()
    excl.add("unknown")  # never offer the no-op as a retry candidate

    seen: set[str] = set()
    out: list[DetectedPMS] = []

    def _emit(
        pms: PmsName,
        confidence: float,
        evidence: list[str],
        client_id: str | None = None,
    ) -> None:
        if pms in excl or pms in seen:
            return
        seen.add(pms)
        out.append(
            DetectedPMS(
                pms=pms,
                confidence=confidence,
                evidence=list(evidence),
                pms_client_account_id=client_id,
                recommended_strategy=_STRATEGY_BY_PMS[pms],
            )
        )

    try:
        # 0. CSV override — exact human-authored hint.
        override = _lookup_csv_pms_override(csv_row)
        if override is not None:
            pms, conf, reason = override
            _emit(pms, conf, [reason])

        # 1. Host fingerprint — high-confidence single hit.
        if isinstance(url, str):
            host_hit = _detect_host(url)
            if host_hit is not None:
                pms, conf, ev, client_id = host_hit
                _emit(pms, conf, ev, client_id)

        # 2. URL extension heuristic — weak Tier-1 hint.
        if isinstance(url, str):
            ext_hit = _detect_url_extension(url)
            if ext_hit is not None:
                pms, conf, ev = ext_hit
                _emit(pms, conf, ev)

        # 3. HTML markers — collect ALL matches, then sort by confidence
        # (descending) so the highest-confidence signal wins. Pass order
        # is preserved as the tie-break (stable sort). 2026-05-20 fix:
        # yield order alone made OneSite (0.85, yielded early) beat
        # SightMap (0.90, yielded later) on co-resident pages.
        if isinstance(page_html, str) and page_html:
            client_id_url = url if isinstance(url, str) else ""
            html_matches = list(_iter_html_markers(page_html))
            # Stable sort by -confidence preserves yield order on ties.
            html_matches.sort(key=lambda m: -m[1])
            for pms, conf, ev in html_matches:
                client_id = (
                    _client_account_id_from_url(client_id_url, pms)
                    if client_id_url
                    else None
                )
                _emit(pms, conf, ev, client_id)
                if len(out) >= max_candidates:
                    break

        # 4. Management-company prior — weak fallback.
        if len(out) < max_candidates:
            mgmt_hit = _lookup_mgmt_prior(csv_row)
            if mgmt_hit is not None:
                pms, reason = mgmt_hit
                _emit(pms, 0.70, [reason])

    except Exception as exc:  # defensive — detector must never crash pipeline
        # Return whatever we collected so far; never raise.
        out.append(
            DetectedPMS(
                pms="unknown",
                confidence=0.0,
                evidence=[f"detector-internal-error: {type(exc).__name__}"],
                recommended_strategy=_STRATEGY_BY_PMS["unknown"],
            )
        )

    return out[:max_candidates]
