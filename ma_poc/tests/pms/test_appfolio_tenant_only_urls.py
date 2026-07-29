"""An AppFolio tenant subdomain identifies the MANAGEMENT COMPANY.

Regression cover for defects #3 and #4 of the 2026-07-28 AppFolio identity
audit: the two feeders that put a management company's whole account roster in
front of the SSR parser.

Reproduction (run 2026-07-27-full-0d54ca7, run-recorded ``scrape_url`` in
``events.jsonl``, denominator = all 4,982 properties):

  * 242 properties were scraped at an UNSCOPED ``{tenant}.appfolio.com/listings``
    account roster; only 10 at a property-scoped URL.
  * 127 of those 242 still carried ``a=cw`` — a parameter that exists only on
    AppFolio's ``/connect/users/sign_in?a=cw&utm_source=apmsites_v3&
    utm_campaign=pay_rent_button`` pay-rent button. ``normalize_appfolio_url``
    dropped ``utm_*`` and kept ``a``, so ``a=cw`` on a ``/listings`` URL is that
    function's own fingerprint.
  * 27 rosters fed more than one property. ``olympicmanagement.appfolio.com``
    was scraped as 8 properties across Lacey / Lakewood / Olympia / Poulsbo /
    Sumner / Federal Way and 7 received the identical 294 units.
    ``terracemgmt.appfolio.com`` shipped the same 214 units as properties in
    Chicopee MA, Columbus OH and New Haven CT.
    ``yourmetropolitan.appfolio.com`` shipped one listing as both Tareyton
    Estates (Langhorne PA) and Metropolitan Bala (Philadelphia PA).
  * 11,761 unit rows in total came from an unscoped roster.

The rule these tests pin: only a listings INDEX (or any URL carrying AppFolio's
server-side ``filters[property_list]`` scope) is evidence that a property's
inventory is reachable at a tenant URL. A portal/auth path, an owner portal, an
SSO endpoint, a per-unit deep link and the bare tenant root are all tenant-only.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from ma_poc.pms.appfolio_urls import (
    is_appfolio_tenant_url,
    is_listings_index_url,
    is_scoped_listings_url,
    property_list_scope,
)
from ma_poc.pms.detector import detect_pms
from ma_poc.pms.resolver import (
    is_tenant_only_appfolio_url,
    normalize_appfolio_url,
    resolve_target,
)

# ── the shared predicate ─────────────────────────────────────────────────────
# Table-driven, and deliberately loaded with cases it must NOT match: the whole
# defect was a rule that said yes to everything on the host.

_INDEX_TABLE: list[tuple[str, bool, bool, str]] = [
    # url,                                                     index, tenant, why
    ("https://becovic.appfolio.com/listings", True, True, "listings index"),
    ("https://becovic.appfolio.com/listings/", True, True, "index, trailing slash"),
    (
        "https://illumepm.appfolio.com/listings?filters%5Bproperty_list%5D=brookside",
        True, True, "scoped index (live-verified: 12 cards, all 97222)",
    ),
    ("https://olympicmanagement.appfolio.com/listings#top", True, True, "index + fragment"),
    ("http://cmnd.appfolio.com/listings", True, True, "http scheme"),
    ("https://BECOVIC.AppFolio.COM/LISTINGS", True, True, "mixed case"),
    ("https://wwwfoo.appfolio.com/listings", True, True, "www-prefixed slug is a real tenant"),
    # ── must NOT be read as an inventory surface ──
    ("https://chamberlin.appfolio.com/connect/users/sign_in", False, True, "resident portal"),
    (
        "https://ironridgecapital.appfolio.com/connect/users/sign_in?a=cw&utm_source=apmsites_v3",
        False, True, "pay-rent button — the 127-property fingerprint",
    ),
    (
        "https://dougwettonproperties.appfolio.com/connect/users/request_access",
        False, True, "portal signup",
    ),
    ("https://adcoapts.appfolio.com/connect", False, True, "portal root"),
    (
        "https://pillarrei.appfolio.com/listings/rental_applications/new?listable_uid=x",
        False, True, "per-unit application (Middlebury Crossing shape)",
    ),
    (
        "https://bltliveworkplay.appfolio.com/listings/showings/new?listable_uid=x",
        False, True, "per-unit tour form",
    ),
    ("https://becovic.appfolio.com/listings/detail/9f1c", False, True, "per-unit detail"),
    ("https://becovic.appfolio.com/listings/233", False, True, "per-unit numeric"),
    ("https://richelsonmanagement.appfolio.com/", False, True, "bare tenant root"),
    ("https://becovic.appfolio.com", False, True, "bare root, no slash"),
    (
        "https://account.appfolio.com/realms/foliospace/protocol/openid-connect/auth?client_id=x",
        False, True, "AppFolio SSO — 9 properties were scraped at this host's roster",
    ),
    ("https://vdbprop.appfolio.com/oportal/dashboard", False, True, "owner portal"),
    ("https://becovic.appfolio.com/listingsmore", False, True, "prefix-only path"),
    ("https://becovic.appfolio.com/listings-archive", False, True, "hyphen path"),
    # ── not an AppFolio tenant at all ──
    ("https://www.appfolio.com/listings", False, False, "AppFolio's own marketing host"),
    ("https://www.appfolio.com/property-manager", False, False, "marketing footer credit"),
    ("https://appfolio.com/listings", False, False, "bare apex, not a tenant"),
    ("https://becovic.appfolio.com.evil.example/listings", False, False, "suffix-spoof host"),
    ("https://notappfolio.com/listings", False, False, "different domain"),
    ("https://example.com/listings", False, False, "unrelated site"),
    ("", False, False, "empty"),
    ("not a url", False, False, "garbage"),
]


@pytest.mark.parametrize("url,is_index,is_tenant,why", _INDEX_TABLE)
def test_appfolio_url_grading_table(url: str, is_index: bool, is_tenant: bool, why: str) -> None:
    assert is_listings_index_url(url) is is_index, f"is_listings_index_url: {why}"
    assert is_appfolio_tenant_url(url) is is_tenant, f"is_appfolio_tenant_url: {why}"


@pytest.mark.parametrize("url,expected", [
    (
        "https://illumepm.appfolio.com/listings?1785&filters%5Bproperty_list%5D=brookside&x=1",
        "brookside",
    ),
    ("https://h.appfolio.com/listings?filters[property_list]=East%20Hampton", "East%20Hampton"),
    # ``&amp;`` leaks in from raw markup; it terminates the value.
    ("https://h.appfolio.com/listings?filters%5Bproperty_list%5D=A&amp;b=2", "A"),
    ("https://h.appfolio.com/listings", ""),
    ("https://h.appfolio.com/connect/users/sign_in?a=cw", ""),
])
def test_property_list_scope_extraction(url: str, expected: str) -> None:
    assert property_list_scope(url) == expected
    assert is_scoped_listings_url(url) is bool(expected)


# ── defect #3: normalize_appfolio_url no longer manufactures a roster ────────


@pytest.mark.parametrize("url,why", [
    (
        "https://ironridgecapital.appfolio.com/connect/users/sign_in"
        "?a=cw&utm_source=apmsites_v3&utm_campaign=pay_rent_button",
        "Southcrest (pid 11399) was scraped at "
        "ironridgecapital.appfolio.com/listings?a=cw and shipped 175 rows",
    ),
    (
        "https://olympicmanagement.appfolio.com/connect/users/sign_in",
        "8 properties in 6 Washington cities were scraped at this tenant's roster",
    ),
    ("https://yourmetropolitan.appfolio.com/connect/users/sign_in", "Tareyton + Metropolitan Bala"),
    ("https://adcoapts.appfolio.com/connect", "portal root"),
    ("https://gcmultifamily.appfolio.com/apply/abc-123/start", "application deep link"),
    ("https://vdbprop.appfolio.com/oportal/dashboard", "owner portal"),
    (
        "https://account.appfolio.com/realms/foliospace/protocol/openid-connect/auth?client_id=x",
        "AppFolio SSO endpoint",
    ),
    ("https://richelsonmanagement.appfolio.com/", "bare tenant root"),
    ("https://becovic.appfolio.com", "bare tenant root, no trailing slash"),
    ("https://becovic.appfolio.com/some/random/path", "arbitrary tenant path"),
])
def test_normalize_appfolio_url_never_manufactures_a_roster(url: str, why: str) -> None:
    """Tenant-only URLs come back untouched — no ``/listings`` is synthesised."""
    assert normalize_appfolio_url(url) == url, why
    assert "/listings" not in normalize_appfolio_url(url).split("?")[0] or "/listings" in url


def test_normalize_appfolio_url_still_cleans_a_real_listings_index() -> None:
    """The index case is untouched: junk params out, filter directives in."""
    out = normalize_appfolio_url(
        "https://hayloftpropmgmt.appfolio.com/listings"
        "?1778663185787&filters%5Bproperty_list%5D=EAST%20HAMPTON"
        "&theme_color=%23676767&utm_source=x"
    )
    assert out.startswith("https://hayloftpropmgmt.appfolio.com/listings?")
    assert "property_list" in out and "EAST" in out
    assert "theme_color" in out
    assert "1778663185787" not in out and "utm_source" not in out


def test_normalize_appfolio_url_honours_an_explicit_property_scope() -> None:
    """A non-index path that names a property via ``filters[property_list]`` is
    NOT tenant-only evidence — it is pointed at /listings with the scope kept."""
    out = normalize_appfolio_url(
        "https://x.appfolio.com/?filters%5Bproperty_list%5D=FOO%20BAR"
    )
    assert out.startswith("https://x.appfolio.com/listings?")
    assert "property_list" in out and "FOO" in out


@pytest.mark.parametrize("url,expected", [
    ("https://chamberlin.appfolio.com/connect/users/sign_in", True),
    ("https://richelsonmanagement.appfolio.com/", True),
    ("https://becovic.appfolio.com/listings/detail/9f1c", True),
    ("https://becovic.appfolio.com/listings", False),
    ("https://becovic.appfolio.com/listings?filters%5Bproperty_list%5D=a", False),
    ("https://x.appfolio.com/?filters%5Bproperty_list%5D=a", False),
    ("https://www.appfolio.com/listings", False),
    ("https://example.com/connect/users/sign_in", False),
])
def test_is_tenant_only_appfolio_url(url: str, expected: bool) -> None:
    assert is_tenant_only_appfolio_url(url) is expected


# ── defect #3: the resolver stops hopping to the account portal ──────────────


def _page(links: list[dict[str, str]] | None = None,
          iframes: list[str] | None = None,
          url: str = "https://vanity.example/") -> AsyncMock:
    page = AsyncMock()
    page.url = url
    page.evaluate = AsyncMock(side_effect=[links or [], iframes or []])
    return page


@pytest.mark.asyncio
async def test_resolver_does_not_hop_to_an_appfolio_resident_portal() -> None:
    """The exact shape behind Southcrest / College Park / Middlebury Crossing:
    a footer pay-rent button and the property's own floor-plans page. Before
    the fix the portal anchor won pass 3a at priority 75 and became
    ``{tenant}.appfolio.com/listings?a=cw`` — the account roster."""
    links = [
        {"href": "https://vanity.example/floor-plans/", "text": "Floor Plans"},
        {
            "href": "https://ironridgecapital.appfolio.com/connect/users/sign_in"
                    "?a=cw&utm_source=apmsites_v3&utm_campaign=pay_rent_button",
            "text": "Pay Rent",
        },
    ]
    result = await resolve_target(
        _page(links=links), "https://vanity.example/", detect_pms("https://vanity.example/")
    )
    assert "appfolio.com" not in result.resolved_url
    assert result.resolved_url == "https://vanity.example/floor-plans/"


@pytest.mark.asyncio
async def test_resolver_still_hops_to_a_real_appfolio_listings_index() -> None:
    """The capability is not withdrawn — an operator-published listings index
    is still followed."""
    links = [
        {"href": "https://vanity.example/floor-plans/", "text": "Floor Plans"},
        {"href": "https://becovic.appfolio.com/listings", "text": "Availability"},
    ]
    result = await resolve_target(
        _page(links=links), "https://vanity.example/", detect_pms("https://vanity.example/")
    )
    assert result.resolved_url == "https://becovic.appfolio.com/listings"


@pytest.mark.asyncio
async def test_resolver_skips_a_tenant_only_appfolio_iframe() -> None:
    """Step 4 is held to the same bar as candidate admission."""
    result = await resolve_target(
        _page(links=[], iframes=["https://chamberlin.appfolio.com/connect/users/sign_in"]),
        "https://vanity.example/",
        detect_pms("https://vanity.example/"),
    )
    assert result.method == "failed"
    assert "appfolio.com" not in result.resolved_url


@pytest.mark.asyncio
async def test_resolver_follows_a_scoped_appfolio_iframe() -> None:
    """brooksidejohnsoncreek.com's shape: a property-scoped listings iframe.
    Live control: ``?filters[property_list]=brookside`` returns 12 cards, all
    in 97222, against an account roster of 78."""
    src = "https://illumepm.appfolio.com/listings?filters%5Bproperty_list%5D=brookside"
    result = await resolve_target(
        _page(links=[], iframes=[src]),
        "https://www.brooksidejohnsoncreek.com/",
        detect_pms("https://www.brooksidejohnsoncreek.com/"),
    )
    assert result.method == "iframe"
    assert "property_list" in result.resolved_url
    assert "brookside" in result.resolved_url


# ── defect #4: the detector grades its AppFolio evidence ─────────────────────


def test_detector_listings_index_still_earns_the_strong_grade() -> None:
    html = (
        "<html><body>"
        '<script src="https://static2.apts247.info/widget.js"></script>'
        '<iframe src="https://gcmultifamily.appfolio.com/listings"></iframe>'
        "</body></html>"
    )
    r = detect_pms("https://vanity.example/", page_html=html)
    assert r.pms == "appfolio"
    assert r.confidence >= 0.92


def test_detector_tenant_only_evidence_still_routes_to_appfolio() -> None:
    """Demoting the GRADE must not drop the ROUTE: AppFolio really is the
    leasing backend, and ``AppFolioAdapter``'s address-filtered vanity path is
    how these properties get their (correctly scoped) units."""
    html = (
        "<html><body>"
        '<a href="https://gcmultifamily.appfolio.com/connect/users/sign_in">'
        "Resident Login</a>"
        "</body></html>"
    )
    r = detect_pms("https://vanity.example/", page_html=html)
    assert r.pms == "appfolio"
    assert "tenant-only" in " ".join(r.evidence)


def test_detector_tenant_only_evidence_does_not_claim_an_inventory_surface() -> None:
    """The 'definitive leasing backend / outranks co-resident marketing
    widgets' claim is reserved for a listings index. A pay-rent button gets
    the tenant-only label instead.

    The CONFIDENCE is intentionally identical for both grades — see the long
    comment at the yield site. Demoting the tenant-only branch was measured
    over all 4,097 saved landing bodies of run 2026-07-27-full-0d54ca7 and
    re-routes 19 properties off ``AppFolioAdapter``, 18 of them currently
    served correctly by its address-filtered paths. This test pins the label,
    and pins that the label did not become a routing change by accident.
    """
    html = (
        "<html><body>"
        '<a href="https://gcmultifamily.appfolio.com/connect/users/sign_in">'
        "Resident Login</a>"
        "</body></html>"
    )
    r = detect_pms("https://vanity.example/", page_html=html)
    joined = " ".join(r.evidence)
    assert "tenant-only" in joined
    assert "definitive leasing backend" not in joined
    assert r.confidence >= 0.92, "grading the evidence must not demote the route"


def test_detector_per_unit_deep_link_is_not_an_inventory_surface() -> None:
    """Middlebury Crossing (pid 50129) carried only an 'Apply Now' link and
    shipped the account roster's 1579 Hamilton Avenue, ZIP 06706, as its unit."""
    html = (
        "<html><body>"
        '<a href="https://genesisaffordablemgmt.appfolio.com/listings/'
        'rental_applications/new?listable_uid=x">Apply Now</a>'
        "</body></html>"
    )
    r = detect_pms("https://vanity.example/", page_html=html)
    assert r.pms == "appfolio"
    assert "tenant-only" in " ".join(r.evidence)


def test_detector_cdn_asset_is_not_a_listings_index() -> None:
    """``listings.cdn.appfolio.com`` is the widget's asset host. It must not
    be read as a published listings index."""
    html = (
        "<html><body>"
        '<script src="https://listings.cdn.appfolio.com/assets/widget.js"></script>'
        '<a href="https://chamberlin.appfolio.com/connect">Portal</a>'
        "</body></html>"
    )
    r = detect_pms("https://vanity.example/", page_html=html)
    assert r.pms == "appfolio"
    assert "tenant-only" in " ".join(r.evidence)
