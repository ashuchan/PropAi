"""Build a "site-universe" of every link and API path discovered for a property.

The universe is the input the LLM ranker scores against. Once scored, the
explorer pops items in confidence order and runs targeted extraction on each.

Two callers are expected:
  - the GenericAdapter cascade, when ``JUGNU_LLM_TIER_MODE`` enables
    universe exploration (after deterministic tiers fail);
  - the experiment harness, which constructs universes from prior-run
    artefacts and replays them through both legacy and universe paths.

This module does no I/O and no LLM calls. ``gather_universe`` is a pure
function over (html, base_url, captured_api_responses, profile).
"""

from __future__ import annotations

import json
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Iterable

# ---------------------------------------------------------------------------
# Heuristics — lifted out of pms.scraper so the universe can score links
# without depending on the legacy link-hop module.
# ---------------------------------------------------------------------------

_LINK_ANCHOR_KEYWORDS: tuple[tuple[str, int], ...] = (
    ("availability", 100),
    ("floor plan", 90),
    ("floor-plan", 90),
    ("floorplan", 85),
    ("pricing", 80),
    ("rent", 70),
    ("apartment", 60),
    ("unit", 55),
    ("lease", 50),
    ("tour", 40),
    ("apply", 30),
    ("schedule", 20),
)

_LINK_PATH_KEYWORDS: tuple[tuple[str, int], ...] = (
    ("/floor-plan", 95),
    ("/floorplan", 90),
    ("/availability", 95),
    ("/pricing", 80),
    ("/apartments", 70),
    ("/rent", 60),
    ("/units", 85),
    ("/leasing", 50),
    ("/lease", 45),
    ("/floorplans", 90),
    ("/availabilities", 95),
)

# Portal subdomains usually carry a leasing widget — strong signal regardless of path.
_LINK_HOST_KEYWORDS: tuple[tuple[str, int], ...] = (
    (".rentcafe.com", 120),
    (".appfolio.com", 120),
    (".onlineleasing.realpage.com", 120),
    ("sightmap.com", 110),
    (".entrata.com", 115),
    ("commoncf.entrata.com", 115),
)

# Drop these href shapes outright — never availability pages.
_LINK_SKIP_PATTERNS: tuple[str, ...] = (
    "tel:",
    "mailto:",
    "javascript:",
    ".pdf",
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".svg",
    ".mp4",
    ".mov",
    "/blog/",
    "/news/",
    "/privacy",
    "/terms",
    "/accessibility",
    "/sitemap",
    "facebook.com/",
    "twitter.com/",
    "instagram.com/",
    "linkedin.com/",
    "youtube.com/",
    "/contact",
    "/careers",
    "/jobs",
)

# External link allow-policy: only count a link as worth scoring when its
# host is on a known leasing-portal suffix list. Everything else is captured
# but flagged as "external" — the LLM may still surface it, but the explorer
# won't auto-explore it without an explicit confidence override (see spec §13).
_PORTAL_HOST_SUFFIXES: tuple[str, ...] = tuple(suffix for suffix, _ in _LINK_HOST_KEYWORDS) + (
    ".funnelleasing.com",
    ".nestiolistings.com",
    ".mytouchtour.com",
    ".liveovation.com",
)

# Hard-deny external hosts: analytics, social, CDN, chat widgets. Never explore
# these even if the LLM mistakenly scores them high.
_EXTERNAL_HARD_DENY_SUFFIXES: tuple[str, ...] = (
    "googleapis.com",
    "google-analytics.com",
    "googletagmanager.com",
    "doubleclick.net",
    "facebook.com",
    "facebook.net",
    "instagram.com",
    "linkedin.com",
    "twitter.com",
    "youtube.com",
    "hotjar.com",
    "sentry.io",
    "go-mpulse.net",
    "visitor-analytics.io",
    "userway.org",
    "meetelise.com",
    "sierra.chat",
)


# ---------------------------------------------------------------------------
# Candidate dataclasses
# ---------------------------------------------------------------------------


@dataclass
class ApiCandidate:
    """A single captured API response, eligible for LLM-driven extraction."""

    url: str
    body: Any  # raw body — dict / list / str / bytes; carried verbatim
    base_score: int = 0  # heuristic prior (e.g. matched _has_unit_signals)
    has_unit_signals: bool = False
    preview: str = ""  # truncated stringified body for the ranking prompt
    body_bytes: int = 0
    llm_score: float | None = None  # filled by ranker
    explored: bool = False
    had_data: bool = False
    units_extracted: int = 0


@dataclass
class LinkCandidate:
    """An anchor on the entry page, eligible for sub-page exploration."""

    url: str
    anchor: str = ""
    base_score: int = 0
    is_internal: bool = True
    is_portal: bool = False
    llm_score: float | None = None
    explored: bool = False
    had_data: bool = False
    units_extracted: int = 0


@dataclass
class SiteUniverse:
    """All exploration candidates discovered for one property at one moment.

    External links are kept for visibility but the explorer's policy decides
    when (if ever) to fetch them. Profile-blocked APIs are dropped at gather
    time so the LLM never sees them.
    """

    source_page_url: str
    api_candidates: list[ApiCandidate] = field(default_factory=list)
    internal_links: list[LinkCandidate] = field(default_factory=list)
    external_links: list[LinkCandidate] = field(default_factory=list)
    blocked_api_count: int = 0  # how many APIs the profile blocklist filtered out
    skipped_link_count: int = 0  # how many anchors were rejected by skip patterns

    def all_candidates(self) -> list[Any]:
        """Return every scoreable candidate as a flat list (APIs + links)."""
        return [*self.api_candidates, *self.internal_links, *self.external_links]


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


def gather_universe(
    html: str | None,
    base_url: str,
    api_responses: list[dict[str, Any]] | None,
    profile: Any | None = None,
    preview_bytes: int = 2000,
) -> SiteUniverse:
    """Build a SiteUniverse from an entry page's HTML and captured API responses.

    Parameters
    ----------
    html
        The page HTML (decoded). May be ``None`` if only API responses exist.
    base_url
        URL of the entry page — used as the resolution base for relative
        anchors and as the same-host check for internal-vs-external split.
    api_responses
        List of ``{"url": str, "body": Any}`` dicts as captured by the L1
        fetcher. Profile-blocked URLs are filtered out here.
    profile
        Optional ``ScrapeProfile``. If present, ``api_hints.blocked_endpoints``
        are subtracted from the candidate set.
    preview_bytes
        Cap on stringified body preview attached to each ApiCandidate. Keeps
        the eventual ranking prompt bounded regardless of body size.
    """
    blocked_urls: set[str] = _collect_blocked_urls(profile)

    api_candidates, blocked_count = _build_api_candidates(
        api_responses or [],
        blocked_urls,
        preview_bytes,
    )
    internal_links, external_links, skipped_count = _build_link_candidates(html or "", base_url)

    return SiteUniverse(
        source_page_url=base_url,
        api_candidates=api_candidates,
        internal_links=internal_links,
        external_links=external_links,
        blocked_api_count=blocked_count,
        skipped_link_count=skipped_count,
    )


# ---------------------------------------------------------------------------
# API candidate construction
# ---------------------------------------------------------------------------


def _collect_blocked_urls(profile: Any | None) -> set[str]:
    """Extract the per-profile blocked-endpoint URL patterns. Defensive: any
    error returns an empty set so a bad profile never blocks gathering."""
    if profile is None:
        return set()
    out: set[str] = set()
    try:
        for be in getattr(profile.api_hints, "blocked_endpoints", []) or []:
            pat = getattr(be, "url_pattern", None) or (
                be.get("url_pattern") if isinstance(be, dict) else None
            )
            if pat:
                out.add(str(pat))
    except Exception:
        return set()
    return out


# Same signal set as :func:`ma_poc.pms.adapters.generic._has_unit_signals` —
# duplicated here so site_universe doesn't depend on the adapter module.
_UNIT_SIGNAL_KEYS: frozenset[str] = frozenset(
    {
        "rent",
        "minRent",
        "maxRent",
        "min_rent",
        "max_rent",
        "price",
        "askingRent",
        "monthlyRent",
        "baseRent",
        "bedrooms",
        "beds",
        "bed",
        "sqft",
        "squareFeet",
        "square_footage",
        "sq_ft",
        "unit_number",
        "unitNumber",
        "unit_id",
        "unitId",
        "floorPlan",
        "floor_plan",
        "floorPlanName",
        "available_date",
        "availableDate",
    }
)


def _has_unit_signals(body: Any) -> bool:
    """Lightweight signal check — does this body contain any unit-shaped keys?

    Walks at most one level into list/dict containers. Returns True if any
    item has at least two recognised signal keys (matches the same dual-key
    threshold used by the deterministic narrow parser).
    """
    items = _find_unit_list_lite(body)
    if not items:
        return False
    for item in items:
        if not isinstance(item, dict):
            continue
        hits = sum(1 for k in item.keys() if k in _UNIT_SIGNAL_KEYS)
        if hits >= 2:
            return True
    return False


def _find_unit_list_lite(body: Any) -> list[Any]:
    """Best-effort search for a list of dicts inside a JSON body. Bounded
    walk — does not recurse arbitrarily deep, since ranking only needs a
    fast yes/no signal."""
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        for v in body.values():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v
            if isinstance(v, dict):
                for vv in v.values():
                    if isinstance(vv, list) and vv and isinstance(vv[0], dict):
                        return vv
    return []


def _stringify_preview(body: Any, cap: int) -> tuple[str, int]:
    """Return ``(preview, total_bytes)`` for an API body.

    The preview is JSON-serialised when possible (more useful for the LLM
    than ``repr``). Bytes are decoded best-effort. The total-bytes count
    reflects the *full* body so the explorer can tell large from small.
    """
    if isinstance(body, (dict, list)):
        try:
            full = json.dumps(body, separators=(",", ":"), default=str)
        except Exception:
            full = str(body)
    elif isinstance(body, bytes):
        try:
            full = body.decode("utf-8", errors="replace")
        except Exception:
            full = ""
    elif isinstance(body, str):
        full = body
    elif body is None:
        full = ""
    else:
        full = str(body)

    total = len(full.encode("utf-8", errors="replace"))
    if len(full) <= cap:
        return full, total
    return full[:cap] + "...", total


def _is_json_response(content_type: str, body: Any) -> bool:
    """True when the captured response is JSON we can parse for unit data.

    Filter is dual-track:
      - Trust ``content_type`` first (Playwright captures it from response
        headers; ``application/json`` and friends are kept, everything else
        rejected).
      - When content_type is missing or generic (e.g. ``""`` from a stub
        capture), fall back to body shape: dicts and lists count as JSON,
        strings starting with ``{`` or ``[`` count as JSON.

    Anything else — text/html, JS bundles, CSS, SVG, images, tracking
    pixels, redirect bodies — never carries unit data and burns LLM
    budget if ranked. Drop it at gather time so the universe stays clean.
    """
    ct = (content_type or "").lower().strip()
    if ct:
        # Accept JSON and JSON-shaped variants (json, ld+json, vnd.api+json).
        if "json" in ct:
            return True
        # Anything else explicit is rejected — html/js/css/image/etc.
        return False
    # No content_type → fall back to body shape.
    if isinstance(body, (dict, list)):
        return True
    if isinstance(body, (bytes, bytearray)):
        try:
            sample = body[:32].decode("utf-8", errors="ignore").lstrip()
        except Exception:
            return False
        return sample.startswith(("{", "["))
    if isinstance(body, str):
        s = body.lstrip()
        return s.startswith(("{", "["))
    return False


def _build_api_candidates(
    api_responses: list[dict[str, Any]],
    blocked_urls: set[str],
    preview_bytes: int,
) -> tuple[list[ApiCandidate], int]:
    """Convert raw captured API responses into scored ApiCandidate records.

    Non-JSON responses (HTML pages, JS/CSS bundles, images, tracking
    pixels) are dropped — they never carry unit data and waste both
    ranker tokens and per-item LLM budget if kept in the universe.
    """
    candidates: list[ApiCandidate] = []
    seen_urls: set[str] = set()
    blocked = 0
    for resp in api_responses:
        url = (resp.get("url") or "").strip()
        if not url:
            continue
        if url in blocked_urls:
            blocked += 1
            continue
        if url in seen_urls:
            # Same URL captured twice — keep only the first occurrence.
            continue

        body = resp.get("body")
        if not _is_json_response(resp.get("content_type") or "", body):
            blocked += 1
            continue
        seen_urls.add(url)

        preview, total = _stringify_preview(body, preview_bytes)
        signals = _has_unit_signals(body)
        # Heuristic prior:
        #   - Body has unit-shaped keys → strong score (130). Beats every
        #     link anchor short of the absolute strongest "Availability"
        #     (anchor=100 + path=95 + portal=120 = 315) but solidly above
        #     a generic "Floor Plans" link (anchor=90 + path=0 = 90).
        #   - URL contains availability/floorplan/units keywords → +40.
        #   - No signals + no URL keywords → 0. The link-anchor floor
        #     (any "Floor Plans" or "Availability" anchor scores 90+)
        #     should outrank these in the queue, which is what the spec
        #     calls for — "links which actually claim floor plan should
        #     be rated higher even against APIs with no known patterns".
        score = 0
        if signals:
            score += 130
        url_lower = url.lower()
        for kw in (
            "/availab",
            "/floorplan",
            "/floor-plan",
            "/units",
            "/pricing",
            "/apartments",
            "/rent",
        ):
            if kw in url_lower:
                score += 40
                break

        candidates.append(
            ApiCandidate(
                url=url,
                body=body,
                base_score=score,
                has_unit_signals=signals,
                preview=preview,
                body_bytes=total,
            )
        )
    return candidates, blocked


# ---------------------------------------------------------------------------
# Link candidate construction
# ---------------------------------------------------------------------------


def _is_hard_deny_external(host: str) -> bool:
    """Drop external hosts on the analytics / social / chat blocklist."""
    if not host:
        return False
    h = host.lower()
    for suf in _EXTERNAL_HARD_DENY_SUFFIXES:
        if h == suf or h.endswith("." + suf):
            return True
    return False


def _is_portal_host(host: str) -> bool:
    if not host:
        return False
    h = host.lower()
    for suf in _PORTAL_HOST_SUFFIXES:
        if h.endswith(suf):
            return True
    return False


def _is_same_site(link_host: str, base_host: str) -> bool:
    if not link_host or not base_host:
        return False
    if link_host == base_host:
        return True
    if link_host.endswith("." + base_host):
        return True
    if base_host.endswith("." + link_host):
        return True
    return False


def _score_link(anchor: str, path: str, host: str) -> int:
    """Composite link score — anchor + path + host weights."""
    score = 0
    a = anchor.lower()
    for kw, w in _LINK_ANCHOR_KEYWORDS:
        if kw in a:
            score += w
    p = path.lower()
    for kw, w in _LINK_PATH_KEYWORDS:
        if kw in p:
            score += w
    h = host.lower()
    for suf, w in _LINK_HOST_KEYWORDS:
        if h.endswith(suf):
            score += w
    return score


def _build_link_candidates(
    html: str,
    base_url: str,
) -> tuple[list[LinkCandidate], list[LinkCandidate], int]:
    """Walk the HTML and bucket every <a href> into internal vs external."""
    if not html:
        return [], [], 0
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return [], [], 0
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        try:
            soup = BeautifulSoup(html, "html.parser")
        except Exception:
            return [], [], 0

    try:
        base = urllib.parse.urlparse(base_url)
    except Exception:
        return [], [], 0
    base_host = (base.hostname or "").lower()

    internal: dict[str, LinkCandidate] = {}
    external: dict[str, LinkCandidate] = {}
    skipped = 0

    for a in soup.find_all("a", href=True):
        raw_href = a.get("href") or ""
        href = (raw_href if isinstance(raw_href, str) else " ".join(raw_href)).strip()
        if not href:
            continue

        lower = href.lower()
        if lower.startswith("#") or lower == base_url.rstrip("/"):
            skipped += 1
            continue
        if any(skip in lower for skip in _LINK_SKIP_PATTERNS):
            skipped += 1
            continue

        try:
            resolved = urllib.parse.urljoin(base_url, href)
        except Exception:
            skipped += 1
            continue
        if not resolved.startswith(("http://", "https://")):
            skipped += 1
            continue

        try:
            parsed = urllib.parse.urlparse(resolved)
        except Exception:
            skipped += 1
            continue
        link_host = (parsed.hostname or "").lower()
        link_path = parsed.path or ""

        # Skip the entry URL itself.
        if resolved.rstrip("/") == base_url.rstrip("/"):
            skipped += 1
            continue

        anchor_text = (a.get_text(" ", strip=True) or "")[:160]
        is_portal = _is_portal_host(link_host)
        is_internal = _is_same_site(link_host, base_host)
        score = _score_link(anchor_text, link_path, link_host)

        cand = LinkCandidate(
            url=resolved,
            anchor=anchor_text,
            base_score=score,
            is_internal=is_internal,
            is_portal=is_portal,
        )

        if is_internal or is_portal:
            existing = internal.get(resolved)
            if existing is None or cand.base_score > existing.base_score:
                internal[resolved] = cand
        else:
            if _is_hard_deny_external(link_host):
                skipped += 1
                continue
            existing = external.get(resolved)
            if existing is None or cand.base_score > existing.base_score:
                external[resolved] = cand

    return list(internal.values()), list(external.values()), skipped


# ---------------------------------------------------------------------------
# Helpers exposed for the explorer / ranker
# ---------------------------------------------------------------------------


def candidate_summary_for_ranking(universe: SiteUniverse) -> list[dict[str, Any]]:
    """Flatten the universe to the minimal dicts the ranker prompt needs.

    Token-optimised: APIs send only ``{kind, url, has_unit_signals}`` —
    no preview body, no path duplication. Links send ``{kind, url,
    anchor, is_portal}``. The ranker has plenty to score on with these
    alone, and dropping the preview cut the per-property prompt from
    ~28K tokens to ~1-2K tokens for the same universe.

    APIs come first so the model sees them at the top — matches the
    spec's "APIs get more weightage" requirement.
    """
    out: list[dict[str, Any]] = []
    for c in universe.api_candidates:
        out.append(
            {
                "kind": "api",
                "url": c.url,
                "has_unit_signals": c.has_unit_signals,
            }
        )
    for c in universe.internal_links:
        out.append(
            {
                "kind": "internal_link",
                "url": c.url,
                "anchor": (c.anchor or "")[:80],
                "is_portal": c.is_portal,
            }
        )
    for c in universe.external_links:
        out.append(
            {
                "kind": "external_link",
                "url": c.url,
                "anchor": (c.anchor or "")[:80],
                "is_portal": c.is_portal,
            }
        )
    return out


def _path_of(url: str) -> str:
    try:
        return urllib.parse.urlparse(url).path or "/"
    except Exception:
        return url


def candidates_by_url(universe: SiteUniverse) -> dict[str, Any]:
    """Build a URL → candidate index so the ranker output can be applied
    back onto the universe in O(1) per result item."""
    idx: dict[str, Any] = {}
    for c in universe.all_candidates():
        idx[c.url] = c
    return idx


def apply_llm_scores(universe: SiteUniverse, scored: Iterable[dict[str, Any]]) -> int:
    """Apply ranker output to the universe in place. Returns the number of
    candidates that received an llm_score (unmatched URLs are silently
    dropped — the explorer can still fall back to base_score ordering)."""
    idx = candidates_by_url(universe)
    n = 0
    for item in scored:
        url = (item.get("url") or "").strip()
        score = item.get("confidence")
        if not url or score is None:
            continue
        cand = idx.get(url)
        if cand is None:
            continue
        try:
            cand.llm_score = float(score)
            n += 1
        except (TypeError, ValueError):
            continue
    return n
