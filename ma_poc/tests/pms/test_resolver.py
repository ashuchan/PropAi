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
async def test_resolver_caps_candidates_at_8() -> None:
    """Only first _CANDIDATE_CAP candidates should be checked.

    Cap raised from 5 → 8 on 2026-05-13; this test preserves the original
    "non-PMS links aren't sneakily counted as resolved" intent.
    """
    # 30 CTA links, all with availability text, all to non-PMS / non-CTA-path hosts.
    links = [
        {"href": f"https://example{i}.com/", "text": f"View Availability {i}"}
        for i in range(30)
    ]
    page = _make_mock_page(links=links, url="https://vanity.example/")
    detection = detect_pms("https://vanity.example/")
    result = await resolve_target(page, "https://vanity.example/", detection)
    # None of the example URLs match a portal host or CTA-path → fail.
    assert result.method == "failed"


# ── 2026-05-13: tests for May 13 manual-QC findings ─────────────────────


@pytest.mark.asyncio
async def test_resolver_follows_conventional_path_same_host() -> None:
    """Entrata Property Marketing Site URL pattern:
    `<vanity>/<region>/<property-slug>/conventional/`.

    108 of 373 manual-tagged failures had this exact pattern.
    """
    links = [
        # Anchor text is just the property name — does NOT match _CTA_TEXT_RE.
        {
            "href": "https://www.hpilegacyatfestival.com/montgomery-montgomery/legacy-at-festival/conventional/",
            "text": "Legacy at Festival",
        },
    ]
    page = _make_mock_page(links=links, url="https://www.hpilegacyatfestival.com/")
    detection = detect_pms("https://www.hpilegacyatfestival.com/")
    result = await resolve_target(page, "https://www.hpilegacyatfestival.com/", detection)
    assert result.method == "cta_link"
    assert "/conventional" in result.resolved_url


@pytest.mark.asyncio
async def test_resolver_follows_models_path() -> None:
    """Greystar /models pattern — 16 of 373 manual-tagged."""
    links = [
        {"href": "https://www.accessrohnertpark.com/models", "text": "View Models"},
    ]
    page = _make_mock_page(links=links, url="https://www.accessrohnertpark.com/")
    detection = detect_pms("https://www.accessrohnertpark.com/")
    result = await resolve_target(page, "https://www.accessrohnertpark.com/", detection)
    assert result.method == "cta_link"
    assert "/models" in result.resolved_url


@pytest.mark.asyncio
async def test_resolver_follows_vacancies_path() -> None:
    """`/vacancies` pattern — 1 of 373 manual-tagged plus others observed."""
    links = [
        # Empty anchor text — purely URL-driven match.
        {"href": "https://www.leescrossingapartments.net/vacancies", "text": ""},
    ]
    page = _make_mock_page(links=links, url="https://www.leescrossingapartments.net/")
    detection = detect_pms("https://www.leescrossingapartments.net/")
    result = await resolve_target(page, "https://www.leescrossingapartments.net/", detection)
    assert result.method == "cta_link"
    assert "/vacancies" in result.resolved_url


@pytest.mark.asyncio
async def test_resolver_follows_floor_plans_and_pricing_path() -> None:
    """`/floor-plans-and-pricing` pattern — 13 of 373 manual-tagged."""
    links = [
        {
            "href": "https://www.example.com/floor-plans-and-pricing/",
            "text": "More Info",
        },
    ]
    page = _make_mock_page(links=links, url="https://www.example.com/")
    detection = detect_pms("https://www.example.com/")
    result = await resolve_target(page, "https://www.example.com/", detection)
    assert result.method == "cta_link"
    assert "/floor-plans-and-pricing" in result.resolved_url


@pytest.mark.asyncio
async def test_resolver_recognizes_securecafe_subportal() -> None:
    """SecureCafe (RentCafe sub-product) — 10 of 373 manual-tagged.

    Pre-fix, securecafe.com wasn't in any adapter's static_fingerprints(),
    so sublinks were silently dropped as non-PMS.
    """
    links = [
        {
            "href": "https://arlingtonwestliving.securecafe.com/onlineleasing/arlington-west/floorplans.aspx",
            "text": "Apply Now",
        },
    ]
    page = _make_mock_page(links=links, url="http://www.arlingtonwestnc.com/")
    detection = detect_pms("http://www.arlingtonwestnc.com/")
    result = await resolve_target(page, "http://www.arlingtonwestnc.com/", detection)
    assert result.method == "cta_link"
    assert "securecafe.com" in result.resolved_url


@pytest.mark.asyncio
async def test_resolver_recognizes_prospectportal_sublink() -> None:
    """ProspectPortal (Entrata sub-product) — 5 of 373 manual-tagged."""
    links = [
        {
            "href": "https://93east.prospectportal.com/",
            "text": "View Availability",
        },
    ]
    page = _make_mock_page(links=links, url="https://example.com/")
    detection = detect_pms("https://example.com/")
    result = await resolve_target(page, "https://example.com/", detection)
    assert result.method == "cta_link"
    assert "prospectportal.com" in result.resolved_url


@pytest.mark.asyncio
async def test_resolver_admits_url_match_with_terse_anchor_text() -> None:
    """Anchor with terse text ("More") but a /conventional/ path is admitted
    via the URL-path trigger.
    """
    links = [
        # Terse text — doesn't match CTA regex
        {"href": "https://example.com/atlanta/foo-bar/conventional/", "text": "→"},
    ]
    page = _make_mock_page(links=links, url="https://example.com/")
    detection = detect_pms("https://example.com/")
    result = await resolve_target(page, "https://example.com/", detection)
    assert result.method == "cta_link"
    assert "/conventional" in result.resolved_url


@pytest.mark.asyncio
async def test_resolver_path_match_does_not_cross_domain() -> None:
    """Cross-domain path-only matches (no portal) are NOT followed.

    Avoids the regression risk of navigating to an unrelated marketing parent
    or third-party lead-gen vendor whose URL happens to contain /listings/.
    """
    links = [
        {
            "href": "https://random-marketing-vendor.com/listings/featured",
            "text": "Browse",
        },
    ]
    page = _make_mock_page(links=links, url="https://example.com/")
    detection = detect_pms("https://example.com/")
    result = await resolve_target(page, "https://example.com/", detection)
    assert result.method == "failed"


@pytest.mark.asyncio
async def test_resolver_word_boundary_priority_skips_intra_word_match() -> None:
    """`Communities` must NOT match the `unit` keyword as a substring.

    Pre-fix, "OneWall Communities works with eRenterPlan" got P=60 because
    "uni" appears inside "Communities" — elevating a lead-form vendor link
    above a legitimate PMS portal link.
    """
    links = [
        {
            "href": "https://www.erenterplan.com/quote/prop/620866.aspx",
            "text": "OneWall Communities works with eRenterPlan to streamline applications",
        },
        # P=50 "Apply Now" → onlineleasing.realpage.com — the real portal.
        {
            "href": "https://9259508.onlineleasing.realpage.com/",
            "text": "Apply Now",
        },
    ]
    page = _make_mock_page(links=links, url="https://parkatblanding.com/")
    detection = detect_pms("https://parkatblanding.com/")
    result = await resolve_target(page, "https://parkatblanding.com/", detection)
    assert result.method == "cta_link"
    assert "onlineleasing.realpage.com" in result.resolved_url


@pytest.mark.asyncio
async def test_resolver_follows_plans_html_legacy_path() -> None:
    """`/plans.html` and `/plans.asp` — legacy CMS variants (3 manual-tagged)."""
    links = [
        {"href": "https://example.com/standrews/plans.html", "text": "Plans"},
    ]
    page = _make_mock_page(links=links, url="https://example.com/")
    detection = detect_pms("https://example.com/")
    result = await resolve_target(page, "https://example.com/", detection)
    assert result.method == "cta_link"
    assert "plans.html" in result.resolved_url


@pytest.mark.asyncio
async def test_resolver_follows_communities_portfolio_path() -> None:
    """`/communities/<slug>/` — portfolio PMC pattern. Princeton Mgmt has unit
    data inline at this path (9 rents visible on probe).
    """
    links = [
        {
            "href": "https://princetonmanagement.com/communities/custer-crossing-apartments/",
            "text": "Custer Crossing",
        },
    ]
    page = _make_mock_page(links=links, url="https://princetonmanagement.com/")
    detection = detect_pms("https://princetonmanagement.com/")
    result = await resolve_target(page, "https://princetonmanagement.com/", detection)
    assert result.method == "cta_link"
    assert "/communities/" in result.resolved_url


@pytest.mark.asyncio
async def test_resolver_follows_townhome_floorplans() -> None:
    """`/townhome-floorplans` — variant of /floor-plans."""
    links = [
        {"href": "https://therowtownhomes.com/townhome-floorplans/", "text": ""},
    ]
    page = _make_mock_page(links=links, url="https://therowtownhomes.com/")
    detection = detect_pms("https://therowtownhomes.com/")
    result = await resolve_target(page, "https://therowtownhomes.com/", detection)
    assert result.method == "cta_link"
    assert "/townhome-floorplans" in result.resolved_url


@pytest.mark.asyncio
async def test_resolver_follows_units_available_variant() -> None:
    """`/units-available` — variant of /availability (autumnaugust pattern)."""
    links = [
        {"href": "https://autumnaugust.com/units-available/", "text": ""},
    ]
    page = _make_mock_page(links=links, url="https://autumnaugust.com/")
    detection = detect_pms("https://autumnaugust.com/")
    result = await resolve_target(page, "https://autumnaugust.com/", detection)
    assert result.method == "cta_link"
    assert "/units-available" in result.resolved_url


@pytest.mark.asyncio
async def test_resolver_follows_interactive_site_map() -> None:
    """`/interactive-site-map` — RentManager sitemap with embedded units."""
    links = [
        {
            "href": "https://www.henryonthepark.com/interactive-site-map/",
            "text": "Site Map",
        },
    ]
    page = _make_mock_page(links=links, url="https://www.henryonthepark.com/")
    detection = detect_pms("https://www.henryonthepark.com/")
    result = await resolve_target(page, "https://www.henryonthepark.com/", detection)
    assert result.method == "cta_link"
    assert "interactive-site-map" in result.resolved_url


@pytest.mark.asyncio
async def test_resolver_follows_property_portfolio_path() -> None:
    """`/property/<slug>/` — portfolio PMC pattern (beacon management)."""
    links = [
        {
            "href": "https://www.beaconmanagement.com/property/the-shores-of-lake-st-clair/",
            "text": "The Shores of Lake St. Clair",
        },
    ]
    page = _make_mock_page(links=links, url="https://www.beaconmanagement.com/")
    detection = detect_pms("https://www.beaconmanagement.com/")
    result = await resolve_target(page, "https://www.beaconmanagement.com/", detection)
    assert result.method == "cta_link"
    assert "/property/" in result.resolved_url


@pytest.mark.asyncio
async def test_resolver_recognizes_yottareal_portal() -> None:
    """`adaraportal.yottareal.com` — Yardi sub-product portal (verandahlake redirect)."""
    links = [
        {
            "href": "https://adaraportal.yottareal.com/dba/floorplans?dbaid=58",
            "text": "Floor Plans",
        },
    ]
    page = _make_mock_page(links=links, url="https://verandahlake.com/")
    detection = detect_pms("https://verandahlake.com/")
    result = await resolve_target(page, "https://verandahlake.com/", detection)
    assert result.method == "cta_link"
    assert "yottareal.com" in result.resolved_url


def test_normalize_appfolio_url_preserves_property_filter() -> None:
    """2026-05-13: multi-property AppFolio tenants encode the property
    selection in `filters[property_list]=...`. Stripping the query loses
    property identity → we extract 100+ wrong units. Filter must survive.

    Reproduces the hayloftpropmgmt bug:
      - WITHOUT filter: /listings returns 292 rents (all 100+ properties)
      - WITH filter:    /listings returns 40 rents (just East Hampton)
    """
    from ma_poc.pms.resolver import normalize_appfolio_url

    input_url = (
        "https://hayloftpropmgmt.appfolio.com/listings"
        "?1778663185787"
        "&filters%5Bproperty_list%5D=EAST%20HAMPTON"
        "&theme_color=%23676767"
        "&filters%5Border_by%5D=date_posted"
    )
    out = normalize_appfolio_url(input_url)
    # Filter directives survive; junk (the timestamp) does not.
    assert "property_list" in out
    assert "EAST" in out  # property name preserved
    assert "theme_color" in out
    assert "1778663185787" not in out  # junk timestamp dropped


def test_normalize_appfolio_url_strips_utm_and_referral_noise() -> None:
    """utm_/gclid/fbclid/source params are stripped from /listings."""
    from ma_poc.pms.resolver import normalize_appfolio_url

    out = normalize_appfolio_url(
        "https://x.appfolio.com/listings?utm_source=foo&utm_campaign=bar&gclid=xx&fbclid=yy"
    )
    assert "utm_" not in out
    assert "gclid" not in out
    assert "fbclid" not in out
    assert out == "https://x.appfolio.com/listings"


def test_normalize_appfolio_url_bare_root_to_listings_keeps_filter() -> None:
    """Bare tenant root (`/`) with a property filter → /listings + filter."""
    from ma_poc.pms.resolver import normalize_appfolio_url

    out = normalize_appfolio_url(
        "https://x.appfolio.com/?filters%5Bproperty_list%5D=FOO%20BAR"
    )
    assert out.startswith("https://x.appfolio.com/listings?")
    assert "property_list" in out
    assert "FOO" in out


def test_normalize_appfolio_url_bare_root_no_filter() -> None:
    """Bare tenant root with no filter → canonical /listings (no query)."""
    from ma_poc.pms.resolver import normalize_appfolio_url

    out = normalize_appfolio_url("https://richelsonmanagement.appfolio.com/")
    assert out == "https://richelsonmanagement.appfolio.com/listings"


def test_normalize_appfolio_url_passes_through_non_appfolio() -> None:
    from ma_poc.pms.resolver import normalize_appfolio_url

    out = normalize_appfolio_url("https://example.com/foo?x=y")
    assert out == "https://example.com/foo?x=y"


@pytest.mark.asyncio
async def test_resolver_recognizes_spherexx_portal() -> None:
    """2026-05-13: Spherexx Presentation Software widget. Added to portal
    allowlist so resolver navigates to the iframe URL. Full adapter is
    future work — see investigations/2026-05-13/spherexx_finding.md.
    """
    links = [
        {
            "href": "https://presentation.spherexx.app/#/ssp/availability",
            "text": "Interactive Site Map",
        },
    ]
    page = _make_mock_page(links=links, url="https://www.henryonthepark.com/")
    detection = detect_pms("https://www.henryonthepark.com/")
    result = await resolve_target(page, "https://www.henryonthepark.com/", detection)
    assert result.method == "cta_link"
    assert "spherexx.app" in result.resolved_url


@pytest.mark.asyncio
async def test_resolver_recognizes_mriprospectconnect_portal() -> None:
    """`*.mriprospectconnect.com` — MRI Software portal."""
    links = [
        {
            "href": "https://smg.mriprospectconnect.com/Search/Index/cca",
            "text": "Search Apartments",
        },
    ]
    page = _make_mock_page(links=links, url="http://www.charterclubapts.com/")
    detection = detect_pms("http://www.charterclubapts.com/")
    result = await resolve_target(page, "http://www.charterclubapts.com/", detection)
    assert result.method == "cta_link"
    assert "mriprospectconnect.com" in result.resolved_url


@pytest.mark.asyncio
async def test_resolver_dedupes_duplicate_floor_plan_links() -> None:
    """Identical /floor-plans/ links (header / footer / button) shouldn't eat
    the cap. Reproduces hazelwoodhomesmd.com pattern: many duplicates of the
    same internal floor-plans link + one portal link.
    """
    links = [
        {"href": "https://example.com/floor-plans/", "text": "Floor Plans"},
        {"href": "https://example.com/floor-plans/", "text": "Floor Plans"},
        {"href": "https://example.com/floor-plans/?utm=a", "text": "View Floor Plans"},
        {"href": "https://example.com/floor-plans", "text": "Floor Plans"},
        {"href": "https://example.com/floor-plans/", "text": "FLOOR PLANS"},
        {
            "href": "https://schwebpartners.appfolio.com/connect/users/sign_in",
            "text": "Resident Portal",
        },
    ]
    page = _make_mock_page(links=links, url="https://example.com/")
    detection = detect_pms("https://example.com/")
    result = await resolve_target(page, "https://example.com/", detection)
    # The AppFolio portal candidate should win (Pass 3a runs before Pass 3b).
    assert result.method == "cta_link"
    assert "appfolio.com" in result.resolved_url


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


# ─────────────────────────────────────────────────────────────────────
# 2026-05-21 grind600 portal additions — goprisma.com + fortresstech.io
# are residents-only Angular/React SPAs (no public availableunits
# endpoint). Adding them to _LEASING_PORTAL_DOMAINS stops the resolver
# from treating their links as candidate floor-plan pages.
# ─────────────────────────────────────────────────────────────────────


def test_url_is_known_portal_recognises_goprisma() -> None:
    """grind600 finding: 13 of 600 sites link only to <prop>.goprisma.com.
    These are residents-only Angular SPAs — the resolver must recognise
    them so it doesn't waste a hop trying to crawl them as marketing
    pages."""
    from ma_poc.pms.resolver import _url_is_known_portal
    for url in (
        "https://aspirethunderbird.goprisma.com/auth/login",
        "https://riverbank.goprisma.com/auth/login",
        "https://reserveatwatertowervillage.goprisma.com/auth/login",
        # Hashed-subdomain variant observed live (emersonparkapthomes.com).
        "https://693c45120e14189f759c2889.goprisma.com/auth/login",
    ):
        assert _url_is_known_portal(url), (
            f"{url!r} must match the goprisma.com leasing-portal domain "
            f"so the resolver skips it as a candidate floor-plan page"
        )


def test_url_is_known_portal_recognises_fortresstech() -> None:
    """grind600 finding: 7 of 600 sites link to
    portal.fortresstech.io/{group-uuid}/{property-uuid}/. The same
    group-uuid recurs across multiple properties = one ingestion endpoint
    per management company. Same residents-only treatment as goprisma."""
    from ma_poc.pms.resolver import _url_is_known_portal
    for url in (
        "https://www.portal.fortresstech.io/26b2b2cc-7df5-4d1f-b437-23fdf9e45d83/08c07271-eb06-4536-909a-7c6afe663068/contact-us",
        "https://www.portal.fortresstech.io/4e8caee8-c99e-406c-864c-c8a5ba3e4a03/f9844047-bb44-4f9e-becc-6dde949ec379",
        "https://www.portal.fortresstech.io/9733ced2-72a8-4978-aa6e-559fc187fd4d/179c6783-3c42-4395-83dd-94c92a4bcad1",
    ):
        assert _url_is_known_portal(url), (
            f"{url!r} must match the fortresstech.io leasing-portal "
            f"domain"
        )


def test_url_is_known_portal_negative_cases_still_negative() -> None:
    """Sanity — non-portal URLs must still return False after these
    additions. Avoid the new strings being too broad."""
    from ma_poc.pms.resolver import _url_is_known_portal
    for url in (
        "https://www.example.com/floorplans",
        "https://aspirethunderbird.com/floorplans",  # marketing CMS, NOT the portal
        "https://www.google.com/",
    ):
        assert not _url_is_known_portal(url), (
            f"{url!r} is not a leasing portal — should remain False"
        )


def test_host_keywords_include_goprisma_fortresstech() -> None:
    """The signal-engine host-keyword list (parallel source of truth)
    must include the same two new portals at the standard 115 score so
    they're treated like other leasing-portal subdomains."""
    from ma_poc.pms.signal_engine.defaults import DEFAULT_HOST_KEYWORDS
    keywords = dict(DEFAULT_HOST_KEYWORDS)
    assert "goprisma.com" in keywords, (
        "goprisma.com must be in DEFAULT_HOST_KEYWORDS "
        "(sync with resolver._LEASING_PORTAL_DOMAINS)"
    )
    assert "fortresstech.io" in keywords
    assert keywords["goprisma.com"] == 115
    assert keywords["fortresstech.io"] == 115


def test_known_plan_level_only_patterns_harbor_group_graduated() -> None:
    """2026-05-26: Harbor Group Management graduated from plan-level-only.

    Unit data lives at ``{prop-url}/{plan}/units`` (one level past the
    ``/listing`` dead-end). Dedicated adapter ``_harbor_group.py`` ships
    as sub-tier 2.55 in generic.py.

    _KNOWN_PLAN_LEVEL_ONLY_PATTERNS must be empty (or not contain the
    HGM pattern) to signal the graduation for future reviewers.
    """
    from ma_poc.pms.resolver import _KNOWN_PLAN_LEVEL_ONLY_PATTERNS
    assert "harborgroupmanagement.com/apartments/" not in _KNOWN_PLAN_LEVEL_ONLY_PATTERNS, (
        "Harbor Group Management has been graduated (dedicated adapter ships "
        "2026-05-26) — remove it from _KNOWN_PLAN_LEVEL_ONLY_PATTERNS"
    )
