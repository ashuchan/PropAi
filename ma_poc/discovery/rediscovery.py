"""Re-discovery queue consumer — re-derives the true current URL for DEAD_URL properties.

``reporting.verdict.Verdict.DEAD_URL`` is terminal: excluded from the
success-rate denominator and, per its docstring, "routed to a re-discovery
queue rather than the standard DLQ retry escalation." That queue had no
consumer. This module is the consumer.

A dead / trapped URL stays dead however you fetch it (proxy, render tier, or
otherwise), so recovery is NOT a re-fetch problem — it needs RE-DERIVATION of
the property's true current URL. Two complementary strategies:

  (a) MGMT_SITEMAP / MGMT_HOMEPAGE / REDIRECT_REBRAND — $0 / deterministic.
      The dead URL usually 30x-redirects to a management-company host
      (``pedcorhomes.com`` -> ``pedcorliving.com``) or to a rebranded
      property-specific host (``thehuntingtonapartments.com`` ->
      ``enjoyhuntington.com``). We probe the dead URL, follow the redirect to
      the landing host, crawl its ``sitemap.xml`` (following sitemap indexes)
      or fall back to homepage anchors, and rapidfuzz-match the property name
      to the real property page. Implemented in full; on by default.

  (b) WEB_SEARCH — higher cost, GATED (``enable_web_search=False`` by default).
      For dead-DNS domains (no redirect to follow) and hosted-vendor dead-ends
      (``notfound.apts247.info``), an external name search is the only lever.
      The hit-ranking logic lives here and is unit-tested; the search backend
      is an INJECTED dependency (``search_fn``) so the module makes no paid
      call unless an operator explicitly wires one in.

Design invariants (precision is the whole game — a false positive marks a LIVE
property permanently DEAD_URL, dropping it from the denominator and every
retry queue):

  * Match on the URL SLUG / result TITLE, never a body substring.
  * ``max(token_set_ratio, token_sort_ratio)`` scoring; WRatio is excluded
    (empirically noisy — it scored 85 on wrong slugs during calibration).
  * Accept only ``score >= _MATCH_THRESHOLD`` AND ``best - runner_up >=
    _MATCH_MARGIN``. Two near-tied high scorers => AMBIGUOUS (withheld), never
    a guess. Calibrated on the real 07-12 DEAD_URL cohort: true matches score
    100, the noise floor is ~75.
  * A management PORTFOLIO host (>= _PORTFOLIO_MIN distinct property slugs)
    REQUIRES a name match; only single/few-property landing hosts are eligible
    for the REDIRECT_REBRAND short-circuit.
"""

from __future__ import annotations

import asyncio
import logging
import re
import xml.etree.ElementTree as ET
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol
from urllib.parse import urljoin, urlparse

from rapidfuzz import fuzz

from ..pms.detector import detect_pms

log = logging.getLogger(__name__)

# ── Tuning constants (calibrated on the real 07-12 DEAD_URL cohort) ───────────
_MATCH_THRESHOLD = 90.0   # min fuzzy score to accept a name<->slug match
_MATCH_MARGIN = 12.0      # best must beat runner-up by this to disambiguate
_MAX_EXTRA_SLUG_TOKENS = 3  # a property slug never has ≫ the name's token count
_MAX_CHILD_SITEMAPS = 12  # cap on sitemap-index children we follow
_MAX_SITEMAP_URLS = 5000  # safety cap on a single host's URL pool
_REBRAND_MIN_BODY = 2048  # a real rebranded property page has substantive HTML
_REBRAND_MIN_PMS_CONF = 0.6  # detect_pms confidence to treat a landing as a real PMS site
_REBRAND_MIN_HOST_TOKEN = 4  # min length of a distinctive name token to match a host

_SITEMAP_NS = "http://www.sitemaps.org/schemas/sitemap/0.9"

_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Path segments that are property SUB-pages, not the property landing page.
# The last such segment is stripped before slug extraction so
# ``/apartments/aaron-lake/floorplans`` matches on ``aaron lake``.
_PROPERTY_SUBPAGE_SEGMENTS: frozenset[str] = frozenset(
    {
        "amenities", "floorplans", "floor-plans", "floorplan", "floor-plan",
        "gallery", "photos", "contact", "availability", "available", "apply",
        "neighborhood", "residents", "resident", "reviews", "map", "tour",
        "schedule-tour", "specials", "location", "directions", "faq", "faqs",
        "pet-policy", "virtual-tour", "3d-tour", "leasing", "index.html",
    }
)

# Hosts a dead URL redirects to that are NOT management sites — hosted-vendor
# dead-ends (soft-404 landing, non-payment lockout, security block, domain
# parking). Approach (a) cannot recover from these; they route to (b).
_DEAD_END_HOST_RE = re.compile(
    r"(?:^|\.)(?:notfound|not-found|nonpayment|non-payment|suspended|expired|parked)\b"
    r"|\.cujo\.io$"
    r"|(?:^|\.)apts247\.info$"
    r"|domainparking|sedoparking|parkingcrew",
    re.IGNORECASE,
)

# Aggregators / ILS / search / social — never the "official property site" a
# web-search fallback should return (direct-site-only house rule).
_AGGREGATOR_DOMAINS: frozenset[str] = frozenset(
    {
        "apartments.com", "zillow.com", "trulia.com", "rent.com",
        "apartmentguide.com", "apartmentlist.com", "hotpads.com",
        "realtor.com", "zumper.com", "padmapper.com", "forrent.com",
        "loopnet.com", "costar.com", "niche.com", "yelp.com",
        "facebook.com", "instagram.com", "google.com", "bing.com",
        "yahoo.com", "mapquest.com", "youtube.com", "linkedin.com",
    }
)

# Path segments that mark a page as NON-property content (news/blog/nav). A
# management site's sitemap.xml pools these alongside property pages, and a
# blog headline like ``/scully-news/leasing-has-started-at-avenir-on-fifteenth``
# fuzzy-matches a property name at 100 via token-subset — a false positive that
# would mark a live property DEAD_URL. Any path segment CONTAINING one of these
# tokens disqualifies the URL as a property candidate.
_NONPROPERTY_PATH_TOKENS: tuple[str, ...] = (
    "news", "blog", "post", "article", "story", "stories", "press", "media",
    "about", "team", "career", "job", "privacy", "terms", "policy", "legal",
    "category", "categories", "tag", "author", "event", "resource", "guide",
    "faq", "sitemap", "search", "cart", "account", "login", "signin",
)

# Anchor-path pattern for a management PORTFOLIO index. Used to discover the
# real management host when a dead URL is a splash page that merely LINKS to the
# portfolio (``pedcorhomes.com`` -> a single ``pedcorliving.com/apartments``
# anchor) rather than 30x-redirecting to it.
_PORTFOLIO_INDEX_RE = re.compile(
    r"/(?:apartments|communities|community|properties|our[-_]communities"
    r"|our[-_]properties|portfolio|locations|find[-_a-z]*(?:home|apartment|community))"
    r"(?:/|$|[?#])",
    re.IGNORECASE,
)

# Generic apartment words stripped from a name before host<->name rebrand
# matching, so only DISTINCTIVE tokens (e.g. "huntington") are tested against a
# rebranded host ("enjoyhuntington").
_GENERIC_NAME_TOKENS: frozenset[str] = frozenset(
    {
        "the", "at", "on", "of", "and", "apartments", "apartment", "apts",
        "apt", "homes", "home", "living", "residences", "residence", "lofts",
        "loft", "place", "flats", "villas", "villa", "commons", "square",
        "park", "view", "gardens", "manor", "house", "club", "landing",
    }
)


class RediscoveryMethod(StrEnum):
    """How a re-derived URL was found."""

    MGMT_SITEMAP = "MGMT_SITEMAP"          # matched via a mgmt-host sitemap.xml
    MGMT_HOMEPAGE = "MGMT_HOMEPAGE"        # matched via mgmt-host homepage anchors
    REDIRECT_REBRAND = "REDIRECT_REBRAND"  # the landing (final) URL is itself the property site
    WEB_SEARCH = "WEB_SEARCH"              # matched via the gated search backend


class RediscoveryStatus(StrEnum):
    """Outcome of a re-discovery attempt."""

    RESOLVED = "RESOLVED"                    # a re-derived URL was found
    AMBIGUOUS = "AMBIGUOUS"                  # >1 near-tied high match; withheld for precision
    NO_MATCH = "NO_MATCH"                    # crawled a host but no confident match
    NEEDS_WEB_SEARCH = "NEEDS_WEB_SEARCH"    # dead-DNS / dead-end host; (b) disabled
    SKIPPED = "SKIPPED"                      # no usable signal at all


# ── Input / output contracts ──────────────────────────────────────────────────
@dataclass(frozen=True)
class RediscoveryEntry:
    """A DEAD_URL property queued for re-discovery."""

    property_id: str
    name: str
    original_url: str
    dead_reason: str = ""   # e.g. SOFT_404 / DEAD_DNS / HTTP_404 / PARKED_DOMAIN
    city: str = ""
    state: str = ""


@dataclass(frozen=True)
class RediscoveryResult:
    """Result of a single re-discovery attempt."""

    property_id: str
    original_url: str
    status: RediscoveryStatus
    rediscovered_url: str | None = None
    method: RediscoveryMethod | None = None
    confidence: float = 0.0            # 0..1
    matched_text: str | None = None    # the slug / title we matched against
    runner_up_score: float = 0.0       # 0..100 — the second-best candidate score
    detected_pms: str | None = None    # detect_pms() on the re-derived URL (precision signal)
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a plain JSON-safe dict (one JSONL line)."""
        return {
            "property_id": self.property_id,
            "original_url": self.original_url,
            "status": self.status.value,
            "rediscovered_url": self.rediscovered_url,
            "method": self.method.value if self.method is not None else None,
            "confidence": round(self.confidence, 4),
            "matched_text": self.matched_text,
            "runner_up_score": round(self.runner_up_score, 2),
            "detected_pms": self.detected_pms,
            "notes": self.notes,
        }


# ── Fetch abstraction (injectable; default = httpx) ───────────────────────────
@dataclass(frozen=True)
class FetchedPage:
    """Minimal fetch result for re-discovery HTTP probes. Never an exception."""

    url: str
    status: int | None
    final_url: str
    body: bytes
    error: str | None = None

    @property
    def ok(self) -> bool:
        """True on a 2xx response with no transport error."""
        return self.status is not None and 200 <= self.status < 300 and not self.error


class PageFetcher(Protocol):
    """Async callable that GETs a URL following redirects. MUST NOT raise."""

    async def __call__(self, url: str) -> FetchedPage: ...  # pragma: no cover


class HttpxPageFetcher:
    """Default :class:`PageFetcher` — plain httpx GET with redirect following.

    $0 / deterministic: no proxy, no paid API. Transport failures (DNS,
    connection refused, timeout) are captured into ``FetchedPage.error`` with a
    ``None`` status so the engine can route dead-DNS cases to the web-search
    path instead of crashing.
    """

    def __init__(self, timeout_s: float = 15.0, max_bytes: int = 4_000_000) -> None:
        self._timeout_s = timeout_s
        self._max_bytes = max_bytes

    async def __call__(self, url: str) -> FetchedPage:
        import httpx

        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=self._timeout_s,
                headers={"User-Agent": _DEFAULT_UA},
            ) as client:
                resp = await client.get(url)
                return FetchedPage(
                    url=url,
                    status=resp.status_code,
                    final_url=str(resp.url),
                    body=resp.content[: self._max_bytes],
                )
        except Exception as exc:  # noqa: BLE001 — fetch errors are data, not control flow
            return FetchedPage(
                url=url, status=None, final_url=url, body=b"",
                error=f"{type(exc).__name__}: {exc}",
            )


# ── Web-search abstraction (gated; injectable) ────────────────────────────────
@dataclass(frozen=True)
class SearchHit:
    """A single web-search result (URL + title)."""

    url: str
    title: str = ""


@dataclass(frozen=True)
class RankedSearchHit:
    """A scored search hit."""

    url: str
    score: float  # 0..100
    title: str = ""


SearchFn = Callable[[str], Awaitable[Sequence[SearchHit]]]


# ── Pure helpers ──────────────────────────────────────────────────────────────
def _host(url: str) -> str:
    """Lowercase hostname of *url*, or "" on parse failure."""
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def _registrable(host: str) -> str:
    """Strip a leading ``www.`` so hosts compare on their registrable-ish name."""
    return host[4:] if host.startswith("www.") else host


def normalize_name(name: str) -> str:
    """Lowercase *name*, replace punctuation with spaces, collapse whitespace."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", (name or "").lower())).strip()


def slug_to_text(url: str) -> str:
    """Extract the property-name-bearing slug from *url*'s path.

    Strips trailing sub-page segments (``/floorplans`` etc.), de-slugifies the
    last remaining segment, and returns "" for placeholder (``%...%``), numeric,
    or single-character slugs — none of which carry a name signal.
    """
    try:
        path = urlparse(url).path or ""
    except Exception:
        return ""
    segs = [s for s in path.split("/") if s]
    while segs and segs[-1].lower() in _PROPERTY_SUBPAGE_SEGMENTS:
        segs.pop()
    if not segs:
        return ""
    last = segs[-1]
    if "%" in last:  # unexpanded sitemap template placeholder (e.g. %apartment_location%)
        return ""
    text = re.sub(r"[-_]+", " ", last).strip().lower()
    if len(text) <= 1 or text.replace(" ", "").isdigit():
        return ""
    return text


def base_property_url(url: str) -> str:
    """Strip trailing sub-page segments so ``/x/floorplans`` -> ``/x``."""
    try:
        p = urlparse(url)
    except Exception:
        return url
    segs = [s for s in (p.path or "").split("/") if s]
    changed = False
    while segs and segs[-1].lower() in _PROPERTY_SUBPAGE_SEGMENTS:
        segs.pop()
        changed = True
    if not changed:
        return url
    new_path = "/" + "/".join(segs) if segs else "/"
    return f"{p.scheme}://{p.netloc}{new_path}"


def _property_scope_key(url: str) -> tuple[str, str]:
    """Canonical host/path key used only for redirect tie validation."""
    try:
        parsed = urlparse(base_property_url(url))
    except Exception:
        return "", ""
    host = _registrable((parsed.hostname or "").lower())
    path = re.sub(r"/+", "/", parsed.path or "/").rstrip("/") or "/"
    return host, path.lower()


def _page_matches_entry_location(
    page: FetchedPage, candidate_url: str, entry: RediscoveryEntry
) -> bool:
    """Whether a substantive tied property page explicitly matches city/state."""
    if not page.ok or len(page.body) < 2_048 or not entry.city:
        return False
    try:
        raw = page.body.decode("utf-8", "replace")
    except Exception:
        return False
    city = normalize_name(entry.city)
    haystack = f" {normalize_name(candidate_url + ' ' + raw)} "
    if not city or f" {city} " not in haystack:
        return False
    state = str(entry.state or "").strip().upper()
    if not state:
        return True
    if not re.fullmatch(r"[A-Z]{2}", state):
        return False
    # Prefer explicit structured-address syntax, with a postal-address text
    # fallback. A bare two-letter substring would be far too noisy ("IN",
    # "OR", etc. are ordinary English words).
    return bool(
        re.search(
            rf"addressRegion[\"'\s:=]+[\"']?{re.escape(state)}\b",
            raw,
            re.IGNORECASE,
        )
        or re.search(
            rf"\b{re.escape(state)}\s+\d{{5}}(?:-\d{{4}})?\b",
            raw,
            re.IGNORECASE,
        )
    )


def is_nonproperty_path(url: str) -> bool:
    """True if *url*'s path contains a news/blog/nav segment (not a property page)."""
    try:
        path = (urlparse(url).path or "").lower()
    except Exception:
        return False
    segs = [s for s in path.split("/") if s]
    return any(tok in seg for seg in segs for tok in _NONPROPERTY_PATH_TOKENS)


def distinctive_name_tokens(name: str) -> list[str]:
    """Non-generic tokens of *name* (>= _REBRAND_MIN_HOST_TOKEN chars).

    Drops stop / generic apartment words so only identity-bearing tokens
    (``huntington``, ``sagamore``) remain for host<->name rebrand matching.
    """
    return [
        t
        for t in normalize_name(name).split()
        if len(t) >= _REBRAND_MIN_HOST_TOKEN and t not in _GENERIC_NAME_TOKENS
    ]


def host_matches_name(host: str, name: str) -> bool:
    """True when a (rebranded) *host* plausibly belongs to *name*.

    A distinctive name token is a substring of the host stem
    (``huntington`` in ``enjoyhuntington``), or the whole compacted name
    fuzzily overlaps the host stem. Precision-first: needs a real, distinctive
    token — generic words alone never match.
    """
    stem = _registrable(host).rsplit(".", 1)[0].replace("-", "")
    if not stem:
        return False
    tokens = distinctive_name_tokens(name)
    if any(t in stem for t in tokens):
        return True
    if not tokens:
        return False
    compact = "".join(tokens)
    return float(fuzz.partial_ratio(compact, stem)) >= 90.0


def match_score(name: str, slug_text: str) -> float:
    """Precision-first fuzzy score in [0, 100]: ``max(token_set, token_sort)``.

    WRatio is deliberately excluded — during calibration on the real cohort it
    assigned 85 to clearly-wrong slugs (partial-substring artefacts), which
    would breach the false-positive budget.
    """
    if not name or not slug_text:
        return 0.0
    n = normalize_name(name)
    return float(
        max(
            fuzz.token_set_ratio(n, slug_text),
            fuzz.token_sort_ratio(n, slug_text),
        )
    )


def _local(tag: str) -> str:
    """Local (namespace-stripped) name of an XML tag."""
    return tag.rsplit("}", 1)[-1]


def parse_sitemap(body: bytes) -> tuple[list[str], list[str]]:
    """Parse sitemap XML. Returns ``(page_urls, child_sitemap_urls)``.

    Handles both ``<urlset>`` (page URLs) and ``<sitemapindex>`` (child
    sitemaps), namespaced or not. Malformed XML yields ``([], [])``.
    """
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return [], []
    pages: list[str] = []
    children: list[str] = []
    for el in root.iter():
        lname = _local(el.tag)
        if lname not in ("url", "sitemap"):
            continue
        loc = _find_loc(el)
        if not loc:
            continue
        if lname == "url":
            pages.append(loc)
        else:
            children.append(loc)
    return pages, children


def _find_loc(el: ET.Element) -> str | None:
    """First ``<loc>`` text under *el*, or None."""
    for child in el:
        if _local(child.tag) == "loc" and child.text:
            return child.text.strip()
    return None


_HREF_RE = re.compile(r'href\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE)


def extract_anchor_hrefs(html: bytes, base_url: str) -> list[str]:
    """Absolute http(s) hrefs from *html*, resolved against *base_url* (deduped)."""
    try:
        text = html.decode("utf-8", "replace")
    except Exception:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for m in _HREF_RE.finditer(text):
        raw = m.group(1).strip()
        if not raw or raw.startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
            continue
        absu = urljoin(base_url, raw)
        if not absu.startswith(("http://", "https://")) or absu in seen:
            continue
        seen.add(absu)
        out.append(absu)
    return out


def build_search_query(entry: RediscoveryEntry) -> str:
    """Compose the web-search query for a property (``<name> <city> <state> apartments``)."""
    parts = [entry.name, entry.city, entry.state, "apartments"]
    return re.sub(r"\s+", " ", " ".join(p for p in parts if p)).strip()


def rank_search_hits(entry: RediscoveryEntry, hits: Iterable[SearchHit]) -> list[RankedSearchHit]:
    """Rank search hits by name match (title + domain), excluding junk.

    Drops aggregators / ILS / social / search hosts and the property's own
    known-dead domain. Scores on ``max(title match, domain partial-match)`` so
    a rebranded domain (``livehuntington.com``) still scores on the shared
    ``huntington`` stem. Deterministic — sorted by score then URL.
    """
    dead_host = _registrable(_host(entry.original_url))
    name_compact = normalize_name(entry.name).replace(" ", "")
    ranked: list[RankedSearchHit] = []
    for hit in hits:
        host = _registrable(_host(hit.url))
        if not host or host in _AGGREGATOR_DOMAINS or host == dead_host:
            continue
        title_score = match_score(entry.name, normalize_name(hit.title)) if hit.title else 0.0
        host_stem = host.rsplit(".", 1)[0]  # drop TLD
        domain_score = float(fuzz.partial_ratio(name_compact, host_stem)) if name_compact else 0.0
        ranked.append(RankedSearchHit(url=hit.url, score=max(title_score, domain_score), title=hit.title))
    ranked.sort(key=lambda r: (-r.score, r.url))
    return ranked


# ── Engine ────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class _Candidate:
    url: str
    slug_text: str
    score: float
    source: RediscoveryMethod


class RediscoveryEngine:
    """Re-derives true property URLs for DEAD_URL properties (approaches a + b)."""

    def __init__(
        self,
        fetcher: PageFetcher | None = None,
        *,
        match_threshold: float = _MATCH_THRESHOLD,
        match_margin: float = _MATCH_MARGIN,
        enable_web_search: bool = False,
        search_fn: SearchFn | None = None,
    ) -> None:
        self._fetcher: PageFetcher = fetcher or HttpxPageFetcher()
        self._threshold = match_threshold
        self._margin = match_margin
        self._enable_web_search = enable_web_search
        self._search_fn = search_fn

    async def rediscover_many(
        self, entries: Sequence[RediscoveryEntry], concurrency: int = 6
    ) -> list[RediscoveryResult]:
        """Re-discover a batch concurrently (bounded by *concurrency*)."""
        sem = asyncio.Semaphore(max(1, concurrency))

        async def _one(e: RediscoveryEntry) -> RediscoveryResult:
            async with sem:
                return await self.rediscover(e)

        return list(await asyncio.gather(*[_one(e) for e in entries]))

    async def rediscover(self, entry: RediscoveryEntry) -> RediscoveryResult:
        """Re-derive the true current URL for a single DEAD_URL property."""
        orig_host = _registrable(_host(entry.original_url))

        # 1. Probe the dead URL: where does it land, or is DNS itself dead?
        page = await self._fetcher(entry.original_url)

        # 1a. Transport failure (DNS/conn/timeout) => nothing to crawl => (b).
        if page.status is None:
            return await self._web_search_fallback(entry, "DEAD_DNS", page.error or "")

        final_host = _registrable(_host(page.final_url))

        # 1b. Redirected to a hosted-vendor dead-end host => (b).
        if final_host and _DEAD_END_HOST_RE.search(final_host):
            return await self._web_search_fallback(entry, "DEAD_END_HOST", final_host)

        crawl_host = final_host or orig_host
        if not crawl_host:
            return RediscoveryResult(
                property_id=entry.property_id,
                original_url=entry.original_url,
                status=RediscoveryStatus.SKIPPED,
                notes="no usable host",
            )

        # Path 1 — name-match against the landing host (sitemap + homepage anchors).
        candidates = await self._collect_candidates(entry, crawl_host, page)
        resolved = await self._resolve_from_candidates(entry, candidates, crawl_host)
        if resolved is not None:
            return resolved

        # Path 2 — REDIRECT_REBRAND: the landing host IS the rebranded property
        # (its host name carries the property name, e.g. enjoyhuntington).
        rebrand = self._maybe_rebrand(entry, page, orig_host, final_host)
        if rebrand is not None:
            return rebrand

        # Path 3 — splash page that LINKS to a single management portfolio host
        # (pedcorhomes.com -> one pedcorliving.com/apartments anchor). Crawl it.
        secondary = _external_portfolio_host(page, {orig_host, crawl_host})
        if secondary is not None:
            sec_candidates = await self._collect_candidates(entry, secondary, page)
            resolved = await self._resolve_from_candidates(entry, sec_candidates, secondary)
            if resolved is not None:
                return resolved

        best_score = candidates[0].score if candidates else 0.0
        return await self._web_search_fallback(
            entry, "NO_HOST_MATCH", f"{crawl_host} best={best_score:.0f}"
        )

    async def _resolve_from_candidates(
        self,
        entry: RediscoveryEntry,
        candidates: list[_Candidate],
        host: str,
    ) -> RediscoveryResult | None:
        """RESOLVED / AMBIGUOUS from a scored candidate pool, or None if no match clears threshold."""
        if not candidates:
            return None
        best = candidates[0]
        runner_up = candidates[1].score if len(candidates) > 1 else 0.0
        if best.score < self._threshold:
            return None
        # Two near-tied high scorers => withhold rather than guess (precision).
        if runner_up >= self._threshold and (best.score - runner_up) < self._margin:
            redirect_winner = await self._redirect_disambiguate(entry, candidates)
            if redirect_winner is not None:
                return self._resolved(
                    entry,
                    redirect_winner.url,
                    redirect_winner.source,
                    redirect_winner.score / 100.0,
                    redirect_winner.slug_text,
                    runner_up,
                    host,
                )
            return RediscoveryResult(
                property_id=entry.property_id,
                original_url=entry.original_url,
                status=RediscoveryStatus.AMBIGUOUS,
                matched_text=best.slug_text,
                runner_up_score=runner_up,
                notes=f"top two within margin on {host}",
            )
        return self._resolved(entry, best.url, best.source, best.score / 100.0,
                              best.slug_text, runner_up, host)

    async def _redirect_disambiguate(
        self, entry: RediscoveryEntry, candidates: list[_Candidate]
    ) -> _Candidate | None:
        """Resolve a lexical tie only when redirects disprove every rival.

        Portfolio sitemaps sometimes retain both a stale short slug and its
        current canonical slug.  Name matching correctly ties them (for
        example ``harbour-pointe`` and ``harbour-pointe-apartment-homes``),
        while a live GET proves the stale URL redirects to a generic state
        index and the canonical URL remains property-scoped.

        This stays precision-first: transport errors/non-2xx responses are
        UNKNOWN, not negative evidence.  A winner is returned only when one
        candidate successfully remains in its own property scope and every
        other near-tied candidate successfully redirects out of scope.
        """
        if len(candidates) < 2:
            return None
        best_score = candidates[0].score
        tied = [
            candidate
            for candidate in candidates
            if candidate.score >= self._threshold
            and (best_score - candidate.score) < self._margin
        ][:4]
        if len(tied) < 2:
            return None

        valid: list[tuple[_Candidate, FetchedPage]] = []
        invalid = 0
        unknown = 0
        for candidate in tied:
            page = await self._fetcher(candidate.url)
            if not page.ok or not page.final_url:
                unknown += 1
                continue
            if _property_scope_key(candidate.url) == _property_scope_key(page.final_url):
                valid.append((candidate, page))
            else:
                invalid += 1
        if len(valid) == 1 and invalid == len(tied) - 1 and unknown == 0:
            return valid[0][0]

        # A genuine same-name portfolio tie can survive redirect validation
        # (Harbour Pointe in Bradenton, FL vs Harbor Pointe in Moultrie, GA).
        # The input already carries city/state. Accept one candidate only when
        # its substantive property page explicitly carries BOTH location
        # signals and every tied fetch completed, otherwise keep withholding.
        if len(valid) > 1 and unknown == 0 and entry.city:
            location_matches = [
                candidate
                for candidate, page in valid
                if _page_matches_entry_location(page, candidate.url, entry)
            ]
            if len(location_matches) == 1:
                return location_matches[0]
        return None

    # ── candidate collection ──────────────────────────────────────────────────
    async def _collect_candidates(
        self, entry: RediscoveryEntry, crawl_host: str, landing: FetchedPage
    ) -> list[_Candidate]:
        """Sitemap + homepage-anchor candidate pool, scored and deduped by slug."""
        method_by_url: dict[str, RediscoveryMethod] = {}
        pool: list[str] = []

        sitemap_urls = await self._fetch_sitemap_urls(crawl_host)
        for u in sitemap_urls:
            method_by_url.setdefault(u, RediscoveryMethod.MGMT_SITEMAP)
            pool.append(u)

        # Homepage-anchor fallback when the host has no usable sitemap
        # (e.g. dermotcompany.com serves 404 for /sitemap.xml).
        if not sitemap_urls:
            home_html, home_url = await self._homepage_html(crawl_host, landing)
            for u in extract_anchor_hrefs(home_html, home_url):
                if _registrable(_host(u)) == crawl_host:  # same-host only
                    method_by_url.setdefault(u, RediscoveryMethod.MGMT_HOMEPAGE)
                    pool.append(u)

        name_token_count = len(normalize_name(entry.name).split())
        best_by_slug: dict[str, _Candidate] = {}
        for u in pool:
            # Drop news/blog/nav pages: a long headline containing the property
            # name token-subset-matches at 100 and would be a false positive.
            if is_nonproperty_path(u):
                continue
            base = base_property_url(u)
            st = slug_to_text(base)
            if not st:
                continue
            # Skip index / nav slugs made only of generic apartment words: a
            # bare ``/apartments`` index yields slug "apartments", which
            # token-subset-matches "<Name> Apartments" at 100 and would tie a
            # real property, forcing a spurious AMBIGUOUS.
            if all(tok in _GENERIC_NAME_TOKENS for tok in st.split()):
                continue
            # A real property slug is never much longer than its name. This
            # backstops any non-property page the path filter missed.
            if len(st.split()) > name_token_count + _MAX_EXTRA_SLUG_TOKENS:
                continue
            score = match_score(entry.name, st)
            prev = best_by_slug.get(st)
            if prev is None or score > prev.score:
                best_by_slug[st] = _Candidate(
                    url=base, slug_text=st, score=score,
                    source=method_by_url.get(u, RediscoveryMethod.MGMT_SITEMAP),
                )
        return sorted(best_by_slug.values(), key=lambda c: c.score, reverse=True)

    async def _fetch_sitemap_urls(self, host: str) -> list[str]:
        """Fetch + parse a host's sitemap, following sitemap-index children once."""
        seen: set[str] = set()
        pages: list[str] = []
        for candidate in (f"https://{host}/sitemap.xml", f"https://{host}/sitemap_index.xml"):
            page = await self._fetcher(candidate)
            if not page.ok or not page.body:
                continue
            child_pages, children = parse_sitemap(page.body)
            _extend_unique(pages, child_pages, seen, _MAX_SITEMAP_URLS)
            for child_url in children[:_MAX_CHILD_SITEMAPS]:
                if len(pages) >= _MAX_SITEMAP_URLS:
                    break
                child = await self._fetcher(child_url)
                if not child.ok or not child.body:
                    continue
                grand_pages, _ = parse_sitemap(child.body)
                _extend_unique(pages, grand_pages, seen, _MAX_SITEMAP_URLS)
            if pages:
                break  # first sitemap that yielded URLs wins
        return pages

    async def _homepage_html(self, host: str, landing: FetchedPage) -> tuple[bytes, str]:
        """Return (html, base_url) for *host*'s homepage, reusing *landing* if it is one."""
        landing_path = urlparse(landing.final_url).path or "/"
        if (
            landing.ok
            and landing.body
            and _registrable(_host(landing.final_url)) == host
            and landing_path in ("", "/")
        ):
            return landing.body, landing.final_url
        home = await self._fetcher(f"https://{host}/")
        if home.ok and home.body:
            return home.body, home.final_url
        return b"", f"https://{host}/"

    # ── rebrand short-circuit ─────────────────────────────────────────────────
    def _maybe_rebrand(
        self,
        entry: RediscoveryEntry,
        landing: FetchedPage,
        orig_host: str,
        final_host: str,
    ) -> RediscoveryResult | None:
        """Accept the landing URL when it is the property's own rebranded PMS site.

        Guards (all required): a real cross-host redirect happened; the landing
        is content-bearing; the landing HOST carries the property's distinctive
        name (``huntington`` in ``enjoyhuntington`` — this is what separates a
        rebrand from a shared management-portfolio host like ``pedcorliving``);
        and ``detect_pms`` confirms a real leasing platform. The host-name gate
        keeps portfolio hosts on the name-match path where they belong.
        """
        if not final_host or final_host == orig_host:
            return None
        if not landing.ok or len(landing.body) < _REBRAND_MIN_BODY:
            return None
        if not host_matches_name(final_host, entry.name):
            return None
        html = landing.body.decode("utf-8", "replace")
        det = detect_pms(landing.final_url, None, html)
        if det.confidence < _REBRAND_MIN_PMS_CONF:
            return None
        return RediscoveryResult(
            property_id=entry.property_id,
            original_url=entry.original_url,
            status=RediscoveryStatus.RESOLVED,
            rediscovered_url=landing.final_url,
            method=RediscoveryMethod.REDIRECT_REBRAND,
            confidence=round(det.confidence, 4),
            matched_text=final_host,
            detected_pms=_pms_name(det),
            notes=f"cross-host rebrand {orig_host} -> {final_host}",
        )

    # ── web-search fallback (approach b, gated) ───────────────────────────────
    async def _web_search_fallback(
        self, entry: RediscoveryEntry, reason: str, detail: str
    ) -> RediscoveryResult:
        """Approach (b): injected search backend. Returns NEEDS_WEB_SEARCH when disabled."""
        if not self._enable_web_search or self._search_fn is None:
            return RediscoveryResult(
                property_id=entry.property_id,
                original_url=entry.original_url,
                status=RediscoveryStatus.NEEDS_WEB_SEARCH,
                notes=f"{reason}: {detail}".strip(": "),
            )
        query = build_search_query(entry)
        try:
            hits = await self._search_fn(query)
        except Exception as exc:  # noqa: BLE001 — a search-backend error must not crash the batch
            log.warning("web-search backend failed for %s: %s", entry.property_id, exc)
            return RediscoveryResult(
                property_id=entry.property_id,
                original_url=entry.original_url,
                status=RediscoveryStatus.NO_MATCH,
                notes=f"web_search backend error ({reason})",
            )
        ranked = rank_search_hits(entry, hits)
        if ranked and ranked[0].score >= self._threshold:
            top = ranked[0]
            runner_up = ranked[1].score if len(ranked) > 1 else 0.0
            return RediscoveryResult(
                property_id=entry.property_id,
                original_url=entry.original_url,
                status=RediscoveryStatus.RESOLVED,
                rediscovered_url=top.url,
                method=RediscoveryMethod.WEB_SEARCH,
                confidence=round(top.score / 100.0, 4),
                matched_text=top.title or _host(top.url),
                runner_up_score=runner_up,
                notes=f"web_search ({reason})",
            )
        return RediscoveryResult(
            property_id=entry.property_id,
            original_url=entry.original_url,
            status=RediscoveryStatus.NO_MATCH,
            notes=f"web_search: no confident hit ({reason})",
        )

    # ── helpers ───────────────────────────────────────────────────────────────
    def _resolved(
        self,
        entry: RediscoveryEntry,
        url: str,
        method: RediscoveryMethod,
        confidence: float,
        matched_text: str,
        runner_up: float,
        host: str,
    ) -> RediscoveryResult:
        """Build a RESOLVED result, attaching a detect_pms precision signal."""
        det = detect_pms(url, None, None)
        return RediscoveryResult(
            property_id=entry.property_id,
            original_url=entry.original_url,
            status=RediscoveryStatus.RESOLVED,
            rediscovered_url=url,
            method=method,
            confidence=round(confidence, 4),
            matched_text=matched_text,
            runner_up_score=runner_up,
            detected_pms=_pms_name(det) if det.confidence > 0 else None,
            notes=f"name-matched on {host} (from {_registrable(_host(entry.original_url))})",
        )


def _extend_unique(dst: list[str], src: Iterable[str], seen: set[str], cap: int) -> None:
    """Append items from *src* to *dst* that are not in *seen*, up to *cap* total."""
    for item in src:
        if len(dst) >= cap:
            return
        if item in seen:
            continue
        seen.add(item)
        dst.append(item)


def _pms_name(det: Any) -> str | None:
    """Best-effort provider label from a DetectedPMS (``.pms`` may be a StrEnum)."""
    pms = getattr(det, "pms", None)
    if pms is None:
        return None
    return getattr(pms, "value", str(pms))


def _external_portfolio_host(landing: FetchedPage, exclude: set[str]) -> str | None:
    """The single external management-portfolio host *landing* links to, or None.

    Handles the splash-page trap (``pedcorhomes.com`` serves a 200 page whose
    only useful content is one ``pedcorliving.com/apartments`` link). Returns a
    host ONLY when exactly one distinct external host is linked via a
    portfolio-index path — ambiguity (zero or many) yields None so we never
    wander onto a lead-gen vendor or a second unrelated site.
    """
    if not landing.ok or not landing.body:
        return None
    hosts: set[str] = set()
    for href in extract_anchor_hrefs(landing.body, landing.final_url):
        host = _registrable(_host(href))
        if not host or host in exclude or host in _AGGREGATOR_DOMAINS:
            continue
        if _DEAD_END_HOST_RE.search(host):
            continue
        try:
            path = urlparse(href).path or ""
        except Exception:
            continue
        if _PORTFOLIO_INDEX_RE.search(path):
            hosts.add(host)
    return next(iter(hosts)) if len(hosts) == 1 else None


__all__ = [
    "FetchedPage",
    "HttpxPageFetcher",
    "PageFetcher",
    "RankedSearchHit",
    "RediscoveryEngine",
    "RediscoveryEntry",
    "RediscoveryMethod",
    "RediscoveryResult",
    "RediscoveryStatus",
    "SearchFn",
    "SearchHit",
    "base_property_url",
    "build_search_query",
    "extract_anchor_hrefs",
    "match_score",
    "normalize_name",
    "parse_sitemap",
    "rank_search_hits",
    "slug_to_text",
]
