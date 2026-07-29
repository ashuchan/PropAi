"""
CTA-hop + leasing-portal resolver (Phase 4).

Turns vanity marketing-site URLs into PMS-hosted URLs by following CTAs,
detecting iframes to leasing portals, and capturing redirect chains.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlparse

from ma_poc.pms.adapters.registry import all_adapters
from ma_poc.pms.appfolio_urls import (
    is_appfolio_tenant_url,
    is_listings_index_url,
    is_scoped_listings_url,
)
from ma_poc.pms.detector import DetectedPMS, detect_pms

if TYPE_CHECKING:
    from playwright.async_api import Page


# Path-blacklist regex: URLs whose path matches are dropped before the
# resolver hands them to a CTA hop. Single source of truth (H1 — F1
# spec) — extend the alternation here, never start a parallel list.
#
# - tour / scheduletour / contact / apply / book — generic CTAs that
#   trigger reCAPTCHA on most PMSes.
# - /listings/rental_applications/ — AppFolio's tenant-application form
#   path (added 2026-05-04 — eliminated a 10-property reCAPTCHA cluster
#   in the production run analysis).
_BLACKLISTED_PATH_RE = re.compile(
    r"/(scheduletour|scheduleatour|schedule-tour|tour|contact|apply|book)/?(?:$|[?#])"
    r"|/listings/rental_applications/",
    re.IGNORECASE,
)


def is_blacklisted_path(url: str) -> bool:
    """True if *url*'s path is on the resolver path blacklist.

    Matching is case-insensitive and scoped to the URL path (query
    string and fragment ignored). Non-URL inputs return False.
    """
    if not url:
        return False
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    path = parsed.path or ""
    return bool(_BLACKLISTED_PATH_RE.search(path))


# Anchor text patterns suggesting links to availability/leasing pages.
# Ported from scripts/entrata.py _AVAILABILITY_ANCHOR_RE.
_AVAILABILITY_ANCHOR_RE = re.compile(
    r"view\s+availab|see\s+availab|check\s+availab"
    r"|view\s+floor|see\s+floor|floor\s*plan"
    r"|view\s+pricing|see\s+pricing"
    r"|view\s+unit|see\s+unit|view\s+apartment"
    r"|availab\w*\s+unit|available\s+apartment"
    r"|view\s+all\s+unit|see\s+all\s+unit",
    re.IGNORECASE,
)

# Broader CTA text patterns for apply/lease/availability buttons.
_CTA_TEXT_RE = re.compile(
    r"apply|availab|floor\s*plan|lease|resident.*portal",
    re.IGNORECASE,
)

# Priority map for anchor text: higher = more important.
_PRIORITY_MAP = {
    "availab": 100,
    "floor": 80,
    "pricing": 70,
    "apply": 50,
    "lease": 40,
    "unit": 60,
    "apartment": 55,
    "resident": 30,
}

# 2026-05-13: word-boundary anchored priority matching. Substring-only
# matching produced false hits — e.g. "OneWall Communities works with
# eRenterPlan" got priority 60 because the substring "uni" (the "unit"
# keyword) appears inside "Communities". Left-anchored on word boundary
# keeps prefix matches like "availab" → "availability" / "available".
_PRIORITY_RES: dict[str, re.Pattern[str]] = {
    keyword: re.compile(r"\b" + re.escape(keyword), re.IGNORECASE)
    for keyword in _PRIORITY_MAP
}

# Leasing portal domains — ported from scripts/entrata.py _LEASING_PORTAL_DOMAINS.
#
# 2026-05-13 (May 13 manual-QC analysis of 373 failed-property URLs): added
# the leasing-portal subdomains that no single adapter "owns" but which
# unambiguously resolve to a downstream PMS:
#   - securecafe.com / securecafenet.com (RentCafe leasing-portal sub-product,
#     observed on 10 failed properties: arlingtonwest, kingscross, cheyenne
#     trails, etc.)
#   - prospectportal.com (Entrata leasing-portal subdomain, 5 properties)
#   - appfolio.com (any tenant subdomain — 1 manual-tagged plus ~20 v10 data)
# All four were filtered out as "non-PMS" pre-fix because no adapter advertises
# them in static_fingerprints(). The 2026-05-13 manual QC confirmed all four
# are valid portal hop targets.
_LEASING_PORTAL_DOMAINS = frozenset(
    {
        "sightmap.com",
        "realpage.com",
        "loftliving.com",
        "on-site.com",
        "rentcafe.com",
        "entrata.com",
        "yardi.com",
        "smartrent.com",
        "onlineleasing.realpage.com",
        "securecafe.com",
        "securecafenet.com",
        "prospectportal.com",
        "appfolio.com",
        # 2026-05-13 — additional cross-domain portals observed in the
        # 98 cross-domain-rebrand cases from May 13 manual QC:
        # - yottareal.com (adaraportal.yottareal.com — verandahlake redirect)
        # - mriprospectconnect.com (MRI Software portal — charterclubapts,
        #   tgmcreeksidevillage)
        # - showmojo.com (ShowMojo unit-tour platform)
        # - apartmentsearch.com (CORT — relocation/aggregator with unit data)
        # - selftournow.com (TouchTour / Engrain self-tour)
        # - ovationco.com (Ovation Property Management — Vegas)
        "yottareal.com",
        "mriprospectconnect.com",
        "showmojo.com",
        "apartmentsearch.com",
        "selftournow.com",
        "ovationco.com",
        "knockrentals.com",
        "doorway.knck.io",
        # 2026-05-13 (Round 3): from 30-property live-probe sample
        "myresman.com",      # ResMan PMS portal — areac.myresman.com,
                             # rudeenmgt.myresman.com observed on
                             # crestriverdistrict, riverviewapts
        "reslisting.com",    # marquette-management.reslisting.com style
        "rentcafewebsite.com",  # legacy *.rentcafewebsite.com URLs
        # 2026-05-13 (Round 4): Spherexx Presentation Software ("Convert").
        # Leaflet-based interactive building site-map widget loaded via
        # window.sspcfg={key:<base64>}. Iframe src is
        # https://presentation.spherexx.app/#/ssp/availability.
        # Adapter NOT YET BUILT — but recognizing the host so the resolver
        # navigates there + 257KB SPA bundle XHRs are captured for future
        # adapter work. See investigations/2026-05-13/spherexx_finding.md.
        "presentation.spherexx.app",
        "spherexx.app",
        "spherexx.com",
        # 2026-05-21 (grind600 findings): two more residents-only portals
        # that anchor on marketing-CMS sites. No public availableunits
        # endpoint — both are Angular/React SPAs whose inventory is
        # auth-walled. Recognising the host stops the resolver from
        # treating these portal links as candidate floor-plan pages
        # (which would just hit a login screen). Marketing CMS at the
        # property's own root domain is the actual extraction target.
        #
        #   - goprisma.com — 13 / 600 sites (2.2%). Pattern
        #     ``<property>.goprisma.com/auth/login``. SPA title
        #     "Prisma". Hashed-subdomain variant also observed
        #     (``693c45120e14189f759c2889.goprisma.com``).
        #   - fortresstech.io — 7 / 600 sites (1.2%). Pattern
        #     ``portal.fortresstech.io/{group-uuid}/{property-uuid}/``.
        #     Same group-uuid shared across multiple properties =
        #     single ingestion endpoint per management company.
        "goprisma.com",
        "fortresstech.io",
    }
)


# 2026-05-21 (grind600 findings): "plan-level only" portfolio brands.
# The marketing-CMS root page exposes plan-level "N Homes Available"
# counts but the per-plan deep-probe sub-page is empty — no XHR loads
# unit-level data. Resolver should prefer the parent page and skip the
# documented dead-end sub-path; deep-probe attempts waste time and emit
# zero strict units.
#
#   - Harbor Group Management — GRADUATED 2026-05-26. Unit data lives
#     at ``…/{plan}/units`` (one level deeper than the ``…/{plan}/listing``
#     dead-end). Dedicated adapter ``_harbor_group.py`` ships as sub-tier
#     2.55 in generic.py. No longer plan-level-only.
#
# This constant is documented for future-adapter-author discovery.
# Not yet wired into resolver hop logic — call sites that already
# handle these brands (e.g. a custom Harbor Group adapter) should
# reference this constant when deciding to short-circuit deep probes.
_KNOWN_PLAN_LEVEL_ONLY_PATTERNS: tuple[str, ...] = (
    # empty — all known plan-level-only brands now have dedicated adapters
)


# 2026-05-13 — URL-path patterns that strongly suggest leasing/availability
# content regardless of anchor text. Empirical priors derived from the
# May 13 manual-QC tagging of 400 failed properties (rank by frequency):
#
#   pattern (anchor path contains)         manual-tagged count   % of tagged
#   ─────────────────────────────────────  ───────────────────   ───────────
#   /conventional                          105                   26%
#   /floor-plans                            88                   22%
#   /floor-plans#/  (SPA fragment)          28                    7%
#   /floor-plans.aspx (case variants)       17                    4%
#   /models                                 16                    4%
#   /floor-plans-and-pricing                13                    3%
#   /availability                            6                    2%
#   /vacancies                               1                   <1%
#   /floorplan-availability                  2                   <1%
#   /check-availability                      2                   <1%
#   /units-available, /units                 2                   <1%
#   /townhome-floorplans                     1                   <1%
#   /plans.html, /plans.asp                  3                   <1%
#   /interactive-site-map                    1                   <1%
#   /communities/<slug>, /property/<slug>    4                    1%
#   /apartment-communities/<slug>            1                   <1%
#
# The `/conventional/` segment is the Entrata Property Marketing Site URL
# scheme — `/<region>/<property-slug>/conventional/`. NOT yet handled by
# any adapter; all 105 manual-tagged sites lose because the resolver doesn't
# follow this path.
_CTA_PATH_RE = re.compile(
    r"(?:^|/)(?:"
    # Floor-plan variants (most common): /floor-plans, /floorplans,
    # /floor-plans-and-pricing, /townhome-floorplans, /floor-plans.aspx,
    # /plans.html, /plans.asp.
    r"floor[-_ ]?plans?(?:[-_ ]and[-_ ]pricing)?"
    r"|floorplans?"
    r"|townhome[-_ ]?floorplans?"
    r"|plans?\.(?:html|asp|aspx)"
    # Availability variants: /availability, /units-available, /units,
    # /vacancies, /check-availability, /floorplan-availability.
    r"|floorplan[-_ ]availab\w+"
    r"|check[-_ ]availab\w+"
    r"|availab\w+"
    r"|units?(?:[-_ ]available)?"
    r"|vacancies"
    # Leasing-engine paths: /onlineleasing, /oleapplication, /guestcards,
    # /listings, /pricing, /rentals.
    r"|listings?"
    r"|pricing"
    r"|rentals?"
    r"|onlineleasing"
    r"|oleapplication"
    r"|guestcards?"
    # /<region>/<slug>/conventional/ — Entrata Property Marketing Site.
    r"|conventional"
    # Greystar: /models.
    r"|models?"
    # Portfolio paths: /properties/<slug>, /property/<slug>,
    # /communities/<slug>, /apartment-communities/<slug>,
    # /our-properties/<slug>, /apartments/<region>/<slug>.
    r"|(?:property|properties)/[^/]+"
    r"|(?:community|communities)/[^/]+"
    r"|apartment[-_ ]communit(?:y|ies)/[^/]+"
    r"|our[-_ ]propert(?:y|ies)/[^/]+"
    r"|apartments?/[^/]+"
    # RentManager / Knock sitemap-style components: /interactive-site-map,
    # /availability-sitemap.
    r"|interactive[-_ ]site[-_ ]?map"
    r"|sitemap[-_ ]availab\w+"
    r")(?:/|$|[?#.])",
    re.IGNORECASE,
)


@dataclass
class ResolvedTarget:
    original_url: str
    resolved_url: str
    hop_path: list[str] = field(default_factory=list)
    final_detection: DetectedPMS = field(default_factory=lambda: detect_pms(""))
    method: Literal[
        "no_hop", "cta_link", "iframe", "redirect", "failed", "fetch_only", "no_hop_known_pms"
    ] = "failed"


def _get_priority(text: str) -> int:
    """Score anchor text by availability-relevance.

    2026-05-13: word-boundary anchored. Pre-fix, the substring "uni" inside
    "Communities" gave priority 60 (the "unit" keyword), spuriously elevating
    lead-form vendor links above legitimate PMS portal links.
    """
    score = 0
    for keyword, priority in _PRIORITY_MAP.items():
        if _PRIORITY_RES[keyword].search(text):
            score = max(score, priority)
    return score


def _url_is_known_portal(url: str) -> bool:
    """True when *url* points to a known leasing-portal host.

    Combines two sources:
      - Adapter `static_fingerprints()` — hosts each adapter explicitly claims.
      - `_LEASING_PORTAL_DOMAINS` — leasing-portal subdomains that no single
        adapter "owns" (*.securecafe.com, *.prospectportal.com, etc.).

    Pre-2026-05-13 only the first source was consulted in Step 3 sublinks, so
    *.securecafe.com / *.prospectportal.com / *.appfolio.com links were
    silently dropped. The May 13 manual-QC analysis showed ~16 of 373
    properties (~4%) point to one of these portal sub-products.
    """
    url_lower = url.lower()
    for adapter in all_adapters():
        for fp in adapter.static_fingerprints():
            if fp in url_lower:
                return True
    for domain in _LEASING_PORTAL_DOMAINS:
        if domain in url_lower:
            return True
    return False


def is_tenant_only_appfolio_url(url: str) -> bool:
    """True when *url* is on an AppFolio tenant subdomain but is NOT that
    tenant's listings index.

    Such a URL — a resident-portal sign-in, a pay-rent button, an owner
    portal, an SSO auth endpoint, a per-unit application/tour/detail deep
    link, or the bare tenant root — identifies the MANAGEMENT COMPANY. It is
    not a hop target: following it lands on a login wall at best, and (before
    2026-07-28) ``normalize_appfolio_url`` turned it into that company's
    entire account roster at worst.

    Note this is *not* the same question ``is_blacklisted_path`` answers.
    That blacklist is about reCAPTCHA-guarded CTAs; this is about whether a
    URL speaks for the property or only for the account. ``/connect/users/
    sign_in`` is not on the blacklist and never should be — it is a perfectly
    fetchable page, it just isn't this property's inventory.
    """
    if not is_appfolio_tenant_url(url):
        return False
    # A URL carrying ``filters[property_list]`` names a property, so it is not
    # tenant-only evidence even when its path is not the listings index —
    # ``normalize_appfolio_url`` will point it at /listings with the scope
    # intact.
    return not (is_listings_index_url(url) or is_scoped_listings_url(url))


def _url_matches_pms_fingerprints(url: str) -> bool:
    """Back-compat alias for callers that only want adapter-fingerprint matches
    (Step 1 short-circuit and Step 5 redirect-detection rely on this stricter
    check; Step 3 sublinks use the looser ``_url_is_known_portal``)."""
    url_lower = url.lower()
    for adapter in all_adapters():
        for fp in adapter.static_fingerprints():
            if fp in url_lower:
                return True
    return False


# 2026-05-13: candidate cap raised from 5 → 8 (May 13 manual QC analysis
# showed hazelwoodhomesmd-style cases where 5 P=80–100 internal floorplan
# links ate the cap, dropping the AppFolio "Resident Portal" link at P=30
# before its URL was even checked).
_CANDIDATE_CAP = 8

# PMS names that route to the fallback DOM/plan-text tier rather than a
# unit-capable adapter. Used by the page=None resolver's downgrade guard: a
# confident body-detected known PMS must never be silently rewritten to one of
# these by a same-host CTA hop. Kept in sync with detector.py's fallback names.
_GENERIC_PMS = frozenset({"generic_plan_text", "unknown"})


def _candidate_dedup_key(href: str) -> str:
    """Dedup CTA candidates by netloc + path.

    Many homepages emit the same /floor-plans/ link as a header-nav, footer,
    and CTA button — three anchors with the same href but different
    surrounding text. Without dedup they each take a candidate slot.
    """
    try:
        parsed = urlparse(href)
    except Exception:
        return href.lower()
    netloc = (parsed.netloc or "").lower()
    path = (parsed.path or "/").rstrip("/") or "/"
    return f"{netloc}{path}"


def normalize_appfolio_url(url: str) -> str:
    """Clean an AppFolio **listings index** URL. Never synthesise one.

    2026-07-28 — this function used to do
    ``base = f"{scheme}://{netloc}/listings"`` for *any* ``*.appfolio.com``
    URL whose path wasn't already ``/listings``. An AppFolio tenant subdomain
    identifies the MANAGEMENT COMPANY, not the property, so that rewrite
    silently upgraded "this property's site links to a tenant somewhere" into
    "this property's inventory is the whole of that tenant's account".

    Measured on run 2026-07-27-full-0d54ca7 (run-recorded ``scrape_url`` in
    ``events.jsonl``, all 4,982 properties): 242 properties were scraped at an
    unscoped ``{tenant}.appfolio.com/listings`` account roster and only 10 at a
    property-scoped URL. 127 of the 242 still carried the ``a=cw`` parameter
    that only ever appears on AppFolio's
    ``/connect/users/sign_in?a=cw&utm_source=apmsites_v3&utm_campaign=pay_rent_button``
    pay-rent button — the rewrite drops ``utm_*`` but keeps ``a``, so ``a=cw``
    on a ``/listings`` URL is this function's own fingerprint. 27 rosters fed
    more than one property: ``olympicmanagement.appfolio.com`` was scraped as 8
    different properties across Lacey/Lakewood/Olympia/Poulsbo/Sumner/Federal
    Way and 7 of them received the identical 294 units;
    ``terracemgmt.appfolio.com`` shipped the same 214 units as properties in
    Chicopee MA, Columbus OH and New Haven CT.

    The rule now lives in :mod:`ma_poc.pms.appfolio_urls` and is shared with
    the detector. Only a listings INDEX is an inventory surface; a portal/auth
    path, an owner portal, a per-unit deep link and the bare tenant root are
    all tenant-only evidence and are returned UNCHANGED. Declining to rewrite
    is not declining AppFolio: ``AppFolioAdapter`` still discovers the tenant
    slug from the property's own body (``find_appfolio_slug`` reads exactly
    these ``/connect`` links) and fetches the tenant listings through the
    vanity path, which applies ``filter_listings_by_property_address``. That
    is the address-scoped route — 2 mismatched rows in 754 (0.27%) — instead
    of the unfiltered SSR route this rewrite fed.

    Pass-through:
      - URLs already on /listings or any deeper /listings/<id> path.
      - URLs whose host doesn't end in appfolio.com.
      - The static AppFolio marketing site (www.appfolio.com).
      - Tenant-only paths (see above) — no ``/listings`` is manufactured.

    Pablogroup-style offboarded tenants will still 302 to
    appfolio.com/page-not-found-sub from /listings; the adapter handles
    that signal separately (TENANT_OFFBOARDED).

    2026-05-13 — multi-property tenant filter preservation:
    Some PMC AppFolio tenants host MANY properties under one subdomain
    (e.g. hayloftpropmgmt.appfolio.com manages 100+ buildings). The
    AppFolio listings widget accepts ``filters[property_list]=<NAME>``
    in the query string to narrow to a single property. Stripping the
    query string was correct for single-property tenants (the param was
    just a stale referral cookie like ?source=marketing) but WRONG for
    multi-property tenants — without the filter we extract all 100+
    properties' units and can't associate them with the canonical
    property we're trying to scrape.

    Live probe (2026-05-13) confirms:
      - hayloftpropmgmt.appfolio.com/listings              → 292 rents (all props)
      - hayloftpropmgmt.appfolio.com/listings?filters[...] → 40 rents (East Hampton only)

    Fix: preserve query params that look like filter directives
    (``filters[...]=``). Drop ONLY junk like utm/source/gclid that
    came from the upstream referral.
    """
    if not url:
        return url
    try:
        parsed = urlparse(url)
    except Exception:
        return url
    host = (parsed.hostname or "").lower()
    if not host.endswith(".appfolio.com"):
        return url
    if host == "www.appfolio.com":
        return url
    # urlparse separates path from query/fragment, so /listings?q=1 has
    # path=="/listings". A startswith("/listings?") would be dead code.
    path = parsed.path or "/"
    is_listings_path = path == "/listings" or path.startswith("/listings/")

    # Tenant-only evidence: return UNCHANGED rather than manufacturing an
    # account-wide roster URL. The bare tenant root, ``/connect/...``,
    # ``/apply``, ``/oportal/...`` and AppFolio's ``account.appfolio.com`` SSO
    # paths all land here.
    #
    # ``is_listings_path`` is deliberately the looser ``/listings`` prefix
    # rather than the shared
    # :func:`~ma_poc.pms.appfolio_urls.is_listings_index_url`: a per-unit
    # ``/listings/detail/<uuid>`` is not an inventory surface either, but it is
    # a URL the caller chose and the historic behaviour is to hand it back
    # untouched — same output either way, kept explicit rather than accidental.
    #
    # The scope carve-out is NOT a loophole. ``filters[property_list]=<name>``
    # is AppFolio's server-side property filter: a URL carrying it names a
    # PROPERTY, which is exactly the evidence a bare tenant URL lacks. Only the
    # unscoped synthesis is withdrawn. In run 2026-07-27-full-0d54ca7, 242 of
    # the 252 properties scraped at a ``/listings`` URL had no scope at all, so
    # this carve-out is narrow by measurement, not by hope.
    if not (is_listings_path or is_scoped_listings_url(url)):
        return url

    # 2026-05-13: strip only KNOWN referral-noise params; preserve everything
    # else (including the AppFolio-specific `filters[property_list]=`,
    # `theme_color`, etc. — the filter directives the widget actually uses).
    # Drops utm_*, gclid, fbclid, msclkid, source, ref, mc_eid that came
    # from the upstream vanity site's tracking. Preserving unknown params is
    # safer than the inverse — if AppFolio adds a new filter key tomorrow
    # we won't accidentally strip it.
    from urllib.parse import parse_qsl, urlencode

    _JUNK_PARAM_PREFIXES = ("utm_",)
    _JUNK_PARAM_NAMES = frozenset({
        "gclid", "fbclid", "msclkid", "yclid", "dclid",
        "source", "ref", "referrer", "referral",
        "mc_eid", "mc_cid", "_hsenc", "_hsmi",
    })

    def _keep_param(name: str) -> bool:
        nl = name.lower()
        if nl.startswith(_JUNK_PARAM_PREFIXES):
            return False
        if nl in _JUNK_PARAM_NAMES:
            return False
        # Bare timestamps (e.g. hayloft's `?1778663185787&...`) come through
        # parse_qsl as a key with no value. Drop them.
        if not name or name.isdigit():
            return False
        return True

    kept_qs = ""
    if parsed.query:
        try:
            pairs = parse_qsl(parsed.query, keep_blank_values=True)
            kept = [(k, v) for k, v in pairs if _keep_param(k)]
            if kept:
                kept_qs = urlencode(kept)
        except Exception:
            kept_qs = parsed.query  # fall back to original on parse error

    if is_listings_path:
        # Already on /listings. Preserve the path + kept query.
        if kept_qs:
            return f"{parsed.scheme}://{parsed.netloc}{path}?{kept_qs}"
        return f"{parsed.scheme}://{parsed.netloc}{path}"

    # Only reachable via the scope carve-out above: a non-/listings path that
    # nonetheless carries ``filters[property_list]``. Point it at /listings so
    # the filter reaches the endpoint that honours it. The result is
    # property-scoped, never an account roster.
    base = f"{parsed.scheme}://{parsed.netloc}/listings"
    return f"{base}?{kept_qs}" if kept_qs else base


async def resolve_target(
    page: Page,
    original_url: str,
    initial_detection: DetectedPMS,
) -> ResolvedTarget:
    """Resolve a vanity URL to its underlying PMS portal.

    Algorithm:
    1. If already on a known PMS host with high confidence, return no_hop
    2. Extract CTA links from the loaded page
    3. Check each candidate (capped at 5) for PMS fingerprint match
    4. Check iframes for leasing portal domains
    5. Check if page redirected to a PMS host during load
    6. Return failed if nothing found
    """
    result = ResolvedTarget(
        original_url=original_url,
        resolved_url=original_url,
        final_detection=initial_detection,
    )

    try:
        # Step 1: Already on PMS host?
        if initial_detection.confidence >= 0.85 and _url_matches_pms_fingerprints(original_url):
            # F11: AppFolio entry-URL normalization. Tenant root 302s to
            # /users/oauth/login (looks blocked); /listings is the data page.
            normalized = normalize_appfolio_url(original_url)
            result.resolved_url = normalized
            result.method = "no_hop"
            result.hop_path = [original_url] if normalized == original_url else [original_url, normalized]
            return result

        # Step 2: Extract CTA links from page
        candidates: list[tuple[int, str, str]] = []  # (priority, url, text)
        try:
            links = await page.evaluate("""() => {
                const anchors = document.querySelectorAll('a[href]');
                return Array.from(anchors).map(a => ({
                    href: a.href,
                    text: (a.textContent || '').trim().substring(0, 100),
                })).filter(a => a.href && a.href.startsWith('http'));
            }""")
        except Exception:
            links = []

        # 2026-05-13 — three independent triggers (any one sufficient) for
        # admitting an anchor as a candidate. May 13 manual QC of 373 failed
        # properties showed many portal/data sub-pages have empty or icon-only
        # anchor text, missing the (a) gate even though their URLs are clearly
        # data destinations.
        #
        #   (a) anchor text matches CTA pattern (existing behavior)
        #   (b) URL host is on a known leasing-portal allowlist (catches
        #       footer-logo and icon-only links to *.realpage, *.appfolio,
        #       *.securecafe, *.prospectportal, etc.)
        #   (c) URL path matches _CTA_PATH_RE — catches /Floor-plans.aspx,
        #       /<region>/<slug>/conventional/ (Entrata PMS site URLs),
        #       /models (Greystar), /vacancies, /floor-plans-and-pricing
        seen_keys: set[str] = set()
        for link in links:
            href = link.get("href", "")
            text = link.get("text", "")
            if not href:
                continue
            text_match = bool(_CTA_TEXT_RE.search(text))
            portal_match = _url_is_known_portal(href)
            try:
                href_path = urlparse(href).path or ""
            except Exception:
                href_path = ""
            path_match = bool(_CTA_PATH_RE.search(href_path))
            if not (text_match or portal_match or path_match):
                continue
            # Drop candidates whose path is on the resolver blacklist
            # (/tour, /apply, /contact, /book) — these reCAPTCHA on most PMSes.
            if is_blacklisted_path(href):
                continue
            # 2026-07-28 — an AppFolio tenant URL that is not that tenant's
            # listings index identifies the management company, not this
            # property. ``appfolio.com`` is in ``_LEASING_PORTAL_DOMAINS``, so
            # every footer "Pay Rent" / "Resident Login" anchor was admitted
            # here at priority 75 and won pass 3a ahead of the property's own
            # floor-plans page. Withdraw it as a hop target: AppFolio is still
            # reached, through ``AppFolioAdapter``'s address-filtered vanity
            # path, from the property's own body.
            if is_tenant_only_appfolio_url(href):
                continue
            # Dedup by canonical (netloc + path) key.
            key = _candidate_dedup_key(href)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            # Priority: anchor-text-derived score wins when it fires; otherwise
            # use a baseline reflecting WHY the candidate was admitted.
            if text_match:
                priority = _get_priority(text)
            elif portal_match:
                priority = 75  # portal host proves PMS identity
            else:
                priority = 60  # path match only (less strong than portal)
            candidates.append((priority, href, text))

        # Sort by priority descending, cap at _CANDIDATE_CAP (8 since 2026-05-13).
        candidates.sort(key=lambda x: -x[0])
        candidates = candidates[:_CANDIDATE_CAP]

        # Step 3: Two-pass — portal candidates first (cross-domain OK), then
        # same-host CTA-path candidates. Portal URLs prove PMS identity and
        # route to a known adapter; same-host paths only hint that data lives
        # deeper.
        try:
            original_host = (urlparse(original_url).hostname or "").lower()
        except Exception:
            original_host = ""

        # Pass 3a — known portal hosts (any host, cross-domain allowed).
        for _priority, href, _text in candidates:
            if not _url_is_known_portal(href):
                continue
            detection = detect_pms(href)
            normalized = normalize_appfolio_url(href)
            result.resolved_url = normalized
            result.final_detection = detection
            result.method = "cta_link"
            hops = [original_url, href]
            if normalized != href:
                hops.append(normalized)
            result.hop_path = hops
            return result

        # Pass 3b — same-host CTA-path candidates. Targets the May 13
        # manual-QC cluster: 108 properties have data at
        # `<vanity>/<region>/<slug>/conventional/`, 78 at /floor-plans, etc.
        # Same-host-only restriction avoids the cross-domain risk of
        # navigating to a marketing parent or third-party lead-gen vendor.
        for _priority, href, _text in candidates:
            try:
                href_parsed = urlparse(href)
            except Exception:
                continue
            href_host = (href_parsed.hostname or "").lower()
            if href_host != original_host:
                continue
            if not _CTA_PATH_RE.search(href_parsed.path or ""):
                continue
            detection = detect_pms(href)
            normalized = normalize_appfolio_url(href)
            result.resolved_url = normalized
            result.final_detection = detection
            result.method = "cta_link"
            hops = [original_url, href]
            if normalized != href:
                hops.append(normalized)
            result.hop_path = hops
            return result

        # Step 4: Check iframes for leasing portal domains
        try:
            iframe_srcs = await page.evaluate("""() => {
                const iframes = document.querySelectorAll('iframe[src]');
                return Array.from(iframes).map(f => f.src).filter(s => s.startsWith('http'));
            }""")
        except Exception:
            iframe_srcs = []

        for src in iframe_srcs:
            src_lower = src.lower()
            # Same rule as candidate admission above: a tenant-only AppFolio
            # iframe (an embedded resident-portal login, a per-unit tour form)
            # is not this property's inventory surface. A scoped or bare
            # ``/listings`` iframe — the shape brooksidejohnsoncreek.com ships —
            # still passes.
            if is_tenant_only_appfolio_url(src):
                continue
            if any(domain in src_lower for domain in _LEASING_PORTAL_DOMAINS):
                detection = detect_pms(src)
                normalized = normalize_appfolio_url(src)
                result.resolved_url = normalized
                result.final_detection = detection
                result.method = "iframe"
                hops = [original_url, src]
                if normalized != src:
                    hops.append(normalized)
                result.hop_path = hops
                return result

        # Step 5: Check if page URL changed (redirect)
        try:
            current_url = page.url
        except Exception:
            current_url = original_url

        if current_url != original_url and _url_matches_pms_fingerprints(current_url):
            detection = detect_pms(current_url)
            normalized = normalize_appfolio_url(current_url)
            result.resolved_url = normalized
            result.final_detection = detection
            result.method = "redirect"
            hops = [original_url, current_url]
            if normalized != current_url:
                hops.append(normalized)
            result.hop_path = hops
            return result

        # Step 6: Nothing found
        result.method = "failed"
        result.hop_path = [original_url]
        return result

    except Exception:
        # Never-fail: return failed on any exception
        result.method = "failed"
        result.hop_path = [original_url]
        return result


class _BodyPage:
    """A minimal page-shim that replays body-parsed signals through the
    EXISTING ``resolve_target`` scoring logic (CTA-candidate ranking, portal
    match, iframe check, redirect). Production dispatches L3 with page=None, so
    the live resolver was skipped entirely — the confirmed root of the
    SightMap-iframe / portal-hop / JS-injected-marker misroute mass. Feeding a
    RENDER-mode body's anchors + iframes + final_url through the same resolver
    recovers those hops with ZERO live-page requirement and ZERO change to the
    (misroute-prone) scoring path.
    """

    __slots__ = ("url", "_links", "_iframes")

    def __init__(self, url: str, links: list[dict[str, str]], iframes: list[str]) -> None:
        self.url = url  # resolve_target reads page.url for the redirect check
        self._links = links
        self._iframes = iframes

    async def evaluate(self, js: str, *_args: object) -> object:
        # Pattern-match the two querySelectorAll calls resolve_target issues.
        if "iframe" in js:
            return self._iframes
        if "a[href]" in js or "anchors" in js or "querySelectorAll('a" in js:
            return self._links
        return []


def _links_and_iframes_from_body(
    body: str, base_url: str
) -> tuple[list[dict[str, str]], list[str]]:
    """Extract absolute anchor hrefs (+text) and iframe srcs from HTML, mirroring
    the shape resolve_target's page.evaluate returns. Relative URLs are resolved
    against ``base_url`` (the page's final URL, exactly what the browser would
    do). Never raises — returns ([], []) on any parse failure."""
    from urllib.parse import urljoin

    try:
        from bs4 import BeautifulSoup
    except Exception:
        return [], []
    try:
        soup = BeautifulSoup(body, "html.parser")
    except Exception:
        return [], []

    links: list[dict[str, str]] = []
    for a in soup.find_all("a", href=True):
        try:
            href = urljoin(base_url, str(a["href"]).strip())
        except Exception:
            continue
        if not href.startswith(("http://", "https://")):
            continue
        text = a.get_text(" ", strip=True)[:100]
        links.append({"href": href, "text": text})

    iframes: list[str] = []
    for f in soup.find_all("iframe", src=True):
        try:
            src = urljoin(base_url, str(f["src"]).strip())
        except Exception:
            continue
        if src.startswith(("http://", "https://")):
            iframes.append(src)

    return links, iframes


async def resolve_target_from_body(
    body: str | None,
    original_url: str,
    final_url: str,
    initial_detection: DetectedPMS,
) -> ResolvedTarget:
    """page=None equivalent of ``resolve_target`` (Track 1). Parses CTA anchors
    + iframes from the already-fetched RENDER body and the redirect from
    ``final_url``, then runs them through the identical resolver scoring so a
    vanity site still hops to its SightMap iframe / leasing portal / redirected
    PMS host without a live page. Never-fail: any parse/resolve error degrades
    to a ``fetch_only`` no-hop (today's behaviour), so it can only ADD hops,
    never regress a currently-working property."""
    if not body:
        return ResolvedTarget(
            original_url=original_url,
            resolved_url=original_url,
            hop_path=[original_url],
            final_detection=initial_detection,
            method="fetch_only",
        )
    try:
        links, iframes = _links_and_iframes_from_body(body, final_url or original_url)
        shim = _BodyPage(final_url or original_url, links, iframes)
        resolved = await resolve_target(shim, original_url, initial_detection)  # type: ignore[arg-type]
        # Downgrade guard (2026-07-19 regression fix). At page=None the adapter
        # runs on the ALREADY-FETCHED body regardless of resolved_url (there is
        # no re-fetch after the hop), so the ONLY thing a hop changes is which
        # adapter is selected. A confident body-detected PMS on a *vanity* URL
        # (parkplacejville.com → rentcafe) fails Step-1's url-fingerprint gate
        # and falls to Pass-3b, which hops to a same-host /floorplans CTA and
        # re-detects it as generic — silently demoting a RentCafe/Entrata/Knock
        # UNIT-level gold to TIER_3_DOM_GENERIC plan-level (8/44 test100c
        # gold→plan demotions). Only UNKNOWN/GENERIC initial detections should
        # hop (those are the legit vanity→SightMap/portal recoveries, which are
        # unaffected here); a confident known unit-capable adapter keeps its
        # routing. Cross-known-PMS hops (rentcafe→securecafe) still pass — the
        # guard only fires when the hop DOWNGRADES to generic/unknown.
        if (
            initial_detection.pms not in _GENERIC_PMS
            and initial_detection.confidence >= 0.7
            and resolved.final_detection.pms in _GENERIC_PMS
        ):
            return ResolvedTarget(
                original_url=original_url,
                resolved_url=original_url,
                hop_path=[original_url],
                final_detection=initial_detection,
                method="no_hop_known_pms",
            )
        return resolved
    except Exception:
        return ResolvedTarget(
            original_url=original_url,
            resolved_url=original_url,
            hop_path=[original_url],
            final_detection=initial_detection,
            method="fetch_only",
        )
