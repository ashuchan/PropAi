"""Canonical grading of AppFolio URLs — ONE copy of the rule.

Why this module exists
----------------------
Three independent places used to decide, each in their own words, whether a
``*.appfolio.com`` URL means "this property's inventory lives here":

* ``ma_poc.pms.resolver.normalize_appfolio_url`` — rewrote *any* tenant URL to
  ``{tenant}.appfolio.com/listings``;
* ``ma_poc.pms.detector`` — routed ``/listings | /connect | /apply`` to the
  AppFolio adapter as a "definitive leasing backend";
* ``ma_poc.pms.adapters._appfolio_embed`` — the embed-recovery scope gate;
* ``ma_poc.pms.adapters.appfolio`` — the main adapter's SSR branch.

They drifted, and the drift is what shipped one management company's account
roster as many different properties' inventory. The rule below is the single
source of truth; import it, never restate it.

The rule
--------
An AppFolio **tenant subdomain** identifies the management company, not the
property. Only ONE URL shape is evidence that an operator published an
inventory surface for the page you are standing on:

    the listings INDEX — path exactly ``/listings`` or ``/listings/``,
    query string or fragment allowed.

Everything else is *tenant-only* evidence and identifies the account:

    ``/``                                bare tenant root
    ``/connect/users/sign_in``           resident portal / pay-rent button
    ``/connect/users/request_access``    resident portal signup
    ``/apply``, ``/oportal/...``         application / owner portal
    ``/listings/detail/{uuid}``          one unit's detail page
    ``/listings/showings/new?...``       one unit's tour form
    the tenant-application form          one unit's application form — its path
                                         literal lives in
                                         ``resolver._BLACKLISTED_PATH_RE``, the
                                         single source of truth for it, and is
                                         deliberately not repeated here
    ``/realms/.../openid-connect/auth``  AppFolio SSO (``account.appfolio.com``)

The table test below the fold is structural: it needs none of those literals.
It admits the index and rejects every deeper path by shape.

A per-unit deep link proves a unit exists; it does not license fetching the
whole account. Tenant-only evidence must never be silently upgraded into an
account-wide roster fetch.

Note on scope: ``filters[property_list]=<name>`` is AppFolio's *server-side*
property filter — a URL carrying it returns one property's listings rather
than the account's. It is always carried in the query string of a listings
index, so :func:`is_listings_index_url` already admits it;
:func:`property_list_scope` exposes the value, and
:func:`is_account_index_url` is the "index AND unscoped" question the
adapters actually ask when deciding whether a body is a whole roster.
"""

from __future__ import annotations

import re
from urllib.parse import quote, urlsplit

__all__ = [
    "APPFOLIO_RESERVED_SLUGS",
    "appfolio_tenant_slug",
    "is_account_index_url",
    "is_appfolio_tenant_url",
    "is_listings_index_url",
    "is_scoped_listings_url",
    "property_list_scope",
    "scoped_listings_url",
]

# Sub-domains on ``appfolio.com`` that are AppFolio's own infrastructure, not
# an operator's tenant instance. Mirrors ``appfolio._APPFOLIO_SKIP_SLUGS``,
# which the slug-discovery path has used since 2026-05; kept here because this
# module must not import the adapter (the adapter imports this).
APPFOLIO_RESERVED_SLUGS = frozenset(
    {
        "www", "app", "support", "secure", "tenant", "tenants", "owner",
        "owners", "demo",
        # 2026-07-30: ``account.appfolio.com`` is AppFolio's centralized OAuth /
        # account host (``/realms/foliospace/protocol/openid-connect``), never a
        # property tenant. Frontera at Pioneer Meadows (leasefrontera.com) links
        # to it FOUR times before its real ``frontera.appfolio.com`` tenant, so
        # find_appfolio_slug returned "account", fetched the wrong host, and the
        # property shipped TIER_1_API_APPFOLIO_EMPTY_PLAN_LEVEL.
        "account", "accounts",
    }
)

# Both the percent-encoded (``%5B``/``%5D``) and literal (``[``/``]``) bracket
# forms occur in the wild.
_PROPERTY_LIST_QS_RE = re.compile(
    r"filters(?:%5B|\[)property_list(?:%5D|\])=([^&#\s\"'<>]+)",
    re.IGNORECASE,
)


def appfolio_tenant_slug(url: str) -> str:
    """Return the operator's tenant slug from *url*, or ``""``.

    Host grading, not a regex over the whole URL — a lookalike host such as
    ``chamberlin.appfolio.com.evil.example`` or ``evil-appfolio.com`` has to
    fail on the HOST, and a string search cannot see where the host ends.

    A leading ``www.`` label is stripped before grading, so both
    ``chamberlin.appfolio.com`` and ``www.chamberlin.appfolio.com`` yield
    ``chamberlin`` while ``www.appfolio.com`` (AppFolio's own marketing site)
    and the bare apex ``appfolio.com`` yield ``""``.
    """
    raw = (url or "").strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
    except ValueError:
        return ""
    if parts.scheme.lower() not in ("http", "https"):
        return ""
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if not host.endswith(".appfolio.com"):
        return ""
    label = host[: -len(".appfolio.com")]
    # Exactly one label — ``a.b.appfolio.com`` is not a tenant instance.
    if not label or "." in label:
        return ""
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", label):
        return ""
    return label


def is_appfolio_tenant_url(url: str) -> bool:
    """True when *url* is on a ``{tenant}.appfolio.com`` subdomain.

    ``www.appfolio.com`` (AppFolio's own marketing site) and the bare apex
    ``appfolio.com`` are not tenant instances and return False. Reserved
    infrastructure sub-domains (``secure``, ``support``, …) ARE tenant hosts
    for this question — they are simply never an inventory surface, which is
    :func:`is_listings_index_url`'s job to say.
    """
    return bool(appfolio_tenant_slug(url))


def is_listings_index_url(url: str) -> bool:
    """True when *url* is the listings SSR **index** rather than a per-unit
    deep link, a portal/auth path, or a bare tenant root.

    This is the only shape that counts as operator-published evidence that a
    property's inventory is reachable at that URL. See the module docstring.

    Reserved slugs are excluded: ``secure.appfolio.com/listings`` is AppFolio's
    own infrastructure, never an operator roster.
    """
    slug = appfolio_tenant_slug(url)
    if not slug or slug in APPFOLIO_RESERVED_SLUGS:
        return False
    path = (urlsplit(url.strip()).path or "/").rstrip("/").lower()
    # ``/listings`` exactly. ``rstrip('/')`` makes ``/listings/`` equal to it
    # and leaves ``/listings/detail/x`` and ``/listingsmore`` distinct.
    return path == "/listings"


def property_list_scope(url: str) -> str:
    """Return the raw (still URL-encoded) ``filters[property_list]`` value
    carried by *url*, or ``""`` when the URL is account-wide.
    """
    m = _PROPERTY_LIST_QS_RE.search(url or "")
    if not m:
        return ""
    # HTML-escaped ampersands (``&amp;``) leak into hrefs harvested straight
    # out of markup; they terminate the value, they are not part of it.
    return m.group(1).split("&")[0]


def is_scoped_listings_url(url: str) -> bool:
    """True when *url* carries AppFolio's server-side property-list filter."""
    return bool(property_list_scope(url))


def is_account_index_url(url: str) -> bool:
    """True when *url* is a tenant's ACCOUNT-WIDE listings index.

    That is: a listings index (see :func:`is_listings_index_url`) that carries
    no ``filters[property_list]`` scope. Live-verified 2026-07-28:
    ``olympicmanagement.appfolio.com/listings`` → 932 cards metro-wide; the
    same URL with ``?filters[property_list]=COLLEGE PARK`` → 12 cards, all in
    98503.
    """
    return is_listings_index_url(url) and not is_scoped_listings_url(url)


def scoped_listings_url(url: str, property_group: str) -> str:
    """Return *url*'s tenant listings index narrowed to one *property_group*.

    AppFolio's ``/listings`` endpoint accepts the per-property scope as
    ``filters[property_list]={group}``; any scope the URL already carried is
    replaced. Non-AppFolio input is returned with its query stripped rather
    than raising — callers only ever hand this a tenant URL.
    """
    parts = urlsplit((url or "").strip())
    if not parts.scheme or not parts.netloc:
        return url
    qs = f"filters%5Bproperty_list%5D={quote(property_group)}"
    return f"{parts.scheme}://{parts.netloc}/listings?{qs}"
