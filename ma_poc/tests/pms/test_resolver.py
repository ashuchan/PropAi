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
