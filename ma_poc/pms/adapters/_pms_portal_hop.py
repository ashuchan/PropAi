"""Shared PMS-portal-hop recovery for floorplan-only extractions.

2026-05-19 deep-probe finding: of the 52 ground-truthed 0%-uid bootstrap79
properties, ~20 have unit-level inventory in a known PMS portal one nav-hop
deep from the marketing site (ResMan ``Portal/Applicants/Availability``,
RentCafe ``SecureCafe`` ``availableunits.aspx``, Entrata ``ProspectPortal``)
but jugnu's marketing-site tiers (TIER_3_DOM / TIER_2_JSONLD / TIER_MERGED)
never follow the portal anchor. This is a pure routing gap — the per-PMS
SSR parsers already exist (``parse_resman_unittypes`` /
``parse_securecafe_availableunits``); they just never get the portal HTML
to chew on.

Mirrors the AppFolio-embed recovery pattern (see ``_appfolio_embed.py``):

  1. Cheap path — scan the live page for a portal URL (iframe ``src`` or
     anchor ``href``).
  2. Probe a tight set of well-known sub-paths under the current origin;
     pull the portal URL out of each body.
  3. Fetch the portal page in-session via ``page.evaluate`` (reuses the
     page's own origin/cookies — same trick the Entrata-endpoint and
     AppFolio-embed probes use), then hand the HTML to the existing
     tier-1 PMS SSR parser. First non-empty wins.

PMS coverage:
  - ResMan:  ``*.myresman.com/Portal/Applicants/Availability?a=…&p=…``
             → ``resman._extract_unittypes`` + ``resman.parse_resman_unittypes``
  - RentCafe SecureCafe: ``*.securecafe.com/.../availableunits.aspx``
             → ``rentcafe.parse_securecafe_availableunits``
  - RentCafe Applicant V2: a published SecureCafe tenant/slug whose legacy
             table is empty → exact property theme + public Applicant API

Not yet covered (TODO):
  - Entrata ProspectPortal — unit-level data is an XHR fragment
    (``view_unit_spaces``) reachable via a per-floorplan request, not a
    top-level HTML page; needs a more elaborate fetch loop.
  - RealPage Online-Leasing (``onlineleasing.realpage.com``) — workflow JSON
    over multiple POSTs; ``parse_realpage_oll_workflow`` takes a dict, not
    HTML.

Verified-live ground-truth sites (2026-05-19 probe; will succeed once
wired): cobblestonephx (ResMan), mansionsattimberland (ResMan),
liveatemerald (ResMan), mansionsatsunsetridge (ResMan), forge65 (RentCafe),
somersetdm (RentCafe). 9 ResMan + 4 RentCafe of the 23 unit-level-reachable
0%-uid sites.
"""

from __future__ import annotations

import logging
import math
import re
from html import unescape
from html.parser import HTMLParser
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urljoin, urlparse, urlunparse

from ma_poc.pms.adapters.rentcafe import parse_securecafe_availableunits
from ma_poc.pms.adapters.resman import (
    _extract_unittypes as _extract_resman_unittypes,
)
from ma_poc.pms.adapters.resman import (
    parse_resman_unittypes,
)

if TYPE_CHECKING:
    from playwright.async_api import Page

    from ma_poc.pms.adapters.base import AdapterContext

log = logging.getLogger(__name__)

# ResMan availability portal: per-property URL under the property-management
# company's ``*.myresman.com`` tenant subdomain. ``a=`` (account id) and
# ``p=`` (property guid) are required query params; the page embeds
# ``var unitTypes = [...]`` which ``_extract_resman_unittypes`` walks.
_RESMAN_PORTAL_RE = re.compile(
    r"""https?://[a-z0-9][a-z0-9-]*\.myresman\.com/Portal/Applicants/Availability"""
    r"""\?a=\d+&p=[a-f0-9-]+""",
    re.IGNORECASE,
)

# Public ResMan marketing links are not limited to the data-bearing
# ``Applicants/Availability`` route.  The 2026-07-31 plan-level cohort also
# exposed these entry routes:
#
# * ``Access/ApplicantRegistration?accountID=...&propertyID=...``
# * ``Prospects/NewGuestCard?a=...&p=...``
# * ``Applicants/New/<property-code>?a=...``
# * ``Access/SignIn/<property-code>``
#
# The first two already carry the target tuple.  The latter two return (or
# same-host redirect to) a public page with hidden AccountID/PropertyID inputs.
# Discovery remains deliberately limited to one ResMan tenant label and these
# exact route families; the resolver below validates the parsed URL again.
_RESMAN_ENTRY_RE = re.compile(
    r"""https?://[a-z0-9][a-z0-9-]*\.myresman\.com/Portal/(?:"""
    r"""Applicants/Availability|Access/ApplicantRegistration|"""
    r"""Prospects/NewGuestCard|Applicants/New/[a-z0-9_-]+|"""
    r"""Access/SignIn/[a-z0-9_-]+)"""
    r"""[^\"'<>\\\s]*""",
    re.IGNORECASE,
)
_RESMAN_GUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_RESMAN_TENANT_HOST_RE = re.compile(
    r"^[a-z0-9][a-z0-9-]*\.myresman\.com$",
    re.IGNORECASE,
)


class _ResManHiddenIdsParser(HTMLParser):
    """Collect exact hidden ResMan account/property identifiers.

    Entry pages use ordinary hidden inputs, but their attribute order and
    casing vary between tenants.  Collect sets instead of taking the first
    value so a management-account page containing more than one property
    fails closed rather than selecting an arbitrary apartment community.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.account_ids: set[str] = set()
        self.property_ids: set[str] = set()

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if tag.casefold() != "input":
            return
        values = {str(key).casefold(): str(value or "").strip() for key, value in attrs}
        field = (values.get("name") or values.get("id") or "").casefold()
        value = values.get("value", "")
        if field == "accountid" and value.isdigit():
            self.account_ids.add(value)
        elif field == "propertyid" and _RESMAN_GUID_RE.fullmatch(value):
            self.property_ids.add(value.lower())


def _resman_ids_from_html(html: str) -> tuple[str, str]:
    """Return one unambiguous ``(account_id, property_id)`` pair."""
    parser = _ResManHiddenIdsParser()
    try:
        parser.feed(html or "")
    except Exception:
        return "", ""
    if len(parser.account_ids) != 1 or len(parser.property_ids) != 1:
        return "", ""
    return next(iter(parser.account_ids)), next(iter(parser.property_ids))


def _canonical_resman_availability(
    entry_url: str,
    entry_html: str = "",
) -> tuple[str, str]:
    """Resolve a public ResMan entry route to its scoped availability URL.

    The URL may already carry ``a``/``p`` or their long-form equivalents.
    ``Applicants/New`` and ``Access/SignIn`` pages instead publish the missing
    tuple in hidden fields.  Only one strict tenant host, numeric account ID,
    and UUID-shaped property ID are accepted.  Returns ``(url, property_id)``
    or ``("", "")``; the property ID is retained for response-scope checks.
    """
    try:
        parsed = urlparse(unescape(entry_url))
    except (TypeError, ValueError):
        return "", ""
    host = (parsed.hostname or "").lower().rstrip(".")
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not _RESMAN_TENANT_HOST_RE.fullmatch(host)
        or parsed.username is not None
        or parsed.password is not None
    ):
        return "", ""

    query = {key.casefold(): values for key, values in parse_qs(parsed.query).items()}

    def _one_query(*keys: str) -> str:
        values: list[str] = []
        for key in keys:
            values.extend(str(value).strip() for value in query.get(key, []))
        unique = {value for value in values if value}
        return next(iter(unique)) if len(unique) == 1 else ""

    account_id = _one_query("a", "accountid")
    property_id = _one_query("p", "propertyid").lower()
    html_account, html_property = _resman_ids_from_html(entry_html)
    account_id = account_id or html_account
    property_id = property_id or html_property
    if not account_id.isdigit() or not _RESMAN_GUID_RE.fullmatch(property_id):
        return "", ""
    canonical = urlunparse(
        (
            "https",
            host,
            "/Portal/Applicants/Availability",
            "",
            f"a={account_id}&p={property_id}",
            "",
        )
    )
    return canonical, property_id


def _resman_groups_match_property(
    data: list[dict[str, Any]],
    property_id: str,
    *,
    require_present: bool,
) -> bool:
    """Validate that a ResMan roster belongs to the resolved property."""
    expected = property_id.strip().lower()
    observed: set[str] = set()
    for group in data:
        if not isinstance(group, dict):
            return False
        raw = group.get("PropertyID") or group.get("PropertyId") or group.get("propertyID")
        if raw not in (None, ""):
            observed.add(str(raw).strip().lower())
    if require_present and not observed:
        return False
    return not observed or observed == {expected}


# RentCafe SecureCafe online-leasing portal. The host is the
# ``securecafe.com`` PMS subdomain (sometimes ``rentcafe.com`` for the
# same product). Real marketing pages link to ANY of the onlineleasing
# entry points — ``guestlogin.aspx``, ``floorplans.aspx``,
# ``availableunits.aspx`` — depending on the "Apply Now"/"Floor Plans"/
# "Availability" anchor label. Match the slug-root broadly; the recovery
# transforms whatever entry it captured into the canonical
# ``/availableunits.aspx`` path before fetching (the SSR table we parse).
#
# 2026-05-19: broadened from the prior ``availableunits.aspx``-only
# match, which missed sweetwaterfl / parkviewapartmenthomes /
# parkerhouse — all three link to ``floorplans.aspx`` or
# ``guestlogin.aspx`` from their marketing site.
_RENTCAFE_PORTAL_RE = re.compile(
    r"""https?://(?:"""
    r"""[a-z0-9][a-z0-9-]*\.(?:securecafe|rentcafe)\.com/"""
    r"""(?:onlineleasing|residentservices)/[a-z0-9_-]+[^\"'<>\\\s]*"""
    r"""|[a-z0-9][a-z0-9-]*\.securecafeapplicant\.com/"""
    r"""onlineleasing/content3/access/[a-z0-9_-]+[^\"'<>\\\s]*"""
    r"""|[a-z0-9][a-z0-9-]*\.securecaferesident\.com/"""
    r"""residentservices/content3/access/[a-z0-9_-]+[^\"'<>\\\s]*"""
    r""")""",
    re.IGNORECASE,
)
# Property-scoped entry paths that all map to the same SecureCafe SSR roster.
# The ``content3/access`` hosts are the current applicant/resident frontends;
# the marketing page publishes the property slug in their path even when no
# legacy ``onlineleasing`` anchor is present.
_RENTCAFE_ONLINE_PATH_RE = re.compile(
    r"^/onlineleasing/([a-z0-9_-]+)(?:/|$)",
    re.IGNORECASE,
)
_RENTCAFE_RESIDENT_PATH_RE = re.compile(
    r"^/residentservices/([a-z0-9_-]+)(?:/|$)",
    re.IGNORECASE,
)
_RENTCAFE_APPLICANT_PATH_RE = re.compile(
    r"^/onlineleasing/content3/access/([a-z0-9_-]+)(?:/|$)",
    re.IGNORECASE,
)
_RENTCAFE_RESIDENT_APP_PATH_RE = re.compile(
    r"^/residentservices/content3/access/([a-z0-9_-]+)(?:/|$)",
    re.IGNORECASE,
)
_RENTCAFE_TENANT_LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$", re.IGNORECASE)

# ``scraper._try_link_hop`` already consumes this item shape from an
# ``AdapterResult``.  Portal recovery runs against ``AdapterContext`` instead,
# so a failed code-only probe records the exact same tuples on the context for
# the caller to promote after the universal-recovery chain finishes.
_PORTAL_HINTS_ATTR = "_embedded_portal_hints"
_PORTAL_HTTP_TIMEOUT_SECONDS = 15.0
_PORTAL_MAX_BODY_BYTES = 3_000_000
_PORTAL_MAX_REDIRECTS = 3
_RENT_FIELDS: tuple[str, ...] = (
    "market_rent_low",
    "market_rent_high",
    "rent_low",
    "rent_high",
    "asking_rent",
    "rent",
)

# Legacy Spherexx availability iframe.  This is intentionally restricted to
# the vendor's exact ``clients.spherexx.com`` host and an availability-bearing
# path.  Several property sites publish static asset URLs from that host even
# after retiring the roster; those must not trigger a recovery.
_SPHEREXX_PORTAL_RE = re.compile(
    r"""https?://clients\.spherexx\.com/[^\s\"'<>]*availability[^\s\"'<>]*""",
    re.IGNORECASE,
)

# At ``page=None`` the universal cascade cannot click the site's navigation.
# Follow only URLs the exact property body explicitly publishes, and only when
# the path itself names the inventory page.  This is narrower than the live-
# page well-known-subpath loop and prevents a cross-property guessed route.
_PUBLISHED_INVENTORY_PATH_RE = re.compile(
    r"(?:^|/)(?:availability|available-units?|floor-?plans?)(?:/|$|[.?])",
    re.IGNORECASE,
)


def _canonical_rentcafe_availableunits(url: str) -> str:
    """Return a strict canonical SecureCafe inventory URL, or ``""``.

    Known entry points (``guestlogin.aspx`` / ``floorplans.aspx`` / a bare
    slug) are normalized to HTTPS, a lowercase tenant host, no query/fragment,
    and the data-bearing ``availableunits.aspx`` path.  Host validation is
    repeated here rather than trusting the discovery regex because this URL is
    handed to the renderer as an external navigation candidate.
    """
    try:
        p = urlparse(url)
        explicit_port = p.port
    except (TypeError, ValueError):
        return ""
    host = (p.hostname or "").lower().rstrip(".")
    canonical_host = ""
    slug = ""

    def _tenant_label(suffix: str) -> str:
        marker = f".{suffix}"
        if not host.endswith(marker):
            return ""
        label = host[: -len(marker)]
        return label if _RENTCAFE_TENANT_LABEL_RE.fullmatch(label) else ""

    standard_tenant = _tenant_label("securecafe.com") or _tenant_label("rentcafe.com")
    if standard_tenant:
        match = _RENTCAFE_ONLINE_PATH_RE.match(p.path) or _RENTCAFE_RESIDENT_PATH_RE.match(p.path)
        if match:
            slug = match.group(1).lower()
            canonical_host = host
    else:
        applicant_tenant = _tenant_label("securecafeapplicant.com")
        resident_tenant = _tenant_label("securecaferesident.com")
        if applicant_tenant:
            match = _RENTCAFE_APPLICANT_PATH_RE.match(p.path)
            if match:
                slug = match.group(1).lower()
                canonical_host = f"{applicant_tenant}.securecafe.com"
        elif resident_tenant:
            match = _RENTCAFE_RESIDENT_APP_PATH_RE.match(p.path)
            if match:
                slug = match.group(1).lower()
                canonical_host = f"{resident_tenant}.securecafe.com"

    if (
        not slug
        or slug == "content3"
        or p.scheme.lower() not in {"http", "https"}
        or not canonical_host
        or p.username is not None
        or p.password is not None
        or explicit_port is not None
    ):
        return ""
    return urlunparse(
        (
            "https",
            canonical_host,
            f"/onlineleasing/{slug}/availableunits.aspx",
            "",
            "",
            "",
        )
    )


def _to_rentcafe_availableunits(url: str) -> str:
    """Canonicalize a valid RentCafe URL; preserve non-matches for callers."""
    return _canonical_rentcafe_availableunits(url) or url


def get_portal_hints(ctx: AdapterContext) -> list[tuple[str, str]]:
    """Return normalized portal hints discovered during this recovery.

    The returned list is a copy.  Malformed dynamically-attached values are
    ignored so observability plumbing can never break extraction.
    """
    raw = getattr(ctx, _PORTAL_HINTS_ATTR, None) or []
    hints: list[tuple[str, str]] = []
    for item in raw:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            continue
        url, portal = item
        if isinstance(url, str) and url and isinstance(portal, str) and portal:
            hints.append((url, portal))
    return hints


def has_strict_securecafe_handoff(ctx: AdapterContext) -> bool:
    """Return whether *ctx* carries a validated SecureCafe render route.

    This predicate is intentionally stricter than merely checking that a
    dynamically attached hint exists.  The scraper uses it to redirect retry
    budget, so only the exact canonical URL produced by this module may
    qualify; lookalike hosts, credentials, ports, query strings, and arbitrary
    portal labels remain ordinary evidence and cannot change control flow.
    """
    for url, portal in get_portal_hints(ctx):
        if portal.casefold() != "securecafe":
            continue
        canonical = _canonical_rentcafe_availableunits(url)
        if canonical and canonical == url:
            return True
    return False


def _record_rentcafe_portal_hint(ctx: AdapterContext, url: str) -> None:
    """Attach one strict, de-duplicated SecureCafe render handoff to *ctx*."""
    canonical = _canonical_rentcafe_availableunits(url)
    if not canonical:
        return
    existing = get_portal_hints(ctx)
    key = canonical.casefold()
    if any(old_url.casefold() == key for old_url, _ in existing):
        return
    try:
        setattr(ctx, _PORTAL_HINTS_ATTR, [*existing, (canonical, "securecafe")])
    except Exception:  # pragma: no cover - defensive for foreign context stubs
        return


def _has_positive_numeric_rent(row: dict[str, Any]) -> bool:
    """True only for a positive, finite numeric value in a canonical rent key."""
    for key in _RENT_FIELDS:
        value = row.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if math.isfinite(float(value)) and float(value) > 0:
            return True
    return False


def _validated_portal_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Preserve plan evidence; admit apartments only with numeric rent.

    A plan row remains useful to the universal recovery chain's plan-catalogue
    fallback.  An apartment row is stronger evidence and must satisfy both the
    shared canonical-identity predicate and the positive-numeric-rent contract.
    """
    from ma_poc.core.identity import unit_has_real_anchor

    validated: list[dict[str, Any]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        if unit_has_real_anchor(row) and not _has_positive_numeric_rent(row):
            continue
        validated.append(row)
    return validated


def _has_canonical_unit(rows: list[dict[str, Any]]) -> bool:
    from ma_poc.core.identity import unit_has_real_anchor

    return any(unit_has_real_anchor(row) for row in rows)


# Sub-paths a marketing site uses for the page that links/embeds the PMS
# portal. Ordered by observed frequency in the 2026-05-19 deep probe.
# Kept tight — every entry was seen on a real failed-case property.
_PORTAL_SUBPATHS: tuple[str, ...] = (
    "/floorplans",
    "/floorplans/",
    "/floor-plans",
    "/floor-plans/",
    "/availability",
    "/apartments",
    "/units",
    "/apply",
    "/lease-online",
    "/lease",
    "/available-rentals",
    "/floorplans.php",  # observed on cobblestonephx
    "/floorplans.aspx",
)

# JS run on the live page: harvest any PMS portal URL already present in
# ``iframe`` ``src`` or ``a`` ``href``. The cheap path — fires when the
# marketing site links/embeds the portal on the page we're already on.
# Note the same-anchor heuristic catches anchors hidden behind onclick
# handlers as long as the canonical ``href`` is set (very common).
_LIVE_PORTAL_SRC_JS = r"""
() => {
  const out = [];
  document.querySelectorAll('iframe, a').forEach((el) => {
    const s = (el.src || el.href || '');
    if (
      /\.myresman\.com\/Portal\/(?:Applicants\/Availability|Access\/ApplicantRegistration|Prospects\/NewGuestCard|Applicants\/New\/|Access\/SignIn\/)/i.test(s) ||
      /\.(securecafe|rentcafe)\.com\/(?:onlineleasing|residentservices)\/[a-z0-9_-]+/i.test(s) ||
      /\.securecafeapplicant\.com\/onlineleasing\/content3\/access\/[a-z0-9_-]+/i.test(s) ||
      /\.securecaferesident\.com\/residentservices\/content3\/access\/[a-z0-9_-]+/i.test(s) ||
      /^https?:\/\/clients\.spherexx\.com\/.*availability/i.test(s)
    ) {
      out.push(s);
    }
  });
  return out;
}
"""


def _portal_urls_from_html(body: str) -> list[str]:
    """Extract recognised absolute portal URLs from HTML, in body order."""
    if not body:
        return []
    matches: list[tuple[int, str]] = []
    for pattern in (_RESMAN_PORTAL_RE, _RENTCAFE_PORTAL_RE, _SPHEREXX_PORTAL_RE):
        for match in pattern.finditer(body):
            matches.append((match.start(), unescape(match.group(0))))
    matches.sort(key=lambda item: item[0])
    return list(dict.fromkeys(url for _, url in matches if url))


def _published_inventory_urls(body: str, base_url: str) -> list[str]:
    """Return same-property inventory anchors explicitly published in HTML.

    The helper is used only for the production ``page=None`` path.  It never
    invents a conventional route: each returned URL must exist in an ``a``
    tag in the fetched property body and stay on the same hostname.
    """
    if not body or not base_url:
        return []
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(body, "lxml")
        base = urlparse(base_url)
    except Exception:
        return []
    if not base.hostname:
        return []

    def _host_key(hostname: str | None) -> str:
        host = (hostname or "").casefold().rstrip(".")
        return host[4:] if host.startswith("www.") else host

    expected_host = _host_key(base.hostname)
    out: list[str] = []
    for anchor in soup.find_all("a", href=True):
        raw_href = unescape(str(anchor.get("href") or "")).strip()
        if not raw_href or raw_href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        candidate = urljoin(base_url, raw_href)
        try:
            parsed = urlparse(candidate)
        except Exception:
            continue
        if parsed.scheme not in {"http", "https"}:
            continue
        if _host_key(parsed.hostname) != expected_host:
            continue
        if not _PUBLISHED_INVENTORY_PATH_RE.search(parsed.path or ""):
            continue
        canonical = urlunparse((parsed.scheme, parsed.netloc, parsed.path or "/", "", parsed.query, ""))
        if canonical not in out:
            out.append(canonical)
    return out


def _origin(page: Page, ctx: AdapterContext) -> str:
    """``scheme://host`` for sub-path probing — prefer the settled page URL."""
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
    """Fetch *url* in-session via ``page.evaluate``. Returns ``(status, body)``.

    Status is 0 on network error / unavailable evaluate. Body is ``''`` on
    any non-2xx. Dict-shape JS is the new wire format; string-shape result
    keeps existing ``evaluate`` mocks (which return body strings keyed by
    URL) working as ``(200, body)`` / ``(0, '')``.
    """
    evaluate = getattr(page, "evaluate", None)
    if not callable(evaluate):
        # page=None (production dispatch): direct, bounded public HTTP only.
        # Do not inherit proxy variables or the legacy curl impersonation
        # helper; a blocked response is promoted to the normal render queue.
        return await _direct_public_html(url)
    try:
        result = await evaluate(
            "(u) => fetch(u, {credentials: 'include'})"
            ".then(r => r.text().then(b => ({status: r.status, body: r.ok ? b : ''})))"
            ".catch(() => ({status: 0, body: ''}))",
            url,
        )
    except Exception as exc:  # pragma: no cover — network/SDK variance
        log.debug("PMS-portal fetch failed url=%s err=%s", url, exc)
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


async def _direct_public_html(url: str) -> tuple[int, str]:
    """Fetch one portal document without proxy or fingerprint behavior."""
    import httpx

    try:
        parsed = urlparse(url)
    except ValueError:
        return 0, ""
    expected_host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme not in {"http", "https"} or not expected_host:
        return 0, ""

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(_PORTAL_HTTP_TIMEOUT_SECONDS),
            follow_redirects=False,
            trust_env=False,
            headers={"Accept": "text/html,application/xhtml+xml"},
        ) as client:
            current_url = url
            for _ in range(_PORTAL_MAX_REDIRECTS + 1):
                async with client.stream("GET", current_url) as response:
                    status = int(response.status_code)
                    final_url = str(response.url)
                    final_host = (urlparse(final_url).hostname or "").lower().rstrip(".")
                    if final_host != expected_host:
                        return status, ""
                    if status in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location:
                            return status, ""
                        next_url = urljoin(final_url, location)
                        next_host = (urlparse(next_url).hostname or "").lower().rstrip(".")
                        if next_host != expected_host:
                            return status, ""
                        current_url = next_url
                        continue
                    if not 200 <= status < 300:
                        return status, ""
                    content_length = response.headers.get("content-length")
                    if content_length:
                        try:
                            if int(content_length) > _PORTAL_MAX_BODY_BYTES:
                                return status, ""
                        except ValueError:
                            pass
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > _PORTAL_MAX_BODY_BYTES:
                            return status, ""
                        chunks.append(chunk)
                    body = b"".join(chunks)
                    encoding = response.encoding or "utf-8"
                    try:
                        return status, body.decode(encoding, errors="replace")
                    except LookupError:
                        return status, body.decode("utf-8", errors="replace")
            return 0, ""
    except (httpx.HTTPError, ValueError):
        return 0, ""


async def _fetch(page: Page, url: str) -> str:
    """Body-only convenience wrapper for callers that don't need status."""
    _, body = await _fetch_with_status(page, url)
    return body


def _classify(portal_url: str) -> str:
    """Return ``'resman'`` | ``'rentcafe'`` | ``''`` (unrecognised)."""
    if _RESMAN_ENTRY_RE.search(portal_url):
        return "resman"
    if _RENTCAFE_PORTAL_RE.search(portal_url):
        return "rentcafe"
    if _SPHEREXX_PORTAL_RE.search(portal_url):
        return "spherexx"
    return ""


def _parse_portal_html(
    kind: str,
    html: str,
    source_url: str,
    *,
    expected_resman_property: str = "",
    require_resman_property: bool = False,
) -> list[dict]:
    """Dispatch to the existing tier-1 PMS SSR parser. Never raises."""
    if not html:
        return []
    try:
        if kind == "resman":
            data = _extract_resman_unittypes(html)
            if not data:
                return []
            if expected_resman_property and not _resman_groups_match_property(
                data,
                expected_resman_property,
                require_present=require_resman_property,
            ):
                return []
            return parse_resman_unittypes(data, source_url)
        if kind == "rentcafe":
            return parse_securecafe_availableunits(html, source_url)
        if kind == "spherexx":
            from ma_poc.pms.adapters.spherexx import (
                parse_spherexx_legacy_availability,
            )

            return parse_spherexx_legacy_availability(html, source_url)
    except Exception as exc:  # pragma: no cover — parser internal failure
        log.debug("PMS-portal parse failed kind=%s err=%s", kind, exc)
    return []


async def _recover_rentcafe_applicant(
    candidate_url: str,
    ctx: AdapterContext,
) -> list[dict]:
    """Recover strict native units from a migrated SecureCafe Applicant API.

    The marketing page still publishes the legacy
    ``*.securecafe.com/onlineleasing/<slug>`` link after a tenant migrates its
    inventory UI to ``*.securecafeapplicant.com``.  The legacy
    ``availableunits.aspx`` request then returns an empty/challenge shell, so
    the universal portal hop previously stopped with no rows even though the
    public Applicant API had current apartments.

    Reuse RentCafe's property-bound candidate resolver.  It requires the exact
    published tenant+slug, a matching theme/property identity, one native
    property id repeated by the payload, a physical apartment code, and a
    positive rent.  This universal path is deliberately direct-only: it never
    starts Hyperbrowser or any unlocker/solver path.  The caller can retain its
    existing plan catalogue; this helper returns only native priced units.
    """
    try:
        from ma_poc.core.identity import unit_has_real_anchor
        from ma_poc.pms.adapters.base import AdapterResult
        from ma_poc.pms.adapters.rentcafe import (
            _try_securecafe_applicant_candidate,
        )

        probe_result = AdapterResult()
        rows = await _try_securecafe_applicant_candidate(
            candidate_url,
            ctx,
            probe_result,
            allow_hyperbrowser=False,
        )
        strict_rows = [
            row
            for row in rows
            if isinstance(row, dict)
            and unit_has_real_anchor(row)
            and isinstance(row.get("market_rent_low"), (int, float))
            and not isinstance(row.get("market_rent_low"), bool)
            and row["market_rent_low"] > 0
        ]
        if not strict_rows:
            return []
        tier = str(probe_result.tier_used or "").strip()
        if tier:
            for row in strict_rows:
                row["extraction_tier"] = tier
        return strict_rows
    except Exception as exc:  # pragma: no cover - defensive routing seam
        log.debug("Applicant portal recovery failed url=%s err=%s", candidate_url, exc)
        return []


async def recover_pms_portal(
    page: Page,
    ctx: AdapterContext,
) -> list[dict]:
    """Find a known PMS portal URL on / one hop from the page, parse it.

    Returns SSR-parsed unit dicts (one row per real apartment), or ``[]``
    when no recognised portal is discoverable (so a genuinely no-portal
    site is unaffected). Never raises.
    """
    evaluate = getattr(page, "evaluate", None)
    # No longer a hard page-gate: under page=None, step 1 scans the already-
    # fetched RENDER body for the portal URL and the sub-path probes fall back
    # to curl_cffi. Task #37 Track 1. Live-page path unchanged when present.
    from ma_poc.pms.adapters._probe import body_html_from_ctx

    # 1. Cheap path — the current page IS a public ResMan entry route, or a
    # portal URL is already linked on the page / present in its body.  Warm
    # profiles commonly point winning_page_url straight at Access/SignIn;
    # omitting the current URL made that strongest learned route invisible to
    # the resolver even after link-hop fetched it successfully.
    candidates: list[str] = []
    current_url = ""
    if callable(evaluate):
        try:
            current_url = str(getattr(page, "url", "") or "")
        except Exception:
            current_url = ""
    else:
        fr = getattr(ctx, "fetch_result", None)
        current_url = str(getattr(fr, "final_url", "") or "")
    current_url = current_url or str(getattr(ctx, "base_url", "") or "")
    current_match = _RESMAN_ENTRY_RE.search(current_url)
    if current_match:
        candidates.append(unescape(current_match.group(0)))

    if callable(evaluate):
        try:
            live = await evaluate(_LIVE_PORTAL_SRC_JS)
            if isinstance(live, list):
                candidates.extend(u for u in live if isinstance(u, str) and u)
        except Exception as exc:
            log.debug("PMS-portal live scan failed err=%s", exc)
    else:
        body = body_html_from_ctx(ctx)
        if body:
            candidates.extend(_portal_urls_from_html(body))
    candidates = list(dict.fromkeys(candidates))

    # Production ``page=None`` navigation seam. The current property body may
    # link to a same-origin inventory page which then embeds the exact vendor
    # portal. Follow at most three explicitly authored inventory anchors;
    # never synthesize or brute-force a route in this branch.
    if not candidates and not callable(evaluate):
        body = body_html_from_ctx(ctx)
        base_url = str(getattr(getattr(ctx, "fetch_result", None), "final_url", "") or "") or str(
            getattr(ctx, "base_url", "") or ""
        )
        for inventory_url in _published_inventory_urls(body, base_url)[:3]:
            _, inventory_html = await _fetch_with_status(page, inventory_url)
            if not inventory_html:
                continue
            discovered = _portal_urls_from_html(inventory_html)
            if discovered:
                candidates = discovered
                setattr(ctx, "_pms_portal_published_inventory_url", inventory_url)
                break

    # 2. Probe well-known sub-paths; pull the portal URL out of each body.
    #    First hit wins (probing order is by observed frequency). PAGE-ONLY:
    #    at page=None a blanket curl_cffi probe on every 0-unit property is a
    #    cost/latency risk; the body scan (step 1) covers the in-body case.
    if not candidates and callable(evaluate):
        origin = _origin(page, ctx)
        if origin:
            for path in _PORTAL_SUBPATHS:
                html = await _fetch(page, origin + path)
                if not html:
                    continue
                discovered = _portal_urls_from_html(html)
                if discovered:
                    candidates = discovered
                    break

    # 3. Fetch each portal candidate and parse with its tier-1 parser.
    #    First canonical unit+numeric-rent parse wins. Walking the list (vs
    #    first-only) is cheap and makes us resilient to a stale anchor pointing
    #    at an expired ``p=<guid>``. Plan-only rows remain available as fallback
    #    evidence, but never stop the candidate walk.
    #
    #    A failed SecureCafe code-only fetch records its strict canonical URL as
    #    a context-scoped portal hint. The caller promotes that hint to the
    #    existing bounded link-hop queue, which performs a normal render fetch;
    #    this helper adds no challenge solver, proxy, or fingerprint behavior.
    from ma_poc.pms.adapters._universal_recovery import is_bot_block, mark_blocked

    best_plan_rows: list[dict[str, Any]] = []
    failed_rentcafe_urls: list[str] = []
    portal_fetch_cache: dict[str, tuple[int, str]] = {}
    for src in candidates:
        kind = _classify(src)
        if not kind:
            continue
        # For SecureCafe the captured entry can be any onlineleasing URL
        # (guestlogin.aspx / floorplans.aspx / etc.). Canonicalize to the
        # availableunits.aspx form before fetching — that's the SSR
        # table parse_securecafe_availableunits expects.
        expected_resman_property = ""
        require_resman_property = False
        if kind == "resman":
            fetch_url, expected_resman_property = _canonical_resman_availability(src)
            if not fetch_url:
                entry_status, entry_html = await _fetch_with_status(page, unescape(src))
                if is_bot_block(entry_status):
                    mark_blocked(ctx, "pms_portal_hop:resman_entry", src, entry_status)
                fetch_url, expected_resman_property = _canonical_resman_availability(
                    src,
                    entry_html,
                )
                require_resman_property = bool(fetch_url)
            if not fetch_url:
                continue
        else:
            fetch_url = _to_rentcafe_availableunits(src)
        cached_fetch = portal_fetch_cache.get(fetch_url)
        if cached_fetch is not None:
            status, html = cached_fetch
        else:
            status, html = await _fetch_with_status(page, fetch_url)
            if is_bot_block(status):
                mark_blocked(ctx, f"pms_portal_hop:{kind}", fetch_url, status)
            # SecureCafe's raw SSR table is destroyed by ordinary browser DOM
            # rendering, while plain public HTTP commonly returns 403.  When
            # the operator has explicitly selected the production-safe
            # Hyperbrowser backend, use its existing same-origin *raw* GET on
            # that exact, canonical URL. This is a clean browser + residential
            # route with ``solveCaptchas=False`` hard-coded in the backend; no
            # unlocker, FlareSolverr, CAPTCHA solver, or fingerprint rotation
            # is reachable.
            #
            # Gate on a bot-wall status rather than every empty parse: a
            # genuine 200 zero-availability page remains authoritative and
            # does not spend a cloud-browser call. The strict canonicaliser
            # above is the SSRF boundary, and the body cap matches the direct
            # public fetch lane.
            if kind == "rentcafe" and is_bot_block(status):
                try:
                    from ma_poc.config.feature_flags import hb_enabled

                    if hb_enabled():
                        from ma_poc.fetch.hyperbrowser_backend import hb_raw_get

                        hb_status, hb_html = await hb_raw_get(
                            fetch_url,
                            str(getattr(ctx, "property_id", "") or "?"),
                        )
                        if (
                            hb_status == 200
                            and hb_html
                            and len(hb_html.encode("utf-8", errors="replace")) <= _PORTAL_MAX_BODY_BYTES
                        ):
                            status, html = hb_status, hb_html
                        elif is_bot_block(hb_status):
                            mark_blocked(
                                ctx,
                                "pms_portal_hop:rentcafe_hb_raw",
                                fetch_url,
                                hb_status,
                            )
                except Exception as exc:  # pragma: no cover - never sink fallback
                    log.debug(
                        "SecureCafe HB raw recovery failed url=%s err=%s",
                        fetch_url,
                        exc,
                    )
            portal_fetch_cache[fetch_url] = (status, html)
        rows = _validated_portal_rows(
            _parse_portal_html(
                kind,
                html,
                fetch_url,
                expected_resman_property=expected_resman_property,
                require_resman_property=require_resman_property,
            )
        )
        if _has_canonical_unit(rows):
            if kind == "spherexx":
                published_url = str(getattr(ctx, "_pms_portal_published_inventory_url", "") or "")
                for row in rows:
                    if published_url:
                        row["source_portal_url"] = published_url
                    if not row.get("source_property_name"):
                        row["source_property_name"] = str(getattr(ctx, "property_name", "") or "")
                    row["source_property_provenance"] = "published_spherexx_availability_iframe"
            return rows
        if rows and not best_plan_rows:
            best_plan_rows = rows
        if kind == "rentcafe" and "applicant portal" in (html or "").casefold():
            # Yardi migrated many published legacy portals to Applicant V2
            # while retaining the same tenant/slug on the marketing page.
            # Enter that second API only when the legacy endpoint explicitly
            # identifies itself as the Applicant shell. A plain 403, an
            # authoritative zero-availability page, or a rentless legacy row
            # remains on the normal render-handoff/empty path and never spends
            # an unrelated theme request.
            applicant_rows = await _recover_rentcafe_applicant(src, ctx)
            if applicant_rows:
                return applicant_rows
        if kind == "rentcafe":
            if fetch_url not in failed_rentcafe_urls:
                failed_rentcafe_urls.append(fetch_url)

    for failed_url in failed_rentcafe_urls:
        _record_rentcafe_portal_hint(ctx, failed_url)
    return best_plan_rows
