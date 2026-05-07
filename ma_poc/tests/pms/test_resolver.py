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

    Cap raised from 5 to 8 on 2026-05-06; this preserves the "non-PMS links
    aren't sneakily counted as resolved" intent of the original test.
    """
    # Create 30 CTA links, all with availability text — all to non-PMS hosts.
    links = [{"href": f"https://example{i}.com/", "text": f"View Availability {i}"} for i in range(30)]
    page = _make_mock_page(links=links, url="https://vanity.example/")
    detection = detect_pms("https://vanity.example/")
    result = await resolve_target(page, "https://vanity.example/", detection)
    # None of the example URLs match PMS fingerprints, so should be failed
    assert result.method == "failed"


@pytest.mark.asyncio
async def test_resolver_recognizes_securecafe_subportal() -> None:
    """2026-05-06 fix 1a: *.securecafe.com sublinks must be treated as a portal.

    Previously _LEASING_PORTAL_DOMAINS was checked only for iframes — Step 3
    sublink check used adapter `static_fingerprints()` only, and no adapter
    advertises securecafe.com. So `loftsatopop.securecafe.com/onlineleasing/...`
    was filtered out as non-PMS even though it's clearly the leasing portal.
    """
    links = [
        {
            "href": "https://loftsatopop.securecafe.com/onlineleasing/the-lofts-at-opop0/guestcards",
            "text": "Apply Now",
        },
    ]
    page = _make_mock_page(links=links, url="https://www.loftsatopop.com/")
    detection = detect_pms("https://www.loftsatopop.com/")
    result = await resolve_target(page, "https://www.loftsatopop.com/", detection)
    assert result.method == "cta_link"
    assert "securecafe.com" in result.resolved_url


@pytest.mark.asyncio
async def test_resolver_recognizes_prospectportal_sublink() -> None:
    """2026-05-06 fix 1a: *.prospectportal.com sublinks must be treated as a portal."""
    links = [
        {
            "href": "https://lcnewalbany.prospectportal.com/",
            "text": "View Availability",
        },
    ]
    page = _make_mock_page(links=links, url="https://example.com/")
    detection = detect_pms("https://example.com/")
    result = await resolve_target(page, "https://example.com/", detection)
    assert result.method == "cta_link"
    assert "prospectportal.com" in result.resolved_url


@pytest.mark.asyncio
async def test_resolver_dedupes_duplicate_floor_plan_links() -> None:
    """2026-05-06 fix 1b: identical floor-plan links don't crowd out a real portal.

    Reproduces the hazelwoodhomesmd.com pattern: 5 P=80–100 internal floor-plan
    links + 1 P=30 portal link. Pre-fix, the portal got dropped at cap=5.
    Post-fix, dedupe collapses the duplicates and the cap (8) preserves the
    portal link.
    """
    links = [
        {"href": "https://hazelwoodhomesmd.com/availability/", "text": "Availability"},
        {"href": "https://hazelwoodhomesmd.com/availability/", "text": "Availability"},
        {"href": "https://hazelwoodhomesmd.com/floor-plans/", "text": "Floor Plans"},
        {"href": "https://hazelwoodhomesmd.com/floor-plans/", "text": "Floor Plans"},
        {"href": "https://hazelwoodhomesmd.com/floor-plans/", "text": "View Floor Plans"},
        # Different anchor text but same canonical URL — should still dedupe.
        {"href": "https://hazelwoodhomesmd.com/floor-plans/?utm=x", "text": "View Floor Plans"},
        {
            "href": "https://schwebpartners.appfolio.com/connect/users/sign_in",
            "text": "Resident Portal",
        },
    ]
    page = _make_mock_page(links=links, url="https://hazelwoodhomesmd.com/")
    detection = detect_pms("https://hazelwoodhomesmd.com/")
    result = await resolve_target(page, "https://hazelwoodhomesmd.com/", detection)
    assert result.method == "cta_link"
    assert "appfolio.com" in result.resolved_url


@pytest.mark.asyncio
async def test_resolver_admits_portal_url_with_empty_anchor_text() -> None:
    """2026-05-06 smart link-hop: footer-logo style links to a portal host
    must be admitted even when the anchor text is empty (image-only).

    Reproduces the typical "icon-only RESIDENT PORTAL" footer button.
    """
    links = [
        # Empty text (image-only logo link to AppFolio portal)
        {"href": "https://schwebpartners.appfolio.com/connect/users/sign_in", "text": ""},
    ]
    page = _make_mock_page(links=links, url="https://example.com/")
    detection = detect_pms("https://example.com/")
    result = await resolve_target(page, "https://example.com/", detection)
    assert result.method == "cta_link"
    assert "appfolio.com" in result.resolved_url


@pytest.mark.asyncio
async def test_resolver_admits_yottareal_portal_via_url_only() -> None:
    """Cross-domain external portals (yottareal, ovationco, etc.) match via
    the expanded _LEASING_PORTAL_DOMAINS set even with terse anchor text.
    """
    links = [
        # Terse anchor text that doesn't match _CTA_TEXT_RE
        {"href": "https://adaraportal.yottareal.com/dba/floorplans?dbaid=58", "text": "More"},
    ]
    page = _make_mock_page(links=links, url="https://verandahlake.com/")
    detection = detect_pms("https://verandahlake.com/")
    result = await resolve_target(page, "https://verandahlake.com/", detection)
    assert result.method == "cta_link"
    assert "yottareal.com" in result.resolved_url


@pytest.mark.asyncio
async def test_resolver_essex_style_same_host_subpage() -> None:
    """Essex Apartments case: same-host path match wins when no portal exists.

    Essex's URL scheme is essexapartmenthomes.com/apartments/<region>/<slug>/floor-plans.
    The homepage is a JS-rendered React shell with no extractable units; the
    /floor-plans sub-page IS where the data lives. Pre-fix, the resolver only
    accepted known portals so this same-host path was never followed.
    """
    links = [
        {
            "href": "https://www.essexapartmenthomes.com/apartments/california/walnut-creek/avana-walnut-creek/floor-plans",
            "text": "Avana Walnut Creek",  # property name as anchor — NOT a CTA keyword
        },
    ]
    page = _make_mock_page(links=links, url="https://www.essexapartmenthomes.com/")
    detection = detect_pms("https://www.essexapartmenthomes.com/")
    result = await resolve_target(page, "https://www.essexapartmenthomes.com/", detection)
    assert result.method == "cta_link"
    assert "/floor-plans" in result.resolved_url
    assert "essexapartmenthomes.com" in result.resolved_url


@pytest.mark.asyncio
async def test_resolver_pass_3a_portal_beats_pass_3b_same_host_path() -> None:
    """Even when a same-host path candidate has higher anchor-text priority,
    the cross-domain portal candidate wins because Pass 3a runs first.

    Rationale: a portal URL proves PMS identity and routes to a known
    adapter; a same-host path is only a hint that data lives deeper.
    """
    links = [
        # Same-host path with priority-100 anchor text
        {
            "href": "https://example.com/availability",
            "text": "View Availability",
        },
        # Cross-domain portal with priority-50 anchor text
        {
            "href": "https://9259508.onlineleasing.realpage.com/",
            "text": "Apply Now",
        },
    ]
    page = _make_mock_page(links=links, url="https://example.com/")
    detection = detect_pms("https://example.com/")
    result = await resolve_target(page, "https://example.com/", detection)
    assert result.method == "cta_link"
    assert "onlineleasing.realpage.com" in result.resolved_url


@pytest.mark.asyncio
async def test_resolver_admits_path_match_with_terse_anchor_text() -> None:
    """An anchor with terse text but a /floor-plans path is admitted via
    criterion (c) — URL path matches CTA-path regex.
    """
    links = [
        # Anchor text "More" doesn't match _CTA_TEXT_RE
        {"href": "https://example.com/Floor-plans.aspx", "text": "More →"},
    ]
    page = _make_mock_page(links=links, url="https://example.com/")
    detection = detect_pms("https://example.com/")
    result = await resolve_target(page, "https://example.com/", detection)
    assert result.method == "cta_link"
    assert "Floor-plans.aspx" in result.resolved_url


@pytest.mark.asyncio
async def test_resolver_path_match_does_not_cross_domain() -> None:
    """Cross-domain path-only matches (no portal) are NOT followed.

    Avoids the regression risk of navigating to an unrelated marketing parent
    or third-party lead-gen vendor whose URL happens to contain /listings/.
    """
    links = [
        # Cross-domain, path-only match (NOT a known portal host)
        {
            "href": "https://random-marketing-vendor.com/listings/featured",
            "text": "Browse",
        },
    ]
    page = _make_mock_page(links=links, url="https://example.com/")
    detection = detect_pms("https://example.com/")
    result = await resolve_target(page, "https://example.com/", detection)
    # Cross-domain non-portal match must be rejected — neither pass fires.
    assert result.method == "failed"


@pytest.mark.asyncio
async def test_resolver_word_boundary_priority_skips_intra_word_match() -> None:
    """2026-05-06 fix 1c: `Communities` must not match the `unit` keyword.

    Pre-fix, "OneWall Communities works with eRenterPlan" got priority 60
    because the substring "uni" (the "unit" keyword) appears inside
    "Communities". That elevated a lead-form vendor link above a legitimate
    PMS portal link.
    """
    links = [
        # Intra-word "uni" — must NOT win priority over the PMS link.
        {
            "href": "https://www.erenterplan.com/quote/prop/620866.aspx",
            "text": "OneWall Communities works with eRenterPlan to streamline online tenant applications",
        },
        # "Apply Now" → P=50; the only legitimate portal candidate.
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
