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

# Leasing portal domains — ported from scripts/entrata.py _LEASING_PORTAL_DOMAINS.
#
# 2026-05-06 (canary-batch-11 bulk scan): added securecafe.com /
# securecafenet.com (RentCafe leasing-portal subdomains), prospectportal.com
# (Entrata leasing-portal subdomain), and appfolio.com (any tenant
# subdomain). Bulk scan of 81 still-failing properties in canary v10
# showed 19% link to *.securecafe.com / *.securecafenet.com and 7% to
# *.appfolio.com — both filtered out as "non-PMS" without these entries.
#
# 2026-05-06 (smart link-hop expansion): added cross-domain leasing portals
# observed in deep-dive samples — yottareal.com, ovationco.com,
# mytouchtour.com, liveovation.com, knockrentals.com, doorway.knck.io,
# hyly.ai, marketapts.com. NOTE: some of these (hyly.ai, knockrentals,
# marketapts) are CRM/marketing-widget hosts when embedded as iframes —
# they qualify as link-hop targets (because the redirect chain often
# leads to a real PMS portal) but should NOT be in the iframe-direct-
# follow allowlist (`_IFRAME_PMS_HOSTS` in scraper.py), which is for
# iframes whose embedded src is itself the unit-data source.
#
# 2026-05-07 audit additions: fortresstech.io (Carlson Place — Squarespace
# + widget), appfoliowebsites.com (AppFolio CMS host), g5marketingcloud.com
# (G5 GraphQL inventory API — followed when captured, not via iframe).
_LEASING_PORTAL_DOMAINS = frozenset(
    {
        # Original portals
        "sightmap.com",
        "realpage.com",
        "loftliving.com",
        "on-site.com",
        "rentcafe.com",
        "entrata.com",
        "yardi.com",
        "smartrent.com",
        "onlineleasing.realpage.com",
        # canary-batch-11 portal sublinks
        "securecafe.com",
        "securecafenet.com",
        "prospectportal.com",
        "appfolio.com",
        # canary-batch-11 cross-domain portfolio portals + CRM hosts
        "yottareal.com",
        "ovationco.com",
        "mytouchtour.com",
        "liveovation.com",
        "knockrentals.com",
        "doorway.knck.io",
        "hyly.ai",
        "marketapts.com",
        # 2026-05-07 audit additions
        "fortresstech.io",
        "availability.fortresstech.io",
        "portal.fortresstech.io",
        "appfoliowebsites.com",
        "g5marketingcloud.com",
    }
)


# 2026-05-06 (smart link-hop): URL-path patterns that strongly suggest
# leasing/availability content regardless of anchor text. Catches:
#   - footer logo links to portal (text="" or just a logo image)
#   - icon-only nav buttons (text=" " or unicode chevron)
#   - terse-text anchors ("More" / "Details" / "→") with revealing paths
#   - case-variant paths (/Floor-plans.aspx vs /floor-plans/)
# Word-boundary on the left so /tour-floorplan/ matches but /motorist/ doesn't.
_CTA_PATH_RE = re.compile(
    r"(?:^|/)(?:"
    r"floor[-_ ]?plans?"
    r"|floorplans?"
    r"|availab\w+"
    r"|listings?"
    r"|pricing"
    r"|rentals?"
    r"|onlineleasing"
    r"|guestcards?"
    r"|properties?/[^/]+"
    r"|apartments?/[^/]+"
    r")(?:/|$|[?#.])",
    re.IGNORECASE,
)


@dataclass
class ResolvedTarget:
    original_url: str
    resolved_url: str
    hop_path: list[str] = field(default_factory=list)
    final_detection: DetectedPMS = field(default_factory=lambda: detect_pms(""))
    method: Literal["no_hop", "cta_link", "iframe", "redirect", "failed", "fetch_only"] = "failed"


# 2026-05-06: priority lookup is now word-boundary-anchored on the LEFT.
# Substring-only matching produced false hits — e.g. "OneWall Communities works
# with eRenterPlan" got priority 60 because the substring "uni" (the "unit"
# keyword) appears inside "Communities". Anchor-on-the-left lets prefix matches
# like "availab" still match "availability" / "available" while rejecting the
# intra-word noise.
_PRIORITY_RES: dict[str, re.Pattern[str]] = {
    keyword: re.compile(r"\b" + re.escape(keyword), re.IGNORECASE)
    for keyword in _PRIORITY_MAP
}


def _get_priority(text: str) -> int:
    """Score anchor text by availability-relevance."""
    score = 0
    for keyword, priority in _PRIORITY_MAP.items():
        if _PRIORITY_RES[keyword].search(text):
            score = max(score, priority)
    return score


def _url_is_known_portal(url: str) -> bool:
    """True when *url* points to a known leasing-portal host.

    Combines two sources:
      - Adapter `static_fingerprints()` — the hosts each adapter explicitly claims.
      - The `_LEASING_PORTAL_DOMAINS` set — leasing-portal subdomains that no
        single adapter "owns" (e.g. *.securecafe.com is RentCafe sub-product;
        *.prospectportal.com is Entrata's portal subdomain).

    Pre-2026-05-06 only the first source was used in Step 3, so cross-domain
    sublinks to *.securecafe.com / *.securecafenet.com / *.prospectportal.com
    were silently dropped. ~25% of still-failing properties had such a sublink
    on their homepage; this widens the gate so the resolver can land on the
    portal and let the existing adapter cascade do its job.
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


def _url_matches_pms_fingerprints(url: str) -> bool:
    """Back-compat alias retained for existing callers (Step 1 / Step 5)."""
    url_lower = url.lower()
    for adapter in all_adapters():
        for fp in adapter.static_fingerprints():
            if fp in url_lower:
                return True
    return False


# 2026-05-06: link-hop candidate cap was 5, which dropped legitimate portal
# sublinks when a homepage had >5 internal "/floor-plans/" candidates sharing
# the top priority. Reproduced on hazelwoodhomesmd.com: 5 P=80–100 internal
# floorplan links ate the cap; the AppFolio "Resident Portal" link at P=30 was
# dropped before its URL was even checked. Raise cap and dedupe by canonical
# URL so duplicate "Floor Plans" anchors don't crowd out real portal hops.
_CANDIDATE_CAP = 8


def _candidate_dedup_key(href: str) -> str:
    """Dedup CTA candidates by netloc + path (drop scheme/query/fragment).

    Many homepages emit the same /floor-plans/ link as a header-nav, footer,
    and CTA button — three anchors with the same href but different surrounding
    text. Without dedup they each take a candidate slot. Stripping the query
    and fragment also de-duplicates "?utm_source=..." variants of the same
    portal URL.
    """
    try:
        parsed = urlparse(href)
    except Exception:
        return href.lower()
    netloc = (parsed.netloc or "").lower()
    path = (parsed.path or "/").rstrip("/") or "/"
    return f"{netloc}{path}"


def normalize_appfolio_url(url: str) -> str:
    """F11: when *url*'s host is `*.appfolio.com`, point at /listings.

    The bare-tenant root (e.g. https://richelsonmanagement.appfolio.com/)
    redirects to /users/oauth/login on most tenants — the L1 fetcher
    treats that 302 as "no useful body" and the property looks blocked.
    /listings is the public SSR data page on every tenant we've sampled
    (becovic, pillarrei, blackrealtymanagement, plentyofplaces,
    richelsonmanagement).

    Pass-through:
      - URLs already on /listings or any deeper /listings/<id> path.
      - URLs whose host doesn't end in appfolio.com.
      - The static AppFolio marketing site (www.appfolio.com).

    Pablogroup-style offboarded tenants will still 302 to
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
    # urlparse separates path from query/fragment, so /listings?q=1 has
    # path=="/listings". A startswith("/listings?") would be dead code.
    path = parsed.path or "/"
    if path == "/listings" or path.startswith("/listings/"):
        return url
    # Drop existing query/fragment when normalizing the entry path —
    # AppFolio /listings ignores them and a stale `?source=...` from the
    # vanity-site referral can break the SSR layout.
    return f"{parsed.scheme}://{parsed.netloc}/listings"


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

        seen_keys: set[str] = set()
        for link in links:
            href = link.get("href", "")
            text = link.get("text", "")
            if not href:
                continue
            # 2026-05-06 (smart link-hop): three independent triggers — any
            # one is sufficient to consider the anchor as a candidate. The
            # original code required (a). Bulk scan of 81 still-failing hosts
            # showed many portal links have empty / icon-only anchor text,
            # missing the (a) gate even though their URLs are clearly portal
            # destinations.
            #
            #   (a) anchor text matches CTA pattern (floor plan / availab /
            #       lease / apply / resident.*portal)
            #   (b) URL host is on a known leasing-portal allowlist
            #       (catches footer-logo and icon-only links to *.realpage,
            #       *.appfolio, *.securecafe, *.knockrentals etc.)
            #   (c) URL path matches the CTA-path regex
            #       (catches /Floor-plans.aspx, /properties/<slug>, /listings/ —
            #       the Essex apartment-homes case-variant URLs and similar)
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
            # (/tour, /apply, /contact, /book) — these reCAPTCHA on most PMSes
            # and aren't useful even if anchor text passed the CTA filter.
            if is_blacklisted_path(href):
                continue
            # Dedupe by canonical (netloc + path) key — three anchors with the
            # same href but different surrounding text shouldn't take three
            # candidate slots. The first occurrence wins (preserves whatever
            # text the page used for its primary CTA).
            key = _candidate_dedup_key(href)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            # Priority: anchor-text-derived score wins when it fires; otherwise
            # use a baseline reflecting WHY this candidate was admitted. Portal
            # match (b) is a stronger URL signal than path match (c) because
            # the host alone proves PMS identity.
            if text_match:
                priority = _get_priority(text)
            elif portal_match:
                priority = 75
            else:
                priority = 60
            candidates.append((priority, href, text))

        # Sort by priority descending, cap at _CANDIDATE_CAP (8 since 2026-05-06).
        candidates.sort(key=lambda x: -x[0])
        candidates = candidates[:_CANDIDATE_CAP]

        # Step 3: Two-pass — portal candidates first, then same-host CTA-path
        # candidates. Two passes (rather than one with mixed priorities) so a
        # priority-30 portal candidate still beats a priority-100 same-host
        # path candidate. URLs to a known portal prove PMS identity; same-host
        # paths only suggest there's data deeper in the site.
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

        # Pass 3b — same-host CTA-path candidates. Targets the Essex Apartments
        # case (essexapartmenthomes.com/apartments/<region>/<slug>/floor-plans)
        # plus any vanity CMS where unit data lives one path-hop deep. Limited
        # to same-host to avoid the cross-domain risk of navigating to a
        # marketing parent / unrelated lead-gen vendor.
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


def resolve_target_from_html(
    page_html: str | None,
    original_url: str,
    initial_detection: DetectedPMS,
    iframe_srcs: list[str] | None = None,
) -> ResolvedTarget:
    """Jugnu-mode (no live page) port of `resolve_target`.

    Production audit (2026-05-06 run, 1000-property sample) showed that
    `resolve_target()` was bypassed in 100% of Jugnu runs because the L1
    fetcher returns `fetch_result.body` and `page=None`. Consequence:
    every smart-link-hop / portal-sublink / candidate-dedup / word-
    boundary-keyword improvement made on the resolver shipped in legacy
    mode but never reached production. 850 of 1000 sampled properties
    (85%) were detected as a known PMS but extracted by some other tier
    because `resolve_target()` never ran.

    This function ports steps 1-4 of `resolve_target` to operate on the
    raw HTML body (BeautifulSoup). Step 5 (post-render redirect detection)
    is handled separately via `fetch_result.final_url` in the caller.

    Parameters
    ----------
    page_html : str | None
        The fetched HTML body. If None, only Step 1 (URL-only fingerprint
        match) can succeed.
    original_url : str
        The property's marketing-site URL (entry).
    initial_detection : DetectedPMS
        Result of `detect_pms(original_url, csv_row, page_html)`.
    iframe_srcs : list[str] | None
        Optional pre-extracted iframe srcs (e.g. from detector_signals).
        If None, this function will parse them from `page_html`.
    """
    result = ResolvedTarget(
        original_url=original_url,
        resolved_url=original_url,
        final_detection=initial_detection,
    )

    # Step 1: Already on PMS host?
    try:
        if initial_detection.confidence >= 0.85 and _url_matches_pms_fingerprints(original_url):
            normalized = normalize_appfolio_url(original_url)
            result.resolved_url = normalized
            result.method = "no_hop"
            result.hop_path = [original_url] if normalized == original_url else [original_url, normalized]
            return result
    except Exception:
        pass

    if not page_html or len(page_html) < 100:
        # Without HTML, we can only do URL-level matching (Step 1). If that
        # didn't fire, return fetch_only — the original URL is what the
        # caller will pass to the adapter.
        result.method = "fetch_only"
        result.hop_path = [original_url]
        return result

    # Steps 2-3: Extract candidate anchors from the HTML body.
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        result.method = "fetch_only"
        result.hop_path = [original_url]
        return result

    try:
        soup = BeautifulSoup(page_html, "lxml")
    except Exception:
        try:
            soup = BeautifulSoup(page_html, "html.parser")
        except Exception:
            result.method = "fetch_only"
            result.hop_path = [original_url]
            return result

    candidates: list[tuple[int, str, str]] = []
    seen_keys: set[str] = set()

    for a in soup.find_all("a", href=True):
        raw_href = a.get("href") or ""
        href = (raw_href if isinstance(raw_href, str) else " ".join(raw_href)).strip()
        if not href:
            continue
        # Resolve relative → absolute against original_url
        try:
            from urllib.parse import urljoin
            abs_href = urljoin(original_url, href)
        except Exception:
            abs_href = href
        if not abs_href.startswith(("http://", "https://")):
            continue
        text = (a.get_text(" ", strip=True) or "").strip()[:100]

        # Same three triggers as `resolve_target`:
        text_match = bool(_CTA_TEXT_RE.search(text))
        portal_match = _url_is_known_portal(abs_href)
        try:
            href_path = urlparse(abs_href).path or ""
        except Exception:
            href_path = ""
        path_match = bool(_CTA_PATH_RE.search(href_path))
        if not (text_match or portal_match or path_match):
            continue
        if is_blacklisted_path(abs_href):
            continue
        key = _candidate_dedup_key(abs_href)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        if text_match:
            priority = _get_priority(text)
        elif portal_match:
            priority = 75
        else:
            priority = 60
        candidates.append((priority, abs_href, text))

    candidates.sort(key=lambda x: -x[0])
    candidates = candidates[:_CANDIDATE_CAP]

    try:
        original_host = (urlparse(original_url).hostname or "").lower()
    except Exception:
        original_host = ""

    # Pass 3a — known portal hosts (cross-domain allowed)
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

    # Pass 3b — same-host CTA-path candidates
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

    # Step 4: iframes — operate on pre-extracted list or scan HTML.
    if iframe_srcs is None:
        iframe_srcs = []
        try:
            for iframe in soup.find_all("iframe", src=True):
                src = iframe.get("src") or ""
                if isinstance(src, str) and src.startswith(("http://", "https://", "//")):
                    if src.startswith("//"):
                        src = "https:" + src
                    iframe_srcs.append(src)
        except Exception:
            pass

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

    # Nothing found
    result.method = "failed"
    result.hop_path = [original_url]
    return result
