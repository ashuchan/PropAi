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

# 2026-05-13 (port from fix/resolver-path-patterns-may13): word-boundary
# anchored priority matching. Substring-only matching produced false hits
# -- e.g. "OneWall Communities works with eRenterPlan" got priority 60
# because the substring "uni" (the "unit" keyword) appears inside
# "Communities". Left-anchored on word boundary keeps legitimate prefix
# matches like "availab" -> "availability" / "available".
_PRIORITY_RES: dict[str, re.Pattern[str]] = {
    keyword: re.compile(r"\b" + re.escape(keyword), re.IGNORECASE)
    for keyword in _PRIORITY_MAP
}

# Leasing portal domains. Originally ported from scripts/entrata.py.
#
# 2026-05-13 port (fix/resolver-path-patterns-may13): added the leasing-portal
# subdomains that no single adapter "owns" but which unambiguously resolve to
# a downstream PMS. May 13 manual QC of 373 failed-property URLs flagged
# these as valid hop targets that the pre-port resolver rejected because no
# adapter advertised them via static_fingerprints().
_LEASING_PORTAL_DOMAINS = frozenset(
    {
        # Original main set
        "sightmap.com",
        "realpage.com",
        "loftliving.com",
        "on-site.com",
        "rentcafe.com",
        "entrata.com",
        "yardi.com",
        "smartrent.com",
        "onlineleasing.realpage.com",
        # RentCafe leasing-portal sub-product (10 properties observed in
        # the May 13 manual-QC sample: arlingtonwest, kingscross, etc.)
        "securecafe.com",
        "securecafenet.com",
        # Entrata leasing-portal subdomain (5 properties)
        "prospectportal.com",
        # AppFolio tenant subdomains (1 manual + ~20 v10 data)
        "appfolio.com",
        # Cross-domain rebrand cases (98 properties in May 13 QC)
        "yottareal.com",         # adaraportal.yottareal.com - verandahlake redirect
        "mriprospectconnect.com",  # MRI Software portal
        "showmojo.com",          # ShowMojo unit-tour platform
        "apartmentsearch.com",   # CORT relocation aggregator
        "selftournow.com",       # TouchTour / Engrain self-tour
        "ovationco.com",         # Ovation Property Management - Vegas
        # Knock / Doorway widget portals
        "knockrentals.com",
        "doorway.knck.io",
        # 30-property live-probe sample
        "myresman.com",           # ResMan PMS portal
        "reslisting.com",         # marquette-management.reslisting.com
        "rentcafewebsite.com",    # legacy *.rentcafewebsite.com URLs
        # Spherexx Presentation Software ("Convert") interactive site-map
        # widget. Adapter not yet built in main; recognizing host so the
        # resolver navigates there and the SPA bundle is captured for
        # future adapter work.
        "presentation.spherexx.app",
        "spherexx.app",
        "spherexx.com",
    }
)


# 2026-05-13 port: URL-path patterns that strongly suggest leasing /
# availability content regardless of anchor text. Frequencies (manual
# tagging of 400 failed properties):
#
#   /conventional               105 (26%)  - Entrata Property Marketing Site
#   /floor-plans variants       121 (30%)  - most-common landing path
#   /models                      16 (4%)   - Greystar PMS
#   availability / vacancies     12 (3%)
#   leasing-engine paths         varies    - /listings, /onlineleasing, etc.
#   portfolio paths              varies    - /properties/<slug>, /communities/<slug>
#   sitemap-style                 ~3       - /interactive-site-map
_CTA_PATH_RE = re.compile(
    r"(?:^|/)(?:"
    # Floor-plan variants
    r"floor[-_ ]?plans?(?:[-_ ]and[-_ ]pricing)?"
    r"|floorplans?"
    r"|townhome[-_ ]?floorplans?"
    r"|plans?\.(?:html|asp|aspx)"
    # Availability variants
    r"|floorplan[-_ ]availab\w+"
    r"|check[-_ ]availab\w+"
    r"|availab\w+"
    r"|units?(?:[-_ ]available)?"
    r"|vacancies"
    # Leasing-engine paths
    r"|listings?"
    r"|pricing"
    r"|rentals?"
    r"|onlineleasing"
    r"|oleapplication"
    r"|guestcards?"
    # Entrata Property Marketing Site: /<region>/<slug>/conventional/
    r"|conventional"
    # Greystar
    r"|models?"
    # Portfolio paths
    r"|(?:property|properties)/[^/]+"
    r"|(?:community|communities)/[^/]+"
    r"|apartment[-_ ]communit(?:y|ies)/[^/]+"
    r"|our[-_ ]propert(?:y|ies)/[^/]+"
    r"|apartments?/[^/]+"
    # RentManager / Knock sitemap-style components
    r"|interactive[-_ ]site[-_ ]?map"
    r"|sitemap[-_ ]availab\w+"
    r")(?:/|$|[?#.])",
    re.IGNORECASE,
)


# 2026-05-13 port: candidate cap raised from 5 to 8. May 13 manual QC
# showed hazelwoodhomesmd-style cases where 5 high-priority internal
# floor-plan links ate the cap before a portal link at priority 30
# could be evaluated.
_CANDIDATE_CAP = 8


@dataclass
class ResolvedTarget:
    original_url: str
    resolved_url: str
    hop_path: list[str] = field(default_factory=list)
    final_detection: DetectedPMS = field(default_factory=lambda: detect_pms(""))
    method: Literal["no_hop", "cta_link", "iframe", "redirect", "failed", "fetch_only"] = "failed"


def _get_priority(text: str) -> int:
    """Score anchor text by availability-relevance.

    2026-05-13 port: word-boundary anchored. Pre-port the substring "uni"
    inside "Communities" gave priority 60 (the "unit" keyword), spuriously
    elevating lead-form vendor links above legitimate PMS portal links.
    """
    score = 0
    for keyword, priority in _PRIORITY_MAP.items():
        if _PRIORITY_RES[keyword].search(text):
            score = max(score, priority)
    return score


def _url_matches_pms_fingerprints(url: str) -> bool:
    """Check if a URL's host matches any adapter's static fingerprints.

    Stricter than ``_url_is_known_portal``: only counts hosts an adapter
    explicitly claims. Used by Step 1 (already-on-PMS short-circuit) and
    Step 5 (redirect-detection) where we need to identify "this URL
    is an adapter's home turf" specifically.
    """
    url_lower = url.lower()
    for adapter in all_adapters():
        for fp in adapter.static_fingerprints():
            if fp in url_lower:
                return True
    return False


def _url_is_known_portal(url: str) -> bool:
    """True when *url* points to a known leasing-portal host.

    2026-05-13 port: looser than ``_url_matches_pms_fingerprints``.
    Combines two sources:
      - adapter ``static_fingerprints()`` (hosts each adapter explicitly claims)
      - ``_LEASING_PORTAL_DOMAINS`` (leasing-portal subdomains that no
        single adapter owns, e.g. *.securecafe.com, *.prospectportal.com)

    Pre-port only the first source was consulted at Step 3 candidate
    admission, so *.securecafe.com / *.prospectportal.com / *.appfolio.com
    links were silently dropped. May 13 manual QC found ~16 of 373 failed
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


def _candidate_dedup_key(href: str) -> str:
    """Dedup CTA candidates by netloc + path.

    Many homepages emit the same /floor-plans/ link as header-nav,
    footer, and CTA button -- three anchors with the same href but
    different surrounding text. Without dedup they each consume a
    candidate slot. 2026-05-13 port.
    """
    try:
        parsed = urlparse(href)
    except Exception:
        return href.lower()
    netloc = (parsed.netloc or "").lower()
    path = (parsed.path or "/").rstrip("/") or "/"
    return f"{netloc}{path}"


# 2026-05-13 port: AppFolio multi-tenant query-param preservation.
# Some PMC AppFolio tenants host many properties under one subdomain
# (e.g. hayloftpropmgmt.appfolio.com manages 100+ buildings). The
# AppFolio listings widget accepts ``filters[property_list]=<NAME>``
# in the query string to narrow to a single property. Stripping all
# query params was correct for single-property tenants (the param was
# usually a stale referral cookie) but WRONG for multi-property
# tenants -- without the filter we extract all 100+ properties' units.
#
# Live probe (2026-05-13) confirms:
#   hayloftpropmgmt.appfolio.com/listings              -> 292 rents (all props)
#   hayloftpropmgmt.appfolio.com/listings?filters[...] -> 40 rents (target only)
_APPFOLIO_JUNK_PARAM_PREFIXES = ("utm_",)
_APPFOLIO_JUNK_PARAM_NAMES = frozenset({
    "gclid", "fbclid", "msclkid", "yclid", "dclid",
    "source", "ref", "referrer", "referral",
    "mc_eid", "mc_cid", "_hsenc", "_hsmi",
})


def _appfolio_keep_param(name: str) -> bool:
    """True when *name* should be preserved on a normalized AppFolio URL.

    Drops only KNOWN referral-noise params (utm_*, gclid, source, etc.).
    Preserves everything else -- including AppFolio's
    ``filters[property_list]=`` and ``theme_color``. Bare timestamps
    (keys with empty value, all-digit names) are also dropped.

    Preserving unknown params is safer than the inverse: if AppFolio adds
    a new filter key tomorrow we won't accidentally strip it.
    """
    nl = name.lower()
    if nl.startswith(_APPFOLIO_JUNK_PARAM_PREFIXES):
        return False
    if nl in _APPFOLIO_JUNK_PARAM_NAMES:
        return False
    if not name or name.isdigit():
        return False
    return True


def normalize_appfolio_url(url: str) -> str:
    """F11: when *url*'s host is `*.appfolio.com`, point at /listings.

    The bare-tenant root (e.g. https://richelsonmanagement.appfolio.com/)
    redirects to /users/oauth/login on most tenants -- the L1 fetcher
    treats that 302 as "no useful body" and the property looks blocked.
    /listings is the public SSR data page on every tenant we've sampled.

    Pass-through:
      - URLs already on /listings or any deeper /listings/<id> path
        (query params still get the junk-filter pass below).
      - URLs whose host doesn't end in appfolio.com.
      - The static AppFolio marketing site (www.appfolio.com).

    2026-05-13 port: preserve multi-tenant filter params via
    ``_appfolio_keep_param``. See module-level doc above.

    Pablogroup-style offboarded tenants still 302 to
    appfolio.com/page-not-found-sub from /listings; the adapter handles
    that signal separately (TENANT_OFFBOARDED).
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
    path = parsed.path or "/"
    is_listings_path = path == "/listings" or path.startswith("/listings/")

    # Filter the query string -- drop known junk, keep everything else.
    from urllib.parse import parse_qsl, urlencode

    kept_qs = ""
    if parsed.query:
        try:
            pairs = parse_qsl(parsed.query, keep_blank_values=True)
            kept = [(k, v) for k, v in pairs if _appfolio_keep_param(k)]
            if kept:
                kept_qs = urlencode(kept)
        except Exception:
            # Fall back to the raw query rather than dropping it on parse
            # error -- losing real filter params is worse than keeping junk.
            kept_qs = parsed.query

    if is_listings_path:
        if kept_qs:
            return f"{parsed.scheme}://{parsed.netloc}{path}?{kept_qs}"
        return f"{parsed.scheme}://{parsed.netloc}{path}"

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

        # 2026-05-13 port: three independent triggers (any one sufficient)
        # for admitting an anchor as a candidate. May 13 manual QC showed
        # many portal / data sub-pages have empty or icon-only anchor text,
        # missing the (a) gate even though their URLs are clearly data
        # destinations.
        #
        #   (a) anchor text matches _CTA_TEXT_RE
        #   (b) URL host is on a known leasing-portal allowlist (catches
        #       footer-logo and icon-only links to *.realpage, *.appfolio,
        #       *.securecafe, *.prospectportal, etc.)
        #   (c) URL path matches _CTA_PATH_RE (catches /floor-plans.aspx,
        #       /<region>/<slug>/conventional/, /models, /vacancies, ...)
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
            # (/tour, /apply, /contact, /book) -- these reCAPTCHA on
            # most PMSes.
            if is_blacklisted_path(href):
                continue
            # Dedup by canonical (netloc + path) key. Header-nav, footer,
            # and CTA-button anchors often share an href; without this,
            # they each consume a candidate slot.
            key = _candidate_dedup_key(href)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            # Priority assignment branches by which trigger fired:
            #   text_match    -> anchor-text-derived score (existing path)
            #   portal_match  -> 75 (host proves PMS identity)
            #   path_match    -> 60 (path hints at data; less strong than host)
            if text_match:
                priority = _get_priority(text)
            elif portal_match:
                priority = 75
            else:
                priority = 60
            candidates.append((priority, href, text))

        # Sort by priority descending, cap at _CANDIDATE_CAP (8 since
        # 2026-05-13 port; was inline 5).
        candidates.sort(key=lambda x: -x[0])
        candidates = candidates[:_CANDIDATE_CAP]

        # Step 3: Two-pass admission.
        #   Pass 3a -- known portal hosts (any host, cross-domain OK).
        #     Portal URLs prove PMS identity and route to a known adapter,
        #     so cross-domain is acceptable.
        #   Pass 3b -- same-host CTA-path candidates. Same-host-only
        #     restriction avoids the cross-domain risk of following to a
        #     marketing parent or third-party lead-gen vendor.
        try:
            original_host = (urlparse(original_url).hostname or "").lower()
        except Exception:
            original_host = ""

        # Pass 3a: portal hosts win first.
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

        # Pass 3b: same-host CTA-path candidates. Targets the May 13 QC
        # cluster: 108 properties have data at <vanity>/<region>/<slug>/
        # conventional/, 78 at /floor-plans.
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
