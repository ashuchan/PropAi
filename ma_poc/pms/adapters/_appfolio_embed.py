"""Shared AppFolio-embed recovery for syndication (Wix/Squarespace) shells.

2026-05-19 deep-probe finding: the second-largest recoverable failed-case
cluster (~26 properties) is Wix/Squarespace marketing shells that the
detector dead-ends as ``SYNDICATION_ONLY_*`` even though the real unit data
is one labelled-nav hop deep, embedded as a **cross-origin iframe** to the
AppFolio tenant subdomain:

    <iframe src="https://{tenant}.appfolio.com/listings?..."> ...

Verified live on brooksidejohnsoncreek.com (Squarespace) → ``/listings``
sub-page → ``illumepm.appfolio.com/listings`` iframe = 69 ``data-listing-id``
blocks / 1097 ``js-listing-*`` nodes — exactly what
``parse_appfolio_listings_ssr`` already extracts. The capability exists; the
gap is purely routing. This module is the self-contained recovery: scan the
live page, else probe the well-known sub-paths, find the AppFolio iframe,
fetch it in-session, and hand the HTML to the existing SSR parser.

The fetch runs through ``page.evaluate`` so the page's own origin/session is
reused (same pattern as the Entrata known-endpoint probe).
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING
from urllib.parse import urlparse, urlunparse

from ma_poc.pms.adapters.appfolio import (
    ScopeEvidence,
    filter_listings_by_property_address,
    find_appfolio_property_group,
    parse_appfolio_listings_ssr,
)
from ma_poc.pms.appfolio_urls import (
    is_listings_index_url,
    is_scoped_listings_url,
    property_list_scope,
    scoped_listings_url,
)

if TYPE_CHECKING:
    from playwright.async_api import Page

    from ma_poc.pms.adapters.base import AdapterContext

log = logging.getLogger(__name__)

# AppFolio tenant-subdomain listings iframe/script, e.g.
# https://illumepm.appfolio.com/listings?... — the host is the tenant slug,
# never bare ``appfolio.com``. ``/listings`` is the canonical SSR path.
_APPFOLIO_IFRAME_RE = re.compile(
    r"""https?://[a-z0-9][a-z0-9-]*\.appfolio\.com/listings[^\s"'<>]*""",
    re.IGNORECASE,
)
# Real-world variants captured by the regex above include:
#   {tenant}.appfolio.com/listings                 ← listings SSR index (what we want)
#   {tenant}.appfolio.com/listings?orderable=true  ← listings SSR with filters
#   {tenant}.appfolio.com/listings/detail/{uuid}   ← single-listing detail
#   {tenant}.appfolio.com/listings/showings/new?listable_uid=...  ← "schedule a
#       showing" form — 200 OK but no listings markup, must NOT be fetched
# Canonicalize: strip path after ``/listings`` so we always hit the SSR index.
# 2026-05-19 v2: 100-sample validation surfaced 3 sites with showings/new
# anchor links (no real iframe), where the parser correctly returned 0 units
# but the fetch wasted a round-trip. Canonical URL = ``{scheme}://{host}/listings``.
_APPFOLIO_LISTINGS_HOST_RE = re.compile(
    r"https?://[a-z0-9][a-z0-9-]*\.appfolio\.com/listings",
    re.IGNORECASE,
)


def _to_appfolio_listings_root(url: str) -> str:
    """Strip any path after ``/listings`` (e.g. ``/showings/new``) so we
    fetch the listings SSR index, not a request-a-tour form.

    2026-07-28: the property-list filter survives the strip. Everything else
    in the query string (cache-buster, ``theme_color``, ``order_by``) is
    cosmetic and still dropped.

    2026-07-28 — the shape rule itself is NOT restated here. Whether a URL is
    an operator-published index lives in one place,
    :func:`ma_poc.pms.appfolio_urls.is_listings_index_url`; this function only
    canonicalises. Three copies of that rule is how the AppFolio scope
    predicates drifted apart in the first place.
    """
    m = _APPFOLIO_LISTINGS_HOST_RE.match(url)
    if not m:
        return url
    root = m.group(0)
    scope = property_list_scope(url)
    return f"{root}?filters%5Bproperty_list%5D={scope}" if scope else root


# 2026-07-28 — sub-paths to read the ``propertyGroup`` off when the ENTRY body
# doesn't carry it. Measured over the 32 cohort properties whose scoped surface
# was found by hand: 11 on the captured entry page, 15 on /availability, 5 on
# /floor-plans, 1 on /available-units. The pipeline only ever looked at the
# entry body, which is the whole reason ``find_appfolio_property_group()`` kept
# returning None and the recovery kept falling through to the account roster.
# Deliberately short — this probe fires at page=None (curl_cffi, ~1 req/s) and
# only for properties that already showed an AppFolio tenant reference.
_APPFOLIO_GROUP_SUBPATHS: tuple[str, ...] = (
    "/availability",
    "/floor-plans",
    "/listings",
    "/available-units",
)

# Sub-paths a marketing shell uses for the page that embeds the AppFolio
# widget. Ordered by observed frequency in the 2026-05-19 probe. Kept tight
# — every entry was seen on a real failed-case property.
_APPFOLIO_EMBED_SUBPATHS: tuple[str, ...] = (
    "/listings",
    "/availability",
    "/available-units",
    "/availableunits",
    "/properties-for-rent",
    "/available-rentals",
    "/floorplans",
    "/floor-plans",
    "/apartments",
    "/rentals",
)

# JS run on the live page: harvest any AppFolio iframe/script URL already
# present (the cheap path — fires when the shell embeds the widget on the
# page we're already on).
_LIVE_APPFOLIO_SRC_JS = r"""
() => {
  const out = [];
  document.querySelectorAll('iframe, script').forEach((el) => {
    const s = el.src || '';
    if (/\.appfolio\.com\/listings/i.test(s)) out.push(s);
  });
  return out;
}
"""
# 2026-05-20 fallback (probe finding from feature_fail_1429 grind):
# Wix shells often have *no* ``/listings`` iframe anywhere — only an
# auth link like ``{tenant}.appfolio.com/connect/users/sign_in`` (or
# /request_access) in the footer. Same tenant subdomain, different path.
# This scan harvests ANY ``*.appfolio.com/*`` URL from anchors as well as
# iframes/scripts. The Python side extracts the tenant slug and
# constructs the canonical ``https://{tenant}.appfolio.com/listings``.
# Verified live on aptsedenprairie / aptslindenpark / rentdwp (the top
# three SYNDICATION_ONLY_WIX→AppFolio cases worth 712 strict units).
# Sentinel ``tenant-scan`` in the source lets test FakePages dispatch.
_LIVE_APPFOLIO_TENANT_JS = r"""
() => {
  // tenant-scan: any *.appfolio.com URL on the page (anchors + frames)
  const out = [];
  document.querySelectorAll('a, iframe, script').forEach((el) => {
    const s = el.href || el.src || '';
    if (/\.appfolio\.com\//i.test(s)) out.push(s);
  });
  return out;
}
"""

# Extract the tenant subdomain from any ``*.appfolio.com/*`` URL.
# Captures ``bendermanagement`` from
# ``https://bendermanagement.appfolio.com/connect/users/sign_in``.
_APPFOLIO_TENANT_HOST_RE = re.compile(
    r"https?://([a-z0-9][a-z0-9-]*)\.appfolio\.com/",
    re.IGNORECASE,
)


def _tenant_listings_url(any_appfolio_url: str) -> str | None:
    """Extract tenant slug from any AppFolio URL and synthesize the
    canonical ``https://{tenant}.appfolio.com/listings`` root. Returns
    ``None`` if the URL doesn't match the tenant-host pattern.
    """
    m = _APPFOLIO_TENANT_HOST_RE.match(any_appfolio_url)
    if not m:
        return None
    tenant = m.group(1).lower()
    return f"https://{tenant}.appfolio.com/listings"


def _origin(page: Page, ctx: AdapterContext) -> str:
    """scheme://host for sub-path probing — prefer the settled page URL."""
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


async def _fetch_with_status(page: Page, url: str) -> tuple[int, str]:
    """Fetch *url* in-session via page.evaluate. Returns ``(status, body)``.

    Status is 0 when the network call raised (DNS error, refused, etc.) or
    the page object can't evaluate JS. Body is ``''`` on any non-2xx so
    callers don't accidentally parse a bot-wall HTML payload.

    The dict-shape JS is the new wire format. The string-shape fallback
    keeps existing test ``evaluate`` mocks (which return body strings
    keyed by URL) working unchanged — they're treated as ``(200, body)``
    when the body is non-empty, ``(0, '')`` when empty.
    """
    evaluate = getattr(page, "evaluate", None)
    if not callable(evaluate):
        # page=None (production dispatch): fall back to a cheap curl_cffi GET so
        # the sub-path probing still works. Task #37 Track 1.
        from ma_poc.pms.adapters._probe import probe_fetch_status

        return await probe_fetch_status(url)
    try:
        result = await evaluate(
            "(u) => fetch(u, {credentials: 'include'})"
            ".then(r => r.text().then(b => ({status: r.status, body: r.ok ? b : ''})))"
            ".catch(() => ({status: 0, body: ''}))",
            url,
        )
    except Exception as exc:  # pragma: no cover — network/SDK variance
        log.debug("AppFolio-embed fetch failed url=%s err=%s", url, exc)
        return 0, ""
    if isinstance(result, dict):
        try:
            status = int(result.get("status") or 0)
        except (TypeError, ValueError):
            status = 0
        body = result.get("body")
        return status, body if isinstance(body, str) else ""
    if isinstance(result, str):
        return (200, result) if result else (0, "")
    return 0, ""


async def _fetch(page: Page, url: str) -> str:
    """Body-only convenience wrapper for callers that don't need status."""
    _, body = await _fetch_with_status(page, url)
    return body


def _property_group_in(html: str) -> str:
    """Read the AppFolio widget's per-property ``propertyGroup`` out of *html*.

    Covers both shapes seen in the cohort: the plain ``Appfolio.Listing({
    hostUrl: ..., propertyGroup: 'COLLEGE PARK' })`` embed JS, and the
    base64-encoded widget config that AppFolio Websites (Duda) pages carry.
    """
    if not html:
        return ""
    group = find_appfolio_property_group(html)
    if group and group.strip():
        return group.strip()
    try:
        from ma_poc.pms.adapters._appfolio_websites_duda import (
            extract_appfolio_websites_property_group,
        )

        duda_group = extract_appfolio_websites_property_group(html)
    except Exception:  # pragma: no cover — defensive
        duda_group = None
    return duda_group.strip() if duda_group and duda_group.strip() else ""


async def _discover_property_group(
    page: Page,
    ctx: AdapterContext,
    entry_body: str,
    already_fetched: dict[str, str] | None = None,
) -> tuple[str, str]:
    """Find the property's ``propertyGroup`` scope. Returns ``(group, source)``.

    Looks in the entry body first (free), then in any sub-page body the iframe
    probe already pulled down (also free), and only then spends a request on
    the short sub-path list where the group actually lives on real sites.
    Returns ``("", "")`` when the property publishes no per-property scope.
    """
    group = _property_group_in(entry_body)
    if group:
        return group, "entry_body"

    seen = already_fetched or {}
    for url, html in seen.items():
        group = _property_group_in(html)
        if group:
            return group, url

    origin = _origin(page, ctx)
    if not origin:
        return "", ""

    # 2026-07-28 (QC finding D): this probe was origin-level, so when the
    # property lives at a SUB-PATH of a shared management-company site it walked
    # the PMC's own pages instead of the property's. Danish Village is
    # ``postroadmgmt.com/available-properties/``; probing
    # ``postroadmgmt.com/availability`` reads whatever scope the PMC declares
    # there — some other property's. A group discovered off a page this property
    # does not own is worse than no group: it silently scopes the roster to the
    # wrong listing set, and the result looks like a clean scoped answer.
    # 30 of the 115 cohort properties sit at such a sub-path, so probe THEIR
    # directory instead of the root. Same request count either way.
    base = (getattr(ctx, "base_url", "") or "").split("?")[0].split("#")[0]
    root = base.rstrip("/") if urlparse(base).path.strip("/") else origin

    for path in _APPFOLIO_GROUP_SUBPATHS:
        url = root + path
        if url in seen:
            continue
        html = await _fetch(page, url)
        if not html:
            continue
        group = _property_group_in(html)
        if group:
            return group, url
    return "", ""


async def recover_appfolio_embed(
    page: Page,
    ctx: AdapterContext,
) -> list[dict[str, str]]:
    """Find an embedded AppFolio listings widget and parse it.

    Returns SSR-parsed unit dicts, or ``[]`` when no AppFolio embed is
    discoverable (so a genuine no-PMS Wix/Squarespace site is unaffected).
    Never raises.

    2026-07-28 — SCOPE OR DECLINE. A ``*.appfolio.com`` reference identifies
    the MANAGEMENT COMPANY, not the property: on collegeparklacey.com the sole
    breadcrumb is a ``/connect/users/sign_in`` resident-portal link, and this
    function used to answer it with olympicmanagement's entire 294-unit
    account roster — the same roster it also handed to six other Washington
    communities. Ground truth for that property is 4 listings. The evidence is
    still good (it does find the right tenant); what was missing was any check
    that the roster belongs here. Order of preference now:

      1. a ``filters[property_list]`` scope already on the discovered URL, or a
         ``propertyGroup`` read off the entry body / the availability sub-page
         — AppFolio filters those server-side, so the answer is authoritative
         and is returned verbatim (INCLUDING an empty one: "no vacancies" is a
         real answer, not a reason to widen the query);
      2. otherwise the roster is admitted only through the same address filter
         the VANITY path already applies, at the evidence grade the discovery
         actually earned. A ``/listings`` INDEX the operator put on the
         property's OWN page is ``ScopeEvidence.PUBLISHED_INDEX`` — filtered
         exactly as VANITY filters one. A URL synthesized from a bare tenant
         reference (a ``/connect/users/sign_in`` link, a per-unit deep link)
         never claimed to be a listings surface at all, so it is
         ``ScopeEvidence.WEAK_EVIDENCE``: no free pass for a single-address
         roster and no free pass when ctx has no address to check against;
      3. otherwise nothing is emitted and the reason is recorded on ctx, so
         the property lands visibly unresolved instead of quietly wrong.
    """
    evaluate = getattr(page, "evaluate", None)
    # NOTE: no longer a hard page-gate. Under the production page=None dispatch,
    # step 1 scans the already-fetched RENDER body (the iframe src is in it) and
    # the sub-path probes fall back to curl_cffi (via _fetch_with_status). Task
    # #37 Track 1. The live-page path is unchanged when a page is present.
    from ma_poc.pms.adapters._probe import body_html_from_ctx

    entry_body = body_html_from_ctx(ctx)

    # 1. Cheap path — AppFolio iframe already on the page (live) or in the body.
    iframe_urls: list[str] = []
    if callable(evaluate):
        try:
            live = await evaluate(_LIVE_APPFOLIO_SRC_JS)
            if isinstance(live, list):
                iframe_urls = [u for u in live if isinstance(u, str) and u]
        except Exception as exc:
            log.debug("AppFolio-embed live scan failed err=%s", exc)
    elif entry_body:
        iframe_urls = list(dict.fromkeys(_APPFOLIO_IFRAME_RE.findall(entry_body)))

    # 2. Probe the well-known sub-paths; pull the iframe src out of each.
    #    PAGE-ONLY: the in-session probe is cheap, but at page=None it would fire
    #    a blanket curl_cffi GET on every 0-unit property (cost/latency). At
    #    page=None we rely on the body scan (step 1) + tenant body scan (2.5).
    #    Bodies are kept so the propertyGroup scan below can read them without
    #    paying for the same GET twice.
    probed_bodies: dict[str, str] = {}
    if not iframe_urls and callable(evaluate):
        origin = _origin(page, ctx)
        if origin:
            for path in _APPFOLIO_EMBED_SUBPATHS:
                html = await _fetch(page, origin + path)
                if not html:
                    continue
                probed_bodies[origin + path] = html
                m = _APPFOLIO_IFRAME_RE.search(html)
                if m:
                    iframe_urls = [m.group(0)]
                    break

    # 2.5. Tenant-only fallback (2026-05-20). If steps 1+2 produced no
    # listings URL, scan the live page for ANY ``*.appfolio.com/*``
    # reference (auth/login/dashboard etc.) and synthesize the canonical
    # ``{tenant}.appfolio.com/listings`` URL from the host. Covers
    # SYNDICATION_ONLY_WIX shells whose only AppFolio breadcrumb is a
    # /connect/users/sign_in or /request_access anchor. Dedup by URL.
    #
    # 2026-07-28: these URLs are SYNTHESIZED — nothing on the page said "the
    # listings live here", only "this property is managed by this AppFolio
    # account". Remembered so step 3 can hold them to a stricter admission bar
    # than a listings widget the operator actually embedded.
    #
    # 2026-07-28 (QC): a per-unit deep link under ``/listings/`` is the same
    # grade of evidence as a sign_in anchor, so it lands here too. Roots are
    # partitioned BEFORE this step runs, because step 2.5 appends synthesized
    # ``{tenant}/listings`` URLs that are index-SHAPED but not index-SOURCED.
    strong_roots: set[str] = set()
    tenant_only_urls: set[str] = set()
    for _u in iframe_urls:
        _root = _to_appfolio_listings_root(_u)
        if not _root:
            continue
        if is_listings_index_url(_u):
            strong_roots.add(_root)
        else:
            tenant_only_urls.add(_root)
    # A root the operator DID publish as an index outranks a deep link that
    # happens to canonicalize to the same place.
    tenant_only_urls -= strong_roots

    if not strong_roots:
        tenants: list[str] | None = None
        if callable(evaluate):
            try:
                _t = await evaluate(_LIVE_APPFOLIO_TENANT_JS)
                tenants = _t if isinstance(_t, list) else None
            except Exception as exc:
                log.debug("AppFolio-embed tenant scan failed err=%s", exc)
                tenants = None
        else:
            # page=None: harvest any *.appfolio.com/* reference from the body.
            tenants = re.findall(
                r"https?://[a-z0-9][a-z0-9-]*\.appfolio\.com/[^\s\"'<>]*",
                entry_body,
                re.I,
            ) if entry_body else None
        if isinstance(tenants, list):
            seen: set[str] = set()
            for u in tenants:
                if not isinstance(u, str):
                    continue
                listings = _tenant_listings_url(u)
                if listings and listings not in seen:
                    seen.add(listings)
                    iframe_urls.append(listings)
                    tenant_only_urls.add(listings)

    from ma_poc.pms.adapters._universal_recovery import (
        is_bot_block,
        mark_blocked,
        note_recovery,
    )

    if not iframe_urls:
        return []

    # 2.75. SCOPE THE CANDIDATES. Canonicalization keeps a filter the page
    # already carried; when none did, go looking for the propertyGroup (entry
    # body, then the availability/floor-plan sub-pages where it usually lives)
    # and build the scoped URL ourselves. A single scoped candidate makes every
    # account-wide candidate redundant — and dangerous — so they are dropped.
    candidates = [_to_appfolio_listings_root(u) for u in iframe_urls]
    candidates = list(dict.fromkeys(c for c in candidates if c))
    # A scope is only ever taken from something the OPERATOR published — the
    # widget URL's own filter, or a propertyGroup in their embed JS. Both are
    # the property's own declaration, so an empty response to either is the
    # property's real answer (Cherry Tree: "No vacancies found matching your
    # search criteria") and must not reopen the account-wide query.
    #
    # 2026-07-28 (QC): the one property that WAS scoped by a guess — 46582,
    # 1109/1121 S. Paige St, Wichita KS, which lost 5 real listings to a
    # manufactured "no availability" — was reading AppFolio's commented-out
    # ``//propertyGroup: 'My Group Name'`` template line. That is fixed at the
    # source in ``find_appfolio_property_group``: the placeholder is no longer
    # a scope, so no scope is synthesized and the roster falls through to the
    # address filter here. No fall-through path is needed, and adding one would
    # hand every genuinely-empty scoped property back its account roster.
    scoped = [c for c in candidates if is_scoped_listings_url(c)]
    group_source = "discovered_url"
    if not scoped:
        group, group_source = await _discover_property_group(
            page, ctx, entry_body, probed_bodies
        )
        if group:
            scoped = list(
                dict.fromkeys(scoped_listings_url(c, group) for c in candidates)
            )
    if scoped:
        candidates = scoped

    # 3. Fetch the AppFolio listings page itself and run the existing SSR
    #    parser. A 401/403/429/503 here is recorded as a bot-block (the
    #    production stack — residential proxy + Camoufox + cookie-mint reuse —
    #    may flip the same probe to a hit).
    declined: tuple[str, str] | None = None
    for src in candidates:
        status, html = await _fetch_with_status(page, src)
        if is_bot_block(status):
            mark_blocked(ctx, "appfolio_embed", src, status)
        if not html:
            continue
        units = parse_appfolio_listings_ssr(html, src)

        if is_scoped_listings_url(src):
            # Server-side scoped: authoritative either way. An empty response
            # from a scoped URL is Cherry Tree's real answer ("No vacancies
            # found matching your search criteria"), so it must NOT reopen the
            # account-wide query.
            if not units:
                note_recovery(
                    ctx,
                    "appfolio_embed",
                    "scoped_no_availability",
                    f"{src} (scope from {group_source})",
                )
            return units

        if not units:
            continue

        # Unscoped roster: admit only what verifiably belongs here, at the
        # grade the discovery earned. A synthesized tenant URL never claimed to
        # be this property's listings surface, so its pass-throughs are
        # removed; an index the operator published keeps them.
        evidence = (
            ScopeEvidence.WEAK_EVIDENCE
            if src in tenant_only_urls
            else ScopeEvidence.PUBLISHED_INDEX
        )
        kept, tel = filter_listings_by_property_address(
            units,
            getattr(ctx, "address", "") or "",
            getattr(ctx, "zip_code", "") or "",
            evidence=evidence,
        )
        if kept:
            return kept
        declined = (
            str(tel.get("reason") or "unscopeable"),
            f"{src} dropped={len(units)} evidence={evidence.value} "
            f"ctx_addr={getattr(ctx, 'address', '')!r} "
            f"ctx_zip={getattr(ctx, 'zip_code', '')!r}",
        )

    if declined is not None:
        note_recovery(ctx, "appfolio_embed", declined[0], declined[1])
    return []
