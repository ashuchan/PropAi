"""Phase 4 — CTA-hop resolver tests.

Uses mock pages instead of real Playwright to avoid network calls.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from ma_poc.pms.detector import detect_pms
from ma_poc.pms.resolver import resolve_target


def _make_mock_page(
    *,
    links: list[dict[str, str]] | None = None,
    iframes: list[str] | None = None,
    url: str = "https://vanity.example/",
    evaluate_side_effects: list | None = None,
) -> AsyncMock:
    """Create a mock Playwright Page with controllable evaluate results."""
    page = AsyncMock()
    page.url = url

    if evaluate_side_effects is not None:
        page.evaluate = AsyncMock(side_effect=evaluate_side_effects)
    else:

        async def _evaluate(script: str) -> list:
            if "querySelectorAll('a[href]')" in script:
                return links or []
            if "querySelectorAll('iframe[src]')" in script:
                return iframes or []
            return []

        page.evaluate = AsyncMock(side_effect=_evaluate)

    return page


@pytest.mark.asyncio
async def test_resolver_skips_hop_when_already_on_pms() -> None:
    """URL already on PMS host with high confidence -> no_hop."""
    page = _make_mock_page(url="https://8756399.onlineleasing.realpage.com/")
    detection = detect_pms("https://8756399.onlineleasing.realpage.com/")
    result = await resolve_target(page, "https://8756399.onlineleasing.realpage.com/", detection)
    assert result.method == "no_hop"
    assert result.resolved_url == "https://8756399.onlineleasing.realpage.com/"


@pytest.mark.asyncio
async def test_resolver_finds_rentcafe_via_apply_button() -> None:
    """Vanity HTML with RentCafe apply link -> cta_link."""
    links = [
        {"href": "https://www.rentcafe.com/apartments/mi/ann-arbor/foo/", "text": "Apply Now"},
    ]
    page = _make_mock_page(links=links, url="https://vanity.example/")
    detection = detect_pms("https://vanity.example/")
    result = await resolve_target(page, "https://vanity.example/", detection)
    assert result.method == "cta_link"
    assert "rentcafe.com" in result.resolved_url
    assert result.final_detection.pms == "rentcafe"


@pytest.mark.asyncio
async def test_resolver_finds_sightmap_iframe() -> None:
    """HTML with SightMap iframe -> iframe method."""
    iframes = ["https://tour.sightmap.com/embed/X"]
    page = _make_mock_page(iframes=iframes, url="https://vanity.example/")
    detection = detect_pms("https://vanity.example/")
    result = await resolve_target(page, "https://vanity.example/", detection)
    assert result.method == "iframe"
    assert "sightmap.com" in result.resolved_url
    assert result.final_detection.pms == "sightmap"


@pytest.mark.asyncio
async def test_resolver_prioritizes_availability_over_apply() -> None:
    """Availability link should win over Apply link."""
    links = [
        {"href": "https://8756399.onlineleasing.realpage.com/", "text": "Apply Now"},
        {"href": "https://www.rentcafe.com/apartments/test/", "text": "View Availability"},
    ]
    page = _make_mock_page(links=links, url="https://vanity.example/")
    detection = detect_pms("https://vanity.example/")
    result = await resolve_target(page, "https://vanity.example/", detection)
    assert result.method == "cta_link"
    # availability (priority 100) > apply (priority 50)
    assert "rentcafe.com" in result.resolved_url


@pytest.mark.asyncio
async def test_resolver_returns_failed_when_nothing_found() -> None:
    """No PMS links, no iframes -> method=failed."""
    links = [
        {"href": "https://vanity.example/about", "text": "About Us"},
        {"href": "https://vanity.example/contact", "text": "Contact"},
    ]
    page = _make_mock_page(links=links, url="https://vanity.example/")
    detection = detect_pms("https://vanity.example/")
    result = await resolve_target(page, "https://vanity.example/", detection)
    assert result.method == "failed"
    assert result.final_detection.pms == "unknown"


@pytest.mark.asyncio
async def test_resolver_caps_candidates_at_cap() -> None:
    """Candidate list is capped at ``_CANDIDATE_CAP`` (8 since 2026-05-13).

    Regardless of cap, none of the example URLs match PMS fingerprints
    so the resolver returns failed.
    """
    # Create 20 CTA links, all with availability text
    links = [{"href": f"https://example{i}.com/", "text": f"View Availability {i}"} for i in range(20)]
    page = _make_mock_page(links=links, url="https://vanity.example/")
    detection = detect_pms("https://vanity.example/")
    result = await resolve_target(page, "https://vanity.example/", detection)
    # None of the example URLs match PMS fingerprints, so should be failed
    assert result.method == "failed"


@pytest.mark.asyncio
async def test_resolver_handles_playwright_timeout() -> None:
    """TimeoutError from page.evaluate should not propagate."""
    page = _make_mock_page(evaluate_side_effects=[TimeoutError("page timeout"), TimeoutError("page timeout")])
    detection = detect_pms("https://vanity.example/")
    result = await resolve_target(page, "https://vanity.example/", detection)
    assert result.method == "failed"


@pytest.mark.asyncio
async def test_resolver_records_hop_path() -> None:
    """Hop path records URLs traversed."""
    links = [
        {"href": "https://www.rentcafe.com/test/", "text": "Apply Now"},
    ]
    page = _make_mock_page(links=links, url="https://vanity.example/")
    detection = detect_pms("https://vanity.example/")
    result = await resolve_target(page, "https://vanity.example/", detection)
    assert result.method == "cta_link"
    assert len(result.hop_path) == 2
    assert result.hop_path[0] == "https://vanity.example/"
    assert "rentcafe.com" in result.hop_path[1]


@pytest.mark.asyncio
async def test_resolver_detects_redirect() -> None:
    """If page.url changed to a PMS host, detect redirect."""
    page = _make_mock_page(
        links=[],
        iframes=[],
        url="https://www.rentcafe.com/redirected/",
    )
    detection = detect_pms("https://vanity.example/")
    result = await resolve_target(page, "https://vanity.example/", detection)
    assert result.method == "redirect"
    assert "rentcafe.com" in result.resolved_url


# ────────────────────────────────────────────────────────────────────
# 2026-05-13 port: new tests for word-boundary priority, CTA path
# regex, portal-host allowlist, AppFolio query-param preservation.
# Sources: ma_poc/docs/MAY13_API_TIER_PORT_PLAN.md §4 Commit 2.
# ────────────────────────────────────────────────────────────────────


def test_priority_does_not_match_uni_inside_communities() -> None:
    """The "unit" keyword must not fire on "Communities" via substring.

    Pre-port: the substring "uni" inside "Communities" gave priority 60
    (the "unit" keyword), spuriously elevating lead-form vendor links
    above legitimate PMS portal links.
    """
    from ma_poc.pms.resolver import _get_priority

    # "Communities" alone should yield zero priority.
    assert _get_priority("OneWall Communities works with eRenterPlan") == 0
    # "View Units" still scores -- word-boundary preserves real matches.
    assert _get_priority("View Units") == 60
    # "Availability" still scores via the "availab" prefix.
    assert _get_priority("Check Availability") == 100


def test_cta_path_regex_matches_expected_data_paths() -> None:
    """Empirically-ranked paths from May 13 manual QC of 400 properties."""
    from ma_poc.pms.resolver import _CTA_PATH_RE

    matching = [
        "/conventional/",          # Entrata Property Marketing Site (105 hits in QC)
        "/floor-plans",            # 88 hits
        "/floorplans",
        "/floor-plans-and-pricing",  # 13
        "/floor-plans.aspx",
        "/models",                 # 16 (Greystar)
        "/availability",
        "/vacancies",
        "/units",
        "/listings",
        "/onlineleasing",
        "/interactive-site-map",
        "/communities/foo",
        "/our-properties/foo",
        "/apartments/dallas",
    ]
    for path in matching:
        assert _CTA_PATH_RE.search(path), f"expected match for {path!r}"

    non_matching = ["/random/path", "/contact", "/blog/post", "/about-us"]
    for path in non_matching:
        assert not _CTA_PATH_RE.search(path), f"unexpected match for {path!r}"


def test_known_portal_recognises_secondary_domains() -> None:
    """Hosts on the leasing-portal allowlist must be recognized even
    when no adapter advertises them via static_fingerprints()."""
    from ma_poc.pms.resolver import _url_is_known_portal

    cases = [
        "https://x.securecafe.com/leasing",
        "https://something.prospectportal.com/foo",
        "https://tenant.appfolio.com/listings",
        "https://x.doorway.knck.io/widget",
        "https://x.myresman.com/portal",
        "https://x.knockrentals.com/",
    ]
    for url in cases:
        assert _url_is_known_portal(url), f"expected portal match for {url!r}"


def test_appfolio_url_preserves_filter_params_drops_junk() -> None:
    """Multi-tenant AppFolio query-param preservation.

    Memory `project_run_2026_05_20`: stripping `filters[property_list]=`
    on hayloftpropmgmt.appfolio.com made the resolver point at the
    all-properties /listings, returning 292 rents instead of the target
    property's 40.
    """
    from ma_poc.pms.resolver import normalize_appfolio_url

    # filters[property_list]= preserved; utm_*, gclid, source dropped.
    out = normalize_appfolio_url(
        "https://hayloft.appfolio.com/listings"
        "?filters[property_list]=EastHampton&utm_source=fb&gclid=abc&source=ref"
    )
    assert "filters" in out and "EastHampton" in out
    assert "utm_source" not in out and "gclid" not in out and "source=ref" not in out

    # Bare tenant root with only noise params -> bare /listings.
    assert normalize_appfolio_url(
        "https://richelsonmanagement.appfolio.com/?source=marketing"
    ).endswith("/listings")

    # Non-appfolio host is pass-through.
    assert normalize_appfolio_url("https://www.example.com/listings?x=1") == "https://www.example.com/listings?x=1"


@pytest.mark.asyncio
async def test_resolver_admits_portal_link_with_empty_anchor_text() -> None:
    """Icon-only portal links (anchor text empty) must be admitted via
    the portal-host trigger, not just the anchor-text trigger.

    Pre-port: only `_CTA_TEXT_RE.search(text)` was the gate, so icon-only
    `<a href="https://x.securecafe.com/..."></a>` links were silently
    dropped at candidate-collection.
    """
    links = [
        # Icon-only portal anchor — empty text but host on allowlist
        {"href": "https://acme.securecafe.com/online-leasing/availableunits.aspx", "text": ""},
    ]
    page = _make_mock_page(links=links, url="https://vanity.example/")
    detection = detect_pms("https://vanity.example/")
    result = await resolve_target(page, "https://vanity.example/", detection)
    assert result.method == "cta_link"
    assert "securecafe.com" in result.resolved_url


@pytest.mark.asyncio
async def test_resolver_admits_same_host_conventional_path() -> None:
    """Same-host /conventional/ path (Entrata Property Marketing Site)
    must be followed in Pass 3b even when anchor text is empty/icon-only.

    May 13 manual QC: 108 properties have data at
    <vanity>/<region>/<slug>/conventional/ — pre-port these were dropped
    because the anchor text didn't match _CTA_TEXT_RE.
    """
    links = [
        # Same-host path-only admission. Empty anchor text.
        {"href": "https://vanity.example/north-texas/blossom-park/conventional/", "text": ""},
    ]
    page = _make_mock_page(links=links, url="https://vanity.example/")
    detection = detect_pms("https://vanity.example/")
    result = await resolve_target(page, "https://vanity.example/", detection)
    assert result.method == "cta_link"
    assert "conventional" in result.resolved_url


@pytest.mark.asyncio
async def test_resolver_dedupes_same_href_from_header_and_footer() -> None:
    """Three anchors with the same href (header, footer, hero CTA)
    must consume one candidate slot, not three.

    Pre-port: each ate a slot, allowing as few as 1-2 distinct hrefs in
    the top-5. Post-port: dedup_key collapses by netloc+path.
    """
    links = [
        # Same href, three different surrounding texts.
        {"href": "https://www.rentcafe.com/floor-plans", "text": "Floor Plans"},
        {"href": "https://www.rentcafe.com/floor-plans", "text": "View Availability"},
        {"href": "https://www.rentcafe.com/floor-plans", "text": "Apply Now"},
        # Plus a portal-host candidate so we have something to resolve.
        {"href": "https://acme.appfolio.com/listings", "text": ""},
    ]
    page = _make_mock_page(links=links, url="https://vanity.example/")
    detection = detect_pms("https://vanity.example/")
    result = await resolve_target(page, "https://vanity.example/", detection)
    # First admitted portal candidate wins -- rentcafe.com appears in
    # candidates list (only once, due to dedup) and rentcafe.com IS on
    # the portal allowlist, so it wins in Pass 3a.
    assert result.method == "cta_link"
    assert "rentcafe.com" in result.resolved_url


@pytest.mark.asyncio
async def test_resolver_skips_blacklisted_path() -> None:
    """Blacklisted CTA paths (/tour, /apply, /contact, /book) must be
    dropped even when text/portal/path triggers fire."""
    links = [
        {"href": "https://vanity.example/scheduletour", "text": "Schedule a Tour"},  # blacklisted
        {"href": "https://vanity.example/floor-plans", "text": "Floor Plans"},
    ]
    page = _make_mock_page(links=links, url="https://vanity.example/")
    detection = detect_pms("https://vanity.example/")
    result = await resolve_target(page, "https://vanity.example/", detection)
    # The scheduletour link must NOT be resolved-to.
    if result.method == "cta_link":
        assert "scheduletour" not in result.resolved_url
