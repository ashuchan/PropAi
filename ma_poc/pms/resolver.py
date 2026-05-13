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
    }
)


# 2026-05-13 — URL-path patterns that strongly suggest leasing/availability
# content regardless of anchor text. Empirical priors derived from the
# May 13 manual-QC tagging of 373 failed properties (rank by frequency):
#
#   pattern (anchor path ends with)        manual-tagged count   % of tagged
#   ─────────────────────────────────────  ───────────────────   ───────────
#   /conventional                          108                   29%
#   /floor-plans                            78                   21%
#   /floorplans                             57                   15%
#   /models                                 16                    4%
#   /floor-plans-and-pricing                13                    3%
#   /floor-plans.aspx                        7                    2%
#   /floorplans.aspx                         6                    2%
#   /availability                            6                    2%
#   /Floor-plans.aspx                        5                    1%
#   /vacancies                               1                    <1%
#   /floorplan-availability                  2                    <1%
#   /check-availability                      2                    <1%
#
# The `/conventional/` segment is the Entrata Property Marketing Site URL
# scheme — `/<region>/<property-slug>/conventional/`. NOT yet handled by
# any adapter; all 108 manual-tagged sites lose because the resolver doesn't
# follow this path. Adding it here unblocks the largest cluster.
_CTA_PATH_RE = re.compile(
    r"(?:^|/)(?:"
    r"floor[-_ ]?plans?(?:[-_ ]and[-_ ]pricing)?"  # /floor-plans, /floor-plans-and-pricing
    r"|floorplans?"  # /floorplans
    r"|floorplan[-_ ]availab\w+"  # /floorplan-availability
    r"|check[-_ ]availab\w+"  # /check-availability
    r"|availab\w+"  # /availability
    r"|listings?"
    r"|pricing"
    r"|rentals?"
    r"|onlineleasing"
    r"|oleapplication"  # Entrata Online Leasing Application
    r"|guestcards?"
    r"|models?"  # /models (Greystar pattern)
    r"|vacancies"  # /vacancies (Boston Capital pattern)
    r"|conventional"  # /<region>/<slug>/conventional/ (Entrata PMS site)
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
