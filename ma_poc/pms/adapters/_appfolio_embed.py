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

import html as html_lib
import logging
import re
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, unquote_plus, urljoin, urlparse, urlsplit, urlunparse

from ma_poc.pms.adapters.appfolio import (
    ScopeEvidence,
    filter_listings_by_property_address,
    find_appfolio_property_group,
    parse_appfolio_listings_ssr,
)
from ma_poc.pms.appfolio_urls import (
    appfolio_tenant_slug,
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
    r"""(?:https?:)?//[a-z0-9][a-z0-9-]*\.appfolio\.com/listings[^\s"'<>]*""",
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


def _absolute_appfolio_url(url: str) -> str:
    """Normalize an operator-published protocol-relative AppFolio URL."""
    raw = str(url or "").strip()
    return f"https:{raw}" if raw.startswith("//") else raw


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
    url = _absolute_appfolio_url(url)
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

# Wix ``HtmlComponent`` bridge (2026-08-01).  Several exact plan-level cohort
# properties publish AppFolio one level deeper than the ordinary iframe scan:
#
#   marketing entry -> labelled /appfolio or /apply route
#   -> Wix thunderbolt page-data JSON
#   -> same-site *.filesusr.com/html/<component>.html
#   -> Appfolio.Listing({hostUrl, propertyGroup})
#
# The browser visibly resolves that chain, but the captured entry HTML does
# not contain the final AppFolio URL.  Keep every hop operator-published and
# tightly bounded; there is deliberately no guessed-route list here.
_WIX_MARKER_RE = re.compile(
    r"(?:wixstatic\.com|parastorage\.com|filesusr\.com)", re.IGNORECASE
)
_WIX_ROUTE_NAMES: tuple[str, ...] = ("appfolio", "apply", "availability")
_ATTR_URL_RE = re.compile(r"(?:href|src)\s*=\s*['\"]([^'\"]+)['\"]", re.I)
_FILESUSR_HTML_RE = re.compile(
    r"https://[a-z0-9-]+\.filesusr\.com/html/[a-z0-9_-]+\.html",
    re.IGNORECASE,
)
_APPFOLIO_HOST_CONFIG_RE = re.compile(
    r"\bhostUrl\s*:\s*['\"]([a-z0-9][a-z0-9-]*\.appfolio\.com)['\"]",
    re.IGNORECASE,
)
_APPFOLIO_SCRIPT_HOST_RE = re.compile(
    r"//([a-z0-9][a-z0-9-]*\.appfolio\.com)/javascripts/listing\.js",
    re.IGNORECASE,
)
_LISTABLE_UID_RE = re.compile(
    r"/listings/detail/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12})(?:[?'\"/#]|$)",
    re.IGNORECASE,
)
_LISTING_TITLE_RE = re.compile(
    r"\bjs-listing-title\b[^>]*>(.*?)</(?:h[1-6]|div|span)>",
    re.IGNORECASE | re.DOTALL,
)
_WAITLIST_RE = re.compile(r"\bwait\s*-?\s*list\b", re.IGNORECASE)
_PROPERTY_LABEL_STOPWORDS = frozenset(
    {"website", "apartment", "apartments", "community", "property", "the"}
)


def _tenant_listings_url(any_appfolio_url: str) -> str | None:
    """Extract tenant slug from any AppFolio URL and synthesize the
    canonical ``https://{tenant}.appfolio.com/listings`` root. Returns
    ``None`` if the URL doesn't match the tenant-host pattern.
    """
    m = _APPFOLIO_TENANT_HOST_RE.match(_absolute_appfolio_url(any_appfolio_url))
    if not m:
        return None
    tenant = m.group(1).lower()
    return f"https://{tenant}.appfolio.com/listings"


def _host_without_www(host: str) -> str:
    value = (host or "").strip().lower().rstrip(".")
    return value[4:] if value.startswith("www.") else value


def _marketing_origin_from_ctx(ctx: AdapterContext) -> str:
    try:
        parsed = urlsplit(getattr(ctx, "base_url", "") or "")
    except ValueError:
        return ""
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return ""
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"


def _wix_route_urls(entry_body: str, ctx: AdapterContext) -> list[str]:
    """Return at most two operator-linked Wix inventory routes.

    There is no guessed ``/appfolio`` request: a route is eligible only when
    its exact URL appeared in the already-captured marketing body, stays on
    the same host, and its final path component is one of the three labels
    live-verified in the cohort.
    """
    if not entry_body or not _WIX_MARKER_RE.search(entry_body):
        return []
    origin = _marketing_origin_from_ctx(ctx)
    if not origin:
        return []
    base_host = _host_without_www(urlsplit(origin).hostname or "")
    ranked: list[tuple[int, str]] = []
    for raw in _ATTR_URL_RE.findall(entry_body):
        candidate = html_lib.unescape(raw).strip()
        if not candidate or candidate.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        absolute = urljoin(origin + "/", candidate)
        try:
            parsed = urlsplit(absolute)
        except ValueError:
            continue
        if parsed.scheme.lower() not in {"http", "https"}:
            continue
        if _host_without_www(parsed.hostname or "") != base_host:
            continue
        leaf = parsed.path.rstrip("/").rsplit("/", 1)[-1].lower()
        if leaf not in _WIX_ROUTE_NAMES:
            continue
        clean = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{parsed.path or '/'}"
        ranked.append((_WIX_ROUTE_NAMES.index(leaf), clean))
    return [u for _, u in sorted(dict.fromkeys(ranked))[:2]]


def _filesusr_urls(body: str, marketing_origin: str) -> list[str]:
    """Extract only the Wix component host owned by this marketing domain."""
    if not body or not marketing_origin:
        return []
    try:
        marketing_host = _host_without_www(urlsplit(marketing_origin).hostname or "")
    except ValueError:
        return []
    if not marketing_host:
        return []
    domain_slug = marketing_host.replace(".", "-")
    allowed_hosts = {
        f"{domain_slug}.filesusr.com",
        f"www-{domain_slug}.filesusr.com",
    }
    # Thunderbolt JSON escapes URL slashes.  Decode only those escapes; using
    # html.unescape on an entire Wix URL turns ``&registry...`` into the
    # ``&reg`` entity and corrupts a valid page-data request.
    searchable = body.replace(r"\/", "/").replace(r"\u002F", "/")
    found: list[str] = []
    for raw in _FILESUSR_HTML_RE.findall(searchable):
        try:
            parsed = urlsplit(raw)
        except ValueError:
            continue
        if (parsed.hostname or "").lower() not in allowed_hosts:
            continue
        if parsed.query or parsed.fragment:
            continue
        found.append(raw)
    return list(dict.fromkeys(found))[:3]


def _wix_page_data_urls(route_body: str, marketing_origin: str) -> list[str]:
    """Return the two exact Thunderbolt feature-data URLs published by Wix."""
    if not route_body or not marketing_origin:
        return []
    expected_host = _host_without_www(urlsplit(marketing_origin).hostname or "")
    out: list[str] = []
    for raw in _ATTR_URL_RE.findall(route_body):
        # Decode the only entity Wix emits in these query strings.  General
        # entity decoding is unsafe for ``&registryLibrariesTopology``.
        candidate = raw.replace("&amp;", "&")
        try:
            parsed = urlsplit(candidate)
        except ValueError:
            continue
        if parsed.scheme.lower() != "https":
            continue
        if (parsed.hostname or "").lower() != "siteassets.parastorage.com":
            continue
        if parsed.path != "/pages/pages/thunderbolt":
            continue
        query = parse_qs(parsed.query, keep_blank_values=True)
        if query.get("module") != ["thunderbolt-features"]:
            continue
        if query.get("contentType") != ["application/json"]:
            continue
        external = query.get("externalBaseUrl", [""])[0]
        try:
            external_host = _host_without_www(urlsplit(external).hostname or "")
        except ValueError:
            continue
        if not external_host or external_host != expected_host:
            continue
        out.append(candidate)
    return list(dict.fromkeys(out))[:2]


def _appfolio_config_in_component(body: str) -> tuple[str, str] | None:
    """Validate and return ``(listings_root, property_group)`` from a Wix component."""
    if not body or len(body) > 65_536 or "Appfolio.Listing" not in body:
        return None
    host_match = _APPFOLIO_HOST_CONFIG_RE.search(body)
    script_match = _APPFOLIO_SCRIPT_HOST_RE.search(body)
    if not host_match or not script_match:
        return None
    host = host_match.group(1).lower()
    if host != script_match.group(1).lower():
        return None
    root = f"https://{host}/listings"
    if not appfolio_tenant_slug(root):
        return None
    group = _property_group_in(body)
    if not group:
        return None
    return root, group


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


async def _discover_wix_appfolio_components(
    page: Page,
    ctx: AdapterContext,
    entry_body: str,
) -> dict[str, tuple[str, str]]:
    """Resolve Wix HtmlComponents to scoped AppFolio listing URLs.

    The returned mapping is ``scoped_url -> (property_group, component_url)``.
    Request budget is deterministic: up to three already-published component
    URLs from the entry body, then at most two labelled routes, two Wix feature
    documents per route, and three component URLs total.  All fetches use the
    direct probe path (``probe_fetch_status(..., unlocker=False)`` at
    ``page=None``); this recovery never invokes a CAPTCHA solver or unlocker.
    """
    if not entry_body or not _WIX_MARKER_RE.search(entry_body):
        return {}
    origin = _marketing_origin_from_ctx(ctx)
    if not origin:
        return {}

    component_urls = _filesusr_urls(entry_body, origin)
    for route_url in _wix_route_urls(entry_body, ctx):
        route_body = await _fetch(page, route_url)
        if not route_body or len(route_body) > 2_000_000:
            continue
        component_urls.extend(_filesusr_urls(route_body, origin))
        for data_url in _wix_page_data_urls(route_body, origin):
            data_body = await _fetch(page, data_url)
            if not data_body or len(data_body) > 1_000_000:
                continue
            component_urls.extend(_filesusr_urls(data_body, origin))

    configs: dict[str, tuple[str, str]] = {}
    # The exact live cohort needs one component per property.  Three leaves
    # room for an unrelated review widget plus desktop/mobile variants while
    # keeping an accidental Wix component fan-out impossible.
    for component_url in list(dict.fromkeys(component_urls))[:3]:
        component_body = await _fetch(page, component_url)
        config = _appfolio_config_in_component(component_body)
        if config is None:
            continue
        root, group = config
        scoped_url = scoped_listings_url(root, group)
        configs[scoped_url] = (group, component_url)
    return configs


def _plain_text(fragment: str) -> str:
    value = re.sub(r"<[^>]+>", " ", fragment or "")
    return re.sub(r"\s+", " ", html_lib.unescape(value)).strip()


def _property_label_tokens(value: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", (value or "").lower())
        if len(token) > 1 and token not in _PROPERTY_LABEL_STOPWORDS
    }


def _nested_card_metadata(html: str) -> dict[str, dict[str, str | bool]]:
    """Return listable UID/title/waitlist evidence keyed by numeric card id."""
    from ma_poc.pms.adapters.appfolio import _iter_listing_cards

    out: dict[str, dict[str, str | bool]] = {}
    for card in _iter_listing_cards(html):
        body = card.group("body")
        uid_match = _LISTABLE_UID_RE.search(body)
        title_match = _LISTING_TITLE_RE.search(body)
        title = _plain_text(title_match.group(1)) if title_match else ""
        card_text = _plain_text(body)
        out[card.group("id")] = {
            "listable_uid": uid_match.group(1).lower() if uid_match else "",
            "title": title,
            "waitlist": bool(_WAITLIST_RE.search(card_text)),
        }
    return out


def _nested_wix_units(
    html: str,
    src: str,
    group: str,
    ctx: AdapterContext,
) -> tuple[list[dict[str, str]], str]:
    """Parse and boundary-check a Wix-published AppFolio roster.

    AppFolio does not reliably honor ``filters[property_list]``.  Vestawood's
    live widget publishes that query yet returns 18 Vestawood cards plus 17
    Green Springs cards.  Therefore the query is discovery evidence, not a
    permission slip to emit the whole response:

    * if listing titles explicitly identify the published property group,
      retain only those cards;
    * otherwise require every judgeable row to remain in the CSV property's
      ZIP (the four Breeden cohort widgets satisfy this, including multi-street
      campuses that an exact street filter would incorrectly truncate);
    * require AppFolio's stable public ``listable_uid`` and reject explicit
      wait-list placeholders.
    """
    units = parse_appfolio_listings_ssr(html, src)
    if not units:
        return [], "scoped_no_availability"
    metadata = _nested_card_metadata(html)
    group_tokens = _property_label_tokens(group)

    matching_card_ids = {
        card_id
        for card_id, meta in metadata.items()
        if group_tokens
        and group_tokens.issubset(_property_label_tokens(str(meta.get("title") or "")))
    }
    if matching_card_ids:
        units = [
            unit
            for unit in units
            if str((unit.get("source_ids") or {}).get("appfolio_listing_id") or "")
            in matching_card_ids
        ]
        scope_reason = "property_group_title_scope"
    else:
        ctx_zip = re.sub(r"\D", "", getattr(ctx, "zip_code", "") or "")[:5]
        observed_zips = {
            match.group(1)
            for unit in units
            if (match := re.search(r"\b(\d{5})(?:-\d{4})?\b", unit.get("unit_name") or ""))
        }
        if not ctx_zip or not observed_zips or observed_zips != {ctx_zip}:
            return [], "nested_widget_property_boundary_failed"
        scope_reason = "property_group_single_zip_scope"

    clean: list[dict[str, str]] = []
    for unit in units:
        listing_id = str(
            (unit.get("source_ids") or {}).get("appfolio_listing_id") or ""
        )
        meta = metadata.get(listing_id) or {}
        listable_uid = str(meta.get("listable_uid") or "")
        if not listable_uid or bool(meta.get("waitlist")):
            continue
        source_ids = dict(unit.get("source_ids") or {})
        source_ids["appfolio_listable_uid"] = listable_uid
        unit["source_ids"] = source_ids

        title = str(meta.get("title") or "").strip()
        title_tokens = _property_label_tokens(title)
        is_property_label = bool(
            title_tokens and group_tokens and group_tokens.issubset(title_tokens)
        )
        if title and not is_property_label:
            unit["floor_plan_name"] = title
            unit["_floor_plan_name_provenance"] = (
                "appfolio.listing_title_nested_widget"
            )
        clean.append(unit)
    return clean, scope_reason


def _property_group_in(html: str) -> str:
    """Read the AppFolio widget's per-property ``propertyGroup`` out of *html*.

    Covers all three operator-published scope shapes seen in the cohort: an
    already-scoped ``data-listings-url``/iframe URL, the plain
    ``Appfolio.Listing({hostUrl: ..., propertyGroup: 'COLLEGE PARK'})`` embed
    JS, and the base64-encoded widget config that AppFolio Websites (Duda)
    pages carry.  The URL form is checked first because it is the exact query
    the operator's widget will request, not a scope reconstructed from config.
    """
    if not html:
        return ""
    for published_url in _APPFOLIO_IFRAME_RE.findall(html):
        if not is_listings_index_url(published_url):
            continue
        published_scope = property_list_scope(published_url)
        if published_scope:
            decoded = unquote_plus(published_scope).strip()
            if decoded:
                return decoded
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
    nested_wix_configs: dict[str, tuple[str, str]] = {}

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

    # 2.25. Wix HtmlComponent bridge.  The entry body publishes the exact
    # inventory route, but the AppFolio URL itself lives in the route's Wix
    # page-data document and then in a same-site filesusr component.  Resolve
    # that declared chain only after the direct iframe paths miss.
    if not iframe_urls:
        nested_wix_configs = await _discover_wix_appfolio_components(
            page, ctx, entry_body
        )
        iframe_urls.extend(nested_wix_configs)

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
        _published_url = _absolute_appfolio_url(_u)
        _root = _to_appfolio_listings_root(_published_url)
        if not _root:
            continue
        if is_listings_index_url(_published_url):
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
            tenants = (
                re.findall(
                    r"(?:https?:)?//[a-z0-9][a-z0-9-]*\.appfolio\.com/[^\s\"'<>]*",
                    entry_body,
                    re.I,
                )
                if entry_body
                else None
            )
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
        group, group_source = await _discover_property_group(page, ctx, entry_body, probed_bodies)
        if group:
            scoped = list(dict.fromkeys(scoped_listings_url(c, group) for c in candidates))
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

        nested_config = nested_wix_configs.get(src)
        if nested_config is not None:
            group, component_url = nested_config
            units, nested_reason = _nested_wix_units(html, src, group, ctx)
            if not units:
                note_recovery(
                    ctx,
                    "appfolio_embed",
                    nested_reason,
                    f"{src} component={component_url}",
                )
                continue
            note_recovery(
                ctx,
                "appfolio_embed",
                nested_reason,
                f"{src} component={component_url} kept={len(units)}",
            )
            return units

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
        evidence = ScopeEvidence.WEAK_EVIDENCE if src in tenant_only_urls else ScopeEvidence.PUBLISHED_INDEX
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
