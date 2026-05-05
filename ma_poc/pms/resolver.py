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
    }
)


@dataclass
class ResolvedTarget:
    original_url: str
    resolved_url: str
    hop_path: list[str] = field(default_factory=list)
    final_detection: DetectedPMS = field(default_factory=lambda: detect_pms(""))
    method: Literal["no_hop", "cta_link", "iframe", "redirect", "failed", "fetch_only"] = "failed"


def _get_priority(text: str) -> int:
    """Score anchor text by availability-relevance."""
    text_lower = text.lower()
    score = 0
    for keyword, priority in _PRIORITY_MAP.items():
        if keyword in text_lower:
            score = max(score, priority)
    return score


def _url_matches_pms_fingerprints(url: str) -> bool:
    """Check if a URL's host matches any adapter's static fingerprints."""
    url_lower = url.lower()
    for adapter in all_adapters():
        for fp in adapter.static_fingerprints():
            if fp in url_lower:
                return True
    return False


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

        for link in links:
            href = link.get("href", "")
            text = link.get("text", "")
            if not href or not _CTA_TEXT_RE.search(text):
                continue
            priority = _get_priority(text)
            candidates.append((priority, href, text))

        # Sort by priority descending, cap at 5
        candidates.sort(key=lambda x: -x[0])
        candidates = candidates[:5]

        # Step 3: Check each candidate for PMS fingerprint
        for _priority, href, _text in candidates:
            detection = detect_pms(href)
            if _url_matches_pms_fingerprints(href):
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
