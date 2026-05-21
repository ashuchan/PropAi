"""Phase 1 — detector tests. See claude_refactor.md Phase 1."""

from __future__ import annotations

import typing as t

import ma_poc.pms.adapters  # noqa: F401  # register adapters for confirm_detection
from ma_poc.pms.detector import (
    _STRATEGY_BY_PMS,
    MGMT_TO_PMS_PRIOR,
    DetectedPMS,
    _iter_html_markers,
    confirm_detection,
    detect_pms,
    detect_pms_candidates,
)

# Hand-collected from ma_poc/data/runs/2026-04-15/property_reports/. A third
# URL is constructed in the same pattern documented at
# pms/detector.py — the regex only requires the numeric-prefix shape.
REAL_ONESITE_URLS = [
    "https://8756399.onlineleasing.realpage.com/#k=44781",
    "https://9216254.onlineleasing.realpage.com/",
    "https://1234567.onlineleasing.realpage.com/apply",
]


def test_detect_onesite_from_subdomain() -> None:
    result = detect_pms("https://8756399.onlineleasing.realpage.com/#k=44781")
    assert result.pms == "onesite"
    assert result.confidence >= 0.95
    assert result.pms_client_account_id == "8756399"
    assert result.recommended_strategy == "api_first"


def test_detect_rentcafe_from_host() -> None:
    result = detect_pms("https://www.rentcafe.com/apartments/mi/ann-arbor/woodview-commons0/")
    assert result.pms == "rentcafe"
    assert result.confidence >= 0.95


def test_detect_rentcafe_from_aspx_vanity() -> None:
    # Vanity domain with .aspx path — weak heuristic match (0.40).
    # Confidence is intentionally below the 0.70 threshold so that a single
    # HTML marker is required to confirm the RentCafe routing. Without HTML
    # corroboration the .aspx signal alone is too broad (fires on any ASP.NET
    # management portal, not only Yardi/RentCafe sites).
    result = detect_pms("https://fairwaysatfayetteville.apartments/floorplans.aspx")
    assert result.pms == "rentcafe"
    assert 0.35 <= result.confidence <= 0.50  # weak heuristic — corroboration required


def test_detect_entrata_from_mgmt_prior() -> None:
    result = detect_pms(
        "https://sanartesapartmentsscottsdale.com/",
        csv_row={"Management Company": "Mark-Taylor"},
    )
    assert result.pms == "entrata"
    assert result.confidence >= 0.70


def test_detect_avalonbay_from_host() -> None:
    result = detect_pms(
        "https://www.avaloncommunities.com/new-jersey/west-windsor-apartments/avalon-w-squared/"
    )
    assert result.pms == "avalonbay"
    assert result.confidence >= 0.95


def test_detect_sightmap_from_host() -> None:
    result = detect_pms("https://tour.sightmap.com/embed/abc123")
    assert result.pms == "sightmap"
    assert result.confidence >= 0.95


def test_detect_appfolio_from_host() -> None:
    result = detect_pms("https://livecommonplace.appfolio.com/listings/rental_applications/new")
    assert result.pms == "appfolio"
    assert result.confidence >= 0.95
    assert result.pms_client_account_id == "livecommonplace"


def test_detect_realpage_oll_from_non_onesite_host() -> None:
    # Non-OneSite RealPage host — fallback literal.
    result = detect_pms("https://api.ws.realpage.com/some/api")
    assert result.pms == "realpage_oll"
    assert result.confidence >= 0.80
    assert result.recommended_strategy == "portal_hop"


def test_detect_squarespace_nopms_from_html() -> None:
    html = '<html><head><script src="https://static1.squarespace.com/static/x.js"></script></head><body>Hi</body></html>'
    result = detect_pms("https://83freight.com", page_html=html)
    assert result.pms == "squarespace_nopms"
    assert result.recommended_strategy == "syndication_only"


def test_detect_knock_from_doorway_script() -> None:
    """Knock widget loaded via script tag — most common case for the 570
    Knock-fingerprinted T2/3/4 properties (e.g. liveatcalista.com, where
    a 2026-05-13 probe pulled 53 units from the public Doorway API)."""
    html = (
        '<html><head>'
        '<script src="https://doorway.knck.io/latest/doorway.min.js" defer></script>'
        '</head><body>Welcome</body></html>'
    )
    result = detect_pms("https://www.liveatcalista.com/", page_html=html)
    assert result.pms == "knock"
    assert result.confidence >= 0.85
    assert result.recommended_strategy == "api_first"


def test_entrata_application_authentication_path_is_not_a_widget() -> None:
    """Regression for 2026-05-13: many vanity multifamily marketing sites
    (e.g. foxchaseofalexandriaapts.com, livemuseatl.com) link to
    ``/Apartments/module/application_authentication/`` (Entrata's generic
    tenant login form) even when their actual unit data lives on Jonah
    Digital / ProspectPortal / etc. Detector must NOT route to Entrata
    based on this path alone."""
    html = (
        '<html><body>'
        '<a href="/Apartments/module/application_authentication/">'
        'Sign In</a>'
        '</body></html>'
    )
    result = detect_pms("https://www.foxchaseofalexandriaapts.com/", page_html=html)
    # Either unknown (no other signals) or some non-entrata classification.
    # The hard requirement is: NOT entrata.
    assert result.pms != "entrata"


def test_entrata_real_widget_path_still_routes_to_entrata() -> None:
    """Counter-regression: a real Entrata widget embed (e.g. floor_plans,
    availability, lease_application) must still route to Entrata."""
    html = (
        '<html><body>'
        '<iframe src="/Apartments/module/floor_plans/property/12345"></iframe>'
        '</body></html>'
    )
    result = detect_pms("https://www.someproperty.com/", page_html=html)
    assert result.pms == "entrata"


def test_entrata_commoncf_marker_still_routes_to_entrata() -> None:
    """commoncf.entrata.com host marker remains a strong Entrata signal."""
    html = (
        '<html><head>'
        '<script src="https://commoncf.entrata.com/widgets/x.js"></script>'
        '</head></html>'
    )
    result = detect_pms("https://www.someproperty.com/", page_html=html)
    assert result.pms == "entrata"


def test_detect_funnel_from_funnelleasing_script() -> None:
    """2026-05-13: confirmed Funnel uses ``integrations.funnelleasing.com``
    on its newer customer base (e.g. Brook Lane, Arbor Park). Previously
    these were misrouting to RentCafe via Pass 3 weak markers and ended
    up in ``TIER_1_API_RENTCAFE_SHAPE_REJECTED``."""
    html = (
        '<html><body>'
        '<script src="https://integrations.funnelleasing.com/gen-ai-chatbot/v1/'
        'funnel-gen-ai-chat.js"></script>'
        '</body></html>'
    )
    result = detect_pms("https://www.brooklaneapts.com/", page_html=html)
    assert result.pms == "funnel"
    assert result.recommended_strategy == "api_first"


def test_detect_g5_from_dnn506_cloudfront_cdn() -> None:
    """2026-05-13 teammate analysis (C1 sub-class): 73 of 244 generic Tier-1-API
    failures had ``dnn506yrbagrg.cloudfront.net`` as their script source — that's
    G5's primary CDN. Detector must route these to G5."""
    html = (
        '<html><body>'
        '<script src="https://dnn506yrbagrg.cloudfront.net/themes/main.js"></script>'
        '</body></html>'
    )
    result = detect_pms("https://www.bowmanstation.com/", page_html=html)
    assert result.pms == "g5"
    assert result.recommended_strategy == "api_first"


def test_detect_g5_from_g5dxm_themes_host() -> None:
    """2026-05-13 host survey: 173 properties load themes.g5dxm.com /
    widgets.g5dxm.com (G5's theme + widget CDN). These are G5 customers
    whose ``inventory.g5marketingcloud`` calls happen via fetch() (not
    ``<script src>``) so the original detector missed them."""
    html = (
        '<html><body>'
        '<script src="https://themes.g5dxm.com/themes/g5-cs-12345/dist/main.js"></script>'
        '</body></html>'
    )
    result = detect_pms("https://www.morgan-properties.com/apartments/x/y/z/", page_html=html)
    assert result.pms == "g5"
    assert result.recommended_strategy == "api_first"


def test_detect_funnel_from_nestio_contact_widget() -> None:
    """2026-05-13: Funnel acquired Nestio. Properties still loading the
    Nestio contact-widget script are actually on Funnel. Confirmed via
    probe of livebh.com properties — every Nestio reference co-occurs
    with apply.funnelleasing.com / bh.funnelleasing.com URLs."""
    html = (
        '<html><body>'
        '<script src="https://integrations.nestio.com/contact-widget/v1/integration.js"></script>'
        '</body></html>'
    )
    result = detect_pms("https://www.parkatcanyonridge.com/", page_html=html)
    assert result.pms == "funnel"


def test_detect_funnel_from_apply_subdomain() -> None:
    """``apply.funnelleasing.com`` and ``bh.funnelleasing.com`` are
    customer-specific Funnel subdomains observed on livebh.com properties."""
    html = '<a href="https://apply.funnelleasing.com/1170">Apply Now</a>'
    result = detect_pms("https://livebh.com/x/", page_html=html)
    assert result.pms == "funnel"


def test_detect_funnel_beats_rentcafe_when_both_in_html() -> None:
    """Regression guard: a page with BOTH a residual ``rentcafe`` token AND
    the Funnel script must route to Funnel — not fall through to Pass 3's
    weak ``rentcafe`` matcher (the bug that produced 193 SHAPE_REJECTED rows)."""
    html = (
        '<html><body>'
        '<img src="https://cdn-images.rentcafe.com/something.png">'
        '<script src="https://integrations.funnelleasing.com/x.js"></script>'
        '</body></html>'
    )
    result = detect_pms("https://example-funnel-property.com/", page_html=html)
    assert result.pms == "funnel"


def test_detect_knock_from_init_call() -> None:
    """Some properties carry the knockDoorway.init() call directly in static
    HTML even when the doorway.knck.io script is loaded lazily later."""
    html = (
        '<html><body>'
        '<script>window.knockDoorway.init("d05b0a9af1ca11e99ba712428f0e8cf6", '
        '"community", "0344aa08a46c311f");</script>'
        '</body></html>'
    )
    result = detect_pms("https://example-knock-property.com/", page_html=html)
    assert result.pms == "knock"
    assert result.recommended_strategy == "api_first"


def test_detect_wix_nopms_from_html() -> None:
    html = '<html><head><script src="https://static.parastorage.com/bundle.js"></script></head></html>'
    result = detect_pms("https://example-wixsite.com", page_html=html)
    assert result.pms == "wix_nopms"


def test_detect_unknown_returns_cascade_strategy() -> None:
    result = detect_pms("https://totally-unknown-apartment-site.example/")
    assert result.pms == "unknown"
    assert result.confidence == 0.0
    assert result.recommended_strategy == "cascade"


def test_detect_evidence_populated_for_every_result() -> None:
    for url in (
        "https://8756399.onlineleasing.realpage.com/",
        "https://www.rentcafe.com/x",
        "https://totally-unknown.example/",
    ):
        r = detect_pms(url)
        assert isinstance(r.evidence, list)
        assert r.evidence, f"evidence list empty for {url}"


def test_detect_never_raises() -> None:
    # Fuzz with pathological inputs.
    bad_inputs: list[t.Any] = [
        "",
        "not a url",
        "javascript:alert(1)",
        "http://",
        "://no-scheme",
        b"\xff\xfe\xfd",  # type: ignore[list-item]
    ]
    for bi in bad_inputs:
        r = detect_pms(t.cast(str, bi))
        assert isinstance(r, DetectedPMS)
        assert r.pms == "unknown"
        assert r.confidence == 0.0


def test_detect_never_raises_on_none_csv_and_html() -> None:
    r = detect_pms("not-a-url", csv_row=None, page_html=None)
    assert r.pms == "unknown"


def test_mgmt_prior_case_insensitive() -> None:
    upper = detect_pms("https://vanity.example/", csv_row={"Management Company": "MARK-TAYLOR"})
    lower = detect_pms("https://vanity.example/", csv_row={"Management Company": "mark-taylor"})
    title = detect_pms("https://vanity.example/", csv_row={"Management Company": "Mark-Taylor"})
    assert upper.pms == lower.pms == title.pms == "entrata"


def test_client_id_extraction_onesite_matches_3_real_urls() -> None:
    for url in REAL_ONESITE_URLS:
        r = detect_pms(url)
        assert r.pms == "onesite", url
        assert r.pms_client_account_id is not None
        assert r.pms_client_account_id.isdigit()


def test_html_none_path_doesnt_break_detection() -> None:
    # CSV signal must still work when page_html is omitted.
    r = detect_pms(
        "https://sanartesapartmentsscottsdale.com",
        csv_row={"Management Company": "Mark-Taylor"},
        page_html=None,
    )
    assert r.pms == "entrata"


def test_custom_when_mgmt_prior_avalon_but_vanity_host() -> None:
    # AvalonBay mgmt prior -> avalonbay even on a non-matching host.
    r = detect_pms(
        "https://some-vanity-avalon-site.example/",
        csv_row={"Management Company": "AvalonBay Communities"},
    )
    assert r.pms == "avalonbay"


def test_detect_custom_from_csv_override() -> None:
    # CSV pms_platform column with a non-literal value → custom
    r = detect_pms(
        "https://vanity.example/",
        csv_row={"pms_platform": "resman"},
    )
    assert r.pms == "custom"
    assert r.recommended_strategy == "cascade"


def test_csv_override_trusts_known_literal() -> None:
    r = detect_pms("https://vanity.example/", csv_row={"pms_platform": "entrata"})
    assert r.pms == "entrata"
    assert r.confidence >= 0.95


def test_strategy_table_covers_every_literal() -> None:
    # Gate requirement: every PMS literal has a strategy mapping.
    literals = t.get_args(t.get_type_hints(DetectedPMS)["pms"])
    for lit in literals:
        assert lit in _STRATEGY_BY_PMS, lit


def test_mgmt_prior_table_documented() -> None:
    # The prior table is load-bearing; assert at least these entries exist
    # so a casual refactor doesn't silently drop them.
    assert "mark-taylor" in MGMT_TO_PMS_PRIOR
    assert "lindsey management" in MGMT_TO_PMS_PRIOR
    assert "avalonbay communities" in MGMT_TO_PMS_PRIOR


def test_entrata_marker_html() -> None:
    html = '<html><body><div id="entrata-widget-container"></div><a href="/Apartments/module/application_101/">Apply</a></body></html>'
    r = detect_pms("https://vanity.example/", page_html=html)
    assert r.pms == "entrata"
    assert r.confidence >= 0.80


def test_no_rentcafe_false_positive_on_squarespace_with_cdn_asset() -> None:
    # Squarespace giveaway script MUST win over an incidental rentcafe CDN asset.
    # F0.3: rentcafe is a "weak" marker (pass 3) so the Squarespace pass-2
    # check still wins for this CDN-asset case.
    html = (
        "<html><head>"
        '<script src="https://static1.squarespace.com/x.js"></script>'
        '<img src="https://cdngeneralcf.rentcafe.com/foo.jpg">'
        "</head></html>"
    )
    r = detect_pms("https://vanity.example/", page_html=html)
    assert r.pms == "squarespace_nopms"


def test_onesite_strong_marker_beats_squarespace_shell() -> None:
    # F0.3 (Bug 6): A 123taylor.com-style site has a Squarespace marketing
    # shell linking to a real OneSite portal subdomain. Without the
    # 3-pass priority, Squarespace short-circuits at pass 2 and the
    # OneSite portal hop is lost — the property gets routed to
    # syndication_only and yields no units.
    html = (
        "<html><head>"
        '<script src="https://static1.squarespace.com/x.js"></script>'
        "</head><body>"
        '<a href="https://1234567.onlineleasing.realpage.com/">Apply Now</a>'
        "</body></html>"
    )
    r = detect_pms("https://vanity.example/", page_html=html)
    assert r.pms == "onesite"
    assert r.confidence >= 0.80


def test_commoncf_entrata_strong_marker_beats_squarespace_shell() -> None:
    # F0.3 regression test: ``commoncf.entrata.com`` was added to pass 1 as a
    # strong portal signal (it's an Entrata-served CDN host that only appears
    # on real Entrata-backed properties). A Squarespace shell linking to it
    # must route to entrata, not squarespace_nopms.
    html = (
        "<html><head>"
        '<script src="https://static1.squarespace.com/x.js"></script>'
        '<script src="https://commoncf.entrata.com/widget.js"></script>'
        "</head></html>"
    )
    r = detect_pms("https://vanity.example/", page_html=html)
    assert r.pms == "entrata"
    assert r.confidence >= 0.80


def test_entrata_widget_beats_wix_shell() -> None:
    # F0.3: Entrata widget marker is a strong portal signal (pass 1) and
    # must beat a Wix marketing shell. Without F0.3 the property would
    # be misrouted to syndication_only.
    html = (
        "<html><head>"
        '<script src="https://static.parastorage.com/x.js"></script>'
        "</head><body>"
        '<div id="entrata-widget-container"></div>'
        '<a href="/Apartments/module/application_101/">Apply</a>'
        "</body></html>"
    )
    r = detect_pms("https://vanity.example/", page_html=html)
    assert r.pms == "entrata"
    assert r.confidence >= 0.80


def test_funnel_nestio_beats_squarespace_shell() -> None:
    # F0.3: Nestio is a strong PMS marker. Squarespace shell with a
    # nestiolistings.com script src must route to funnel.
    html = (
        "<html><head>"
        '<script src="https://static1.squarespace.com/x.js"></script>'
        '<script src="https://nestiolistings.com/static/bundle.js"></script>'
        "</head></html>"
    )
    r = detect_pms("https://vanity.example/", page_html=html)
    assert r.pms == "funnel"


def test_squarespace_with_appfolio_marketing_link_stays_squarespace() -> None:
    # F0.3: .appfolio.com is a weak marker (could be a marketing link). A
    # Squarespace site with a stray appfolio.com link must NOT misroute
    # to AppFolio — Squarespace at pass 2 wins.
    html = (
        "<html><head>"
        '<script src="https://static1.squarespace.com/x.js"></script>'
        "</head><body>"
        '<a href="https://www.appfolio.com/blog">Powered by AppFolio</a>'
        "</body></html>"
    )
    r = detect_pms("https://vanity.example/", page_html=html)
    assert r.pms == "squarespace_nopms"


# ---------------------------------------------------------------------------
# Change 2 — confirm_detection (router invariant)
# ---------------------------------------------------------------------------


def _rentcafe_initial() -> DetectedPMS:
    return DetectedPMS(
        pms="rentcafe",
        confidence=0.95,
        evidence=["host ends in rentcafe.com (test.rentcafe.com)"],
        recommended_strategy="jsonld_first",
    )


def _rentcafe_body() -> dict[str, object]:
    # Minimal body shape _is_rentcafe_response accepts (3+ known keys).
    return {
        "data": [
            {
                "floorplanName": "A1",
                "floorplanId": "1",
                "minimumRent": "1500",
                "maximumRent": "1600",
            }
        ]
    }


def _funnel_body() -> dict[str, object]:
    return {"results": [{"listingId": "abc", "marketRent": 1850, "unit": "101"}]}


def test_confirm_detection_keeps_when_body_matches() -> None:
    initial = _rentcafe_initial()
    responses = [{"url": "https://x/api", "body": _rentcafe_body()}]
    result = confirm_detection(initial, responses)
    assert result.pms == "rentcafe"
    assert result.confidence == initial.confidence


def test_confirm_detection_demotes_when_no_body_matches() -> None:
    initial = _rentcafe_initial()
    responses = [{"url": "https://nestiolistings.com/x", "body": _funnel_body()}]
    result = confirm_detection(initial, responses)
    assert result.pms == "unknown"
    assert any("demoted_from_rentcafe" in e for e in result.evidence)
    assert result.recommended_strategy == "cascade"


def test_confirm_detection_preserves_when_no_responses() -> None:
    # F0.2: 0 captured responses ≠ wrong URL detection. When the page
    # was served via httpx GET (change-detector path), blocked, or
    # timed out, we have no evidence to disconfirm the URL-based
    # detection. Demoting here would force the generic cascade and
    # starve real PMS portals of their proper adapter. See the
    # 2026-05-09 cloud-run regression analysis for the recovery cohort.
    initial = _rentcafe_initial()
    result = confirm_detection(initial, [])
    assert result.pms == "rentcafe"
    assert result.confidence == initial.confidence
    assert not any("demoted_from_rentcafe" in e for e in result.evidence)


def test_confirm_detection_preserves_when_responses_is_none() -> None:
    # The scraper passes ``getattr(ctx, "_api_responses", []) or []`` so
    # ``None`` shouldn't reach this function in production, but the
    # signature accepts it. None must behave identically to empty list.
    initial = _rentcafe_initial()
    result = confirm_detection(initial, None)
    assert result.pms == "rentcafe"
    assert result.confidence == initial.confidence


def test_confirm_detection_leaves_unknown_alone() -> None:
    initial = DetectedPMS(
        pms="unknown",
        confidence=0.0,
        evidence=["no signal"],
        recommended_strategy="cascade",
    )
    result = confirm_detection(initial, [{"url": "x", "body": _rentcafe_body()}])
    assert result is initial or result.pms == "unknown"


def test_detect_funnel_from_mgmt_windsor_communities() -> None:
    r = detect_pms(
        "https://windsorcommunities.com/properties/windsor-sugarloaf/",
        csv_row={"Management Company": "Windsor Communities"},
    )
    assert r.pms == "funnel"
    assert r.confidence >= 0.70


def test_detect_funnel_from_html_nestio_script() -> None:
    html = '<html><head><script src="https://nestiolistings.com/static/bundle.js"></script></head></html>'
    r = detect_pms("https://windsorcommunities.com/x/", page_html=html)
    assert r.pms == "funnel"
    assert r.confidence >= 0.85


def test_detect_rentcafe_no_longer_matches_windsor() -> None:
    # Windsor CSV + vanity host must NOT land on rentcafe — Change 3 priors
    # point "windsor communities" at funnel instead.
    r = detect_pms(
        "https://windsorcommunities.com/properties/x/",
        csv_row={"Management Company": "Windsor Communities"},
    )
    assert r.pms != "rentcafe"


def test_detect_funnel_from_nestio_host() -> None:
    r = detect_pms("https://nestiolistings.com/api/v2/listings/residential/rentals/?key=x")
    assert r.pms == "funnel"
    assert r.confidence >= 0.95


def test_confirm_detection_handles_adapter_without_body_check() -> None:
    # The Entrata adapter (pre-Change 2) has no matches_response_body method;
    # confirm_detection must leave the URL-based detection alone.
    initial = DetectedPMS(
        pms="entrata",
        confidence=0.95,
        evidence=["entrata-host"],
        recommended_strategy="api_first",
    )
    result = confirm_detection(initial, [{"url": "x", "body": {"noise": 1}}])
    assert result.pms == "entrata"


# ── Bug C (2026-05-11) — P3 "innocent until proven guilty" demotion rule ────


def test_confirm_detection_preserves_when_bodies_are_pure_noise() -> None:
    """The 2026-05-11 production failure shape: RentCafe URL detection
    + every captured XHR is a third-party widget (analytics, CDN,
    captcha). None match RentCafe's body shape, but none match a
    *different* PMS either — they're just noise. P3 says noise is
    absence-of-evidence, not evidence-of-absence: preserve.

    Before the fix this demoted, losing 558 RentCafe properties/day.
    After the fix it preserves because no cross-match is found in
    Phase 2.
    """
    initial = _rentcafe_initial()
    noise_responses = [
        {"url": "https://www.googletagmanager.com/gtm.js", "body": "ga()"},
        {"url": "https://maps.googleapis.com/maps/api/place", "body": '{"results":[]}'},
        {"url": "https://challenges.cloudflare.com/turnstile", "body": "<html/>"},
        {"url": "https://hotjar.com/c/heatmap.json", "body": '{"hm":1}'},
        {"url": "https://cmp.osano.com/16A0DbT9yDNIaQkvZ/widget", "body": '{"consent":true}'},
    ]
    result = confirm_detection(initial, noise_responses)
    assert result.pms == "rentcafe", (
        f"P3 violation: confirm_detection demoted on pure noise. "
        f"Got {result.pms!r}, evidence={result.evidence}"
    )
    # No demotion → evidence list unchanged from initial.
    assert not any("demoted_from" in e for e in result.evidence)


def test_confirm_detection_demotes_with_specific_cross_match_in_evidence() -> None:
    """Phase 2 finds a captured body that positively matches a different
    adapter (Funnel). That's positive evidence the URL detection was
    wrong — demote, and surface *which* adapter the body actually
    belonged to in the evidence string so operators can triage.

    Documents the new evidence format introduced by the Bug C fix:
    ``demoted_from_<src>:response_<idx>_matches_<dst>_body_shape``.
    """
    initial = _rentcafe_initial()
    responses = [
        {"url": "https://noise.example.com/widget", "body": '{"unrelated": 1}'},
        {"url": "https://nestiolistings.com/api/listings", "body": _funnel_body()},
    ]
    result = confirm_detection(initial, responses)
    assert result.pms == "unknown"
    assert any("demoted_from_rentcafe" in e for e in result.evidence)
    # The specific cross-match must be named — operators look for this
    # when triaging "why did this property route to generic?"
    assert any(
        "funnel" in e.lower() for e in result.evidence
    ), (
        f"demotion evidence must name the matched adapter (funnel); "
        f"got: {result.evidence}"
    )


def test_confirm_detection_preserves_when_only_initial_adapter_has_checker(
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    """Edge case: the registry has zero *other* adapters with
    ``matches_response_body``. Phase 2 finds no checkers to cross-test
    against, so no negative evidence is possible — preserve. Documents
    the safe-default behaviour: when in doubt, trust the URL detection.

    Implemented via a monkeypatched ``all_adapters`` that returns only
    the initial adapter; avoids touching the real registry.
    """
    from ma_poc.pms.adapters import registry
    from ma_poc.pms.adapters.registry import get_adapter

    rentcafe_adapter = get_adapter("rentcafe")
    monkeypatch.setattr(registry, "all_adapters", lambda: [rentcafe_adapter])

    initial = _rentcafe_initial()
    # Body that doesn't match RentCafe — and there's no other adapter
    # in the registry to cross-check against in Phase 2.
    responses = [{"url": "https://x/api", "body": '{"completely": "alien"}'}]
    result = confirm_detection(initial, responses)
    assert result.pms == "rentcafe", (
        f"With only the initial adapter in the registry, Phase 2 has no "
        f"alternative to cross-match against. P3 says preserve when no "
        f"negative evidence is found — got {result.pms!r}"
    )


# ────────────────────────────────────────────────────────────────────────────
# Ported from main commit 78516c3 ("Added fixes for jsonld and few pms"):
# SightMap-embed iframe is a STRONG marker that should beat an Entrata-widget
# marker on the same page. Two real-world cases motivate this:
#   - chaseknollsapts.com (PID 16139, 2026-05-14): Entrata STRONG won pre-fix,
#     SightMap iframe with 9 units never got read.
#   - Morgan Properties cluster (28 props observed 2026-05-19): canary fell
#     to TIER_3_DOM on every one, prod caught SightMap.
# ────────────────────────────────────────────────────────────────────────────


def test_detect_sightmap_embed_iframe_routes_to_sightmap_strong() -> None:
    """A page with ``sightmap.com/embed/<code>`` in an iframe is SightMap.

    The bare ``sightmap.com`` substring was already detected at a weaker
    pass-3 confidence (0.80) — but the embed-iframe form is iframe-specific
    and unambiguous, so it deserves STRONG (0.90).
    """
    html = (
        "<html><body>"
        '<iframe src="https://sightmap.com/embed/abc123def" '
        'width="100%" height="700"></iframe>'
        "</body></html>"
    )
    result = detect_pms("https://www.morgan-properties.com/apartments/x/", page_html=html)
    assert result.pms == "sightmap"
    assert result.confidence >= 0.90


def test_detect_sightmap_embed_beats_entrata_widget_when_both_present() -> None:
    """Real chaseknollsapts.com shape — Entrata widget + SightMap embed.

    The Entrata widget on the same page is typically a contact/amenities/
    photo module that doesn't carry units; the unit data lives in the
    SightMap iframe. Detector must route to SightMap or its units will
    never be read.
    """
    html = (
        "<html><body>"
        '<div class="entrata-widget"></div>'
        '<script src="https://commoncf.entrata.com/widgets/x.js"></script>'
        '<iframe src="https://sightmap.com/embed/xy7890ab" '
        'width="100%" height="700"></iframe>'
        "</body></html>"
    )
    result = detect_pms(
        "https://chaseknollsapts.com/", page_html=html
    )
    assert result.pms == "sightmap"
    assert result.confidence >= 0.90


def test_entrata_widget_without_sightmap_embed_still_routes_to_entrata() -> None:
    """The Entrata-widget path is unchanged when no SightMap embed exists.

    Guards against the regression where the new SightMap-embed branch
    accidentally swallows all Entrata-widget pages.
    """
    html = (
        "<html><body>"
        '<script src="https://commoncf.entrata.com/widgets/x.js"></script>'
        "</body></html>"
    )
    result = detect_pms("https://example.com/", page_html=html)
    assert result.pms == "entrata"
    assert result.confidence >= 0.85


def test_bare_sightmap_com_without_embed_path_is_NOT_strong() -> None:
    """Bare ``sightmap.com`` substring (no ``/embed/``) must not promote.

    Could be a CDN asset, analytics referrer, or favicon link. Only the
    iframe-specific ``sightmap.com/embed/`` is a definitive routing signal.
    Stays in the weaker pass-3 fingerprint path.
    """
    html = (
        "<html><head>"
        '<link rel="preconnect" href="https://sightmap.com">'
        "</head><body><p>marketing copy</p></body></html>"
    )
    result = detect_pms("https://example.com/", page_html=html)
    # Either routed to sightmap at the weaker 0.80 pass-3, OR not routed
    # at all — the hard requirement is: confidence cannot be ≥ 0.90, and
    # we must NOT see the strong-routing evidence string.
    if result.pms == "sightmap":
        assert result.confidence < 0.90
    assert not any(
        "embed iframe in HTML" in e for e in result.evidence
    )


# ─────────────────────────────────────────────────────────────────────
# 2026-05-20 deep-probe finding (feature_fail_1429 cluster #2 of 7):
# The Jonah-Digital marker (jonahwidget/meetelise/jonahdigital) is
# sometimes present on RentCafe-SecureCafe / SightMap / etc. pages as
# a chat-widget bolt-on, not as the primary PMS. When both signals
# co-exist, the more specific PMS (RentCafe/SightMap) must win — the
# encoreskyline_template adapter has no way to extract from those
# sites and bails with NOT_ENCORESKYLINE_TEMPLATE, leaving the property
# with 0 units. Live-verified on ardencebloom.com (pid 238181, main
# extracts 182 strict via TIER_1_API_RENTCAFE).
# ─────────────────────────────────────────────────────────────────────


def test_securecafe_beats_jonah_widget_when_both_present() -> None:
    """ardencebloom.com pattern — meetelise (Jonah chat widget) +
    securecafe.com/onlineleasing (real PMS). Detector must route to
    rentcafe, not encoreskyline_template."""
    html = (
        "<html><body>"
        '<script>JonahWidget.meetelise({org:"x"});</script>'
        '<a href="https://propmgr.securecafe.com/onlineleasing/ardence/'
        'availableunits.aspx">Apply Now</a>'
        "</body></html>"
    )
    result = detect_pms("https://www.ardencebloom.com/", page_html=html)
    assert result.pms == "rentcafe", (
        f"expected rentcafe (page has securecafe portal AND chat widget), "
        f"got {result.pms!r}"
    )


def test_sightmap_embed_beats_jonah_widget_when_both_present() -> None:
    """Same principle for sightmap.com/embed iframe + Jonah widget on the
    same page. SightMap is the real data; Jonah is decoration."""
    html = (
        "<html><body>"
        '<script src="//meetelise.com/widget.js"></script>'
        '<iframe src="https://sightmap.com/embed/abc123def"></iframe>'
        "</body></html>"
    )
    result = detect_pms("https://example.com/", page_html=html)
    assert result.pms == "sightmap", (
        f"expected sightmap (real PMS), got {result.pms!r}"
    )


def test_jonah_alone_still_routes_to_encoreskyline_template() -> None:
    """Regression guard — when ONLY the Jonah marker is present and no
    other PMS competes, encoreskyline_template still wins."""
    html = (
        "<html><body>"
        '<script>JonahWidget.meetelise({organization:"X",building:"Y"});</script>'
        '<a href="/floorplans/spruce/">Spruce</a>'
        "</body></html>"
    )
    result = detect_pms("https://example.com/", page_html=html)
    assert result.pms == "encoreskyline_template"
    assert result.confidence >= 0.85


def test_jonah_widget_does_not_block_entrata_widget() -> None:
    """If a page has Jonah marker AND Entrata widget markers, Entrata
    must win — the encoreskyline_template adapter can't extract from
    an Entrata-backed site."""
    html = (
        "<html><body>"
        '<script>JonahWidget.meetelise({});</script>'
        '<script src="https://commoncf.entrata.com/widgets/x.js"></script>'
        "</body></html>"
    )
    result = detect_pms("https://example.com/", page_html=html)
    assert result.pms == "entrata"


def test_jonah_widget_does_not_block_resman_portal() -> None:
    """If a page has Jonah marker AND a myresman.com portal anchor,
    ResMan must win."""
    html = (
        "<html><body>"
        '<script src="//meetelise.com/x.js"></script>'
        '<iframe src="https://acmepm.myresman.com/Portal/Applicants/'
        'Availability?a=1&p=abc-def">'
        "</iframe></body></html>"
    )
    result = detect_pms("https://example.com/", page_html=html)
    # bare ``myresman`` isn't in current detector but the test documents
    # the intent; lower-confidence resman OR not-encoreskyline is the bar
    assert result.pms != "encoreskyline_template", (
        f"meetelise should not block a ResMan-portal-bearing page; "
        f"got {result.pms!r}"
    )


# ─────────────────────────────────────────────────────────────────────
# 2026-05-20 feature_fail_1429 grind — cluster #3 (G5 + competing PMS).
# 90+ properties tagged ``TIER_1_API_G5_EMPTY`` / ``_NO_URN`` in the
# canary feature run had G5 markers PRESENT (g5marketingcloud / g5dxm /
# g5-c-) but the unit data lives in a co-resident RentCafe (SecureCafe)
# or ResMan portal — main reached TIER_1_API_RENTCAFE / TIER_1_API on
# these sites for 41+ properties. G5 won the detector race (line 567 +
# 690 both before the securecafe/myresman branches) and then bailed
# because the page-level URN was company-level (e.g.
# ``g5-cl-...-lincoln-property-company-...``), not property-level.
# Live-verified: pid 13477 flatirondistrictataustinranch.com — G5
# markers + hasRentCafe=true + 5 LPC company URNs; pid 6274 fmgnj.com
# was a 404 (honest empty, no fix).
#
# Pattern is the same shape as the cluster-2 Jonah-gate: detector
# routes to a co-resident PMS marker whose adapter actually extracts.
# ─────────────────────────────────────────────────────────────────────


def test_securecafe_beats_g5_marker_when_both_present() -> None:
    """flatirondistrictataustinranch.com pattern — G5 marketing-cloud
    markers AND a securecafe online-leasing portal on the same page.
    Detector must route to rentcafe, not g5. G5 adapter would extract a
    company-level URN and return empty; rentcafe adapter hits the
    securecafe ``availableunits.aspx`` path and gets real units."""
    html = (
        "<html><body>"
        '<script src="https://themes.g5dxm.com/themes/g5-cs-12345/main.js"></script>'
        '<a href="https://lpcprop.securecafe.com/onlineleasing/flatiron/'
        'availableunits.aspx">Apply</a>'
        "</body></html>"
    )
    result = detect_pms(
        "https://www.flatirondistrictataustinranch.com/",
        page_html=html,
    )
    assert result.pms == "rentcafe", (
        f"expected rentcafe (securecafe portal beats G5 page-marker), "
        f"got {result.pms!r}"
    )


def test_resman_portal_beats_g5_marker_when_both_present() -> None:
    """G5 markers + myresman.com portal anchor — resman wins. ResMan
    adapter recovers Tier-1 unit-level; G5 would bail empty."""
    html = (
        "<html><body>"
        '<script src="https://dnn506yrbagrg.cloudfront.net/themes/x.js"></script>'
        '<iframe src="https://acme.myresman.com/Portal/Applicants/'
        'Availability?a=1&p=abc"></iframe>'
        "</body></html>"
    )
    result = detect_pms("https://example.com/", page_html=html)
    assert result.pms == "resman", (
        f"expected resman (portal anchor beats G5 CDN marker), "
        f"got {result.pms!r}"
    )


def test_g5_alone_still_routes_to_g5_weak_marker() -> None:
    """Regression guard — pass-2 G5 markers (g5marketingcloud /
    g5dxm.com / g5-c-) WITHOUT any competing PMS marker still route
    to g5. Verifies the gate doesn't over-broaden."""
    html = (
        "<html><body>"
        '<script src="https://themes.g5dxm.com/themes/g5-c-acme/main.js"></script>'
        "</body></html>"
    )
    result = detect_pms("https://example.com/", page_html=html)
    assert result.pms == "g5"
    assert result.confidence >= 0.85


def test_g5_alone_still_routes_to_g5_strong_marker() -> None:
    """Regression guard for the pass-3 G5 branch (line 690) — the
    stronger markers (inventory.g5marketingcloud / g5-cl- /
    dnn506yrbagrg.cloudfront.net) WITHOUT any competing PMS marker
    must still route to g5."""
    html = (
        "<html><body>"
        '<script src="https://dnn506yrbagrg.cloudfront.net/themes/x.js"></script>'
        '<meta name="urn" content="g5-cl-abc123-acme-tx">'
        "</body></html>"
    )
    result = detect_pms("https://example.com/", page_html=html)
    assert result.pms == "g5"
    assert result.confidence >= 0.85


def test_knock_doorway_beats_g5_marker_when_both_present() -> None:
    """Dominant cluster-3 pattern (2026-05-20 live probe of 6 Bucket-A
    worklist props — altaaptstarga, avonleatributary, beechmeadowaptsin,
    unionthompson, 6thandalderapartments, liveatone55lofts): G5 markers
    co-resident with the Knock/Doorway widget. Without the gate the
    pass-2 G5 weak marker at line 567 wins and the page-level URN is
    company-level → empty. Detector must route to knock so the
    KnockAdapter (api_first) hits the Doorway public API."""
    html = (
        "<html><body>"
        '<script src="https://themes.g5dxm.com/themes/g5-c-acme/main.js"></script>'
        '<script src="https://doorway.knck.io/latest/doorway.min.js"></script>'
        '<script>knockDoorway.init("a8e311e98aee0ee4545fea9e01b06ac6",'
        '"community","69e936e6567a11ef");</script>'
        "</body></html>"
    )
    result = detect_pms("https://example.com/", page_html=html)
    assert result.pms == "knock", (
        f"expected knock (Doorway widget beats G5 page-marker), "
        f"got {result.pms!r}"
    )


# ─────────────────────────────────────────────────────────────────────
# Path B Piece 2 (2026-05-20) — _iter_html_markers + detect_pms_candidates.
#
# The HTML-marker scan is now a generator yielding ALL matching PMSs in
# detector priority order. The orchestrator uses detect_pms_candidates
# to find the NEXT PMS to try when the first adapter returns an empty
# exit. These tests pin the contract: candidates[0] == detect_pms()'s
# winner (back-compat); exclude={prev_pms} surfaces the runner-up.
# ─────────────────────────────────────────────────────────────────────


def test_iter_html_markers_yields_first_match_equal_to_detect_html_markers() -> None:
    """The legacy first-match contract is preserved. Picking the first
    yield must equal what the old single-result function returned."""
    from ma_poc.pms.detector import _detect_html_markers
    samples = [
        '<script src="https://commoncf.entrata.com/widgets/x.js"></script>',
        '<script>knockDoorway.init("a"*32,"community","abc123def");</script>',
        '<iframe src="https://sightmap.com/embed/m9pzd4ezvk1"></iframe>',
        '<a href="https://propmgr.securecafe.com/onlineleasing/x/availableunits.aspx">x</a>',
        '<script src="https://themes.g5dxm.com/themes/g5-c-acme/main.js"></script>',
    ]
    for html in samples:
        first_yielded = next(_iter_html_markers(html), None)
        legacy = _detect_html_markers(html)
        assert first_yielded == legacy, (
            f"first yield differs from legacy first-match for html={html[:60]!r}: "
            f"yield={first_yielded!r} legacy={legacy!r}"
        )


def test_iter_html_markers_yields_multiple_on_co_resident_pms() -> None:
    """Page with G5 + Knock + SecureCafe markers — generator yields all
    three that pass the existing detector gates. (G5 is gated out by
    cluster #3 fix when securecafe/knock is present; Knock and RentCafe
    both yield.)"""
    html = (
        "<html><body>"
        '<script src="https://themes.g5dxm.com/themes/g5-c-acme/main.js"></script>'
        '<script src="https://doorway.knck.io/latest/doorway.min.js"></script>'
        '<script>knockDoorway.init("a8e311e98aee0ee4545fea9e01b06ac6",'
        '"community","69e936e6567a11ef");</script>'
        '<a href="https://lpc.securecafe.com/onlineleasing/x/availableunits.aspx">x</a>'
        "</body></html>"
    )
    yielded = [m[0] for m in _iter_html_markers(html)]
    # First yield is the highest-priority survivor of the gates.
    assert yielded[0] == "knock", f"first should be knock, got {yielded[0]!r}"
    # RentCafe (via securecafe) is yielded later as a retry candidate.
    assert "rentcafe" in yielded, (
        f"rentcafe (via securecafe) should appear as a later candidate; got {yielded!r}"
    )


def test_detect_pms_candidates_first_equals_detect_pms() -> None:
    """Back-compat: candidates[0] must equal detect_pms() result so any
    existing call site can be swapped to take ``candidates[0]`` without
    behavior change."""
    samples = [
        # (url, html or None, csv_row or None)
        ("https://www.example.com/", '<iframe src="https://sightmap.com/embed/abc123xyz"></iframe>', None),
        ("https://lakeline.example.com/", '<script src="//doorway.knck.io/latest/doorway.min.js"></script>', None),
        ("https://example.com/", '<script src="https://commoncf.entrata.com/widgets/x.js"></script>', None),
        ("https://example.com/", None, None),  # no signals → no candidates
    ]
    for url, html, csv_row in samples:
        main = detect_pms(url, csv_row=csv_row, page_html=html)
        cands = detect_pms_candidates(url, csv_row=csv_row, page_html=html)
        if main.pms == "unknown":
            # detect_pms returns "unknown" when no signal; candidates is empty.
            assert cands == [], f"expected [] for no-signal input, got {cands!r}"
        else:
            assert cands, f"expected at least 1 candidate for {url!r}, got []"
            assert cands[0].pms == main.pms, (
                f"candidates[0] {cands[0].pms!r} differs from detect_pms {main.pms!r} "
                f"for url={url!r}"
            )


def test_detect_pms_candidates_exclude_removes_first_winner() -> None:
    """Calling with exclude={first_winner} returns the runner-up — the
    primary Path B retry use case."""
    html = (
        "<html><body>"
        '<script src="https://themes.g5dxm.com/themes/g5-c-acme/main.js"></script>'
        '<script>knockDoorway.init("a8e311e98aee0ee4545fea9e01b06ac6",'
        '"community","69e936e6567a11ef");</script>'
        '<a href="https://lpc.securecafe.com/onlineleasing/x/availableunits.aspx">x</a>'
        "</body></html>"
    )
    full = detect_pms_candidates("https://example.com/", page_html=html)
    assert len(full) >= 2, (
        f"expected ≥2 candidates on multi-PMS page, got {len(full)}: {[c.pms for c in full]}"
    )
    winner = full[0].pms
    runner_up = full[1].pms
    second_call = detect_pms_candidates(
        "https://example.com/", page_html=html, exclude={winner}
    )
    assert second_call, "expected non-empty list after excluding the winner"
    assert second_call[0].pms == runner_up, (
        f"excluding {winner!r} should surface {runner_up!r}; got {second_call[0].pms!r}"
    )
    assert all(c.pms != winner for c in second_call), (
        f"excluded PMS appeared in candidates: {[c.pms for c in second_call]}"
    )


def test_detect_pms_candidates_caps_at_max_candidates() -> None:
    """max_candidates default is 4; larger pools must be truncated."""
    # Construct a page with many distinct PMS markers (more than 4).
    html = (
        "<html><body>"
        '<iframe src="https://sightmap.com/embed/abc12345"></iframe>'
        '<script src="https://doorway.knck.io/latest/doorway.min.js"></script>'
        '<script>knockDoorway.init("a8e311e98aee0ee4545fea9e01b06ac6",'
        '"community","69e936e6567a11ef");</script>'
        '<a href="https://lpc.securecafe.com/onlineleasing/x/availableunits.aspx">x</a>'
        '<script src="https://commoncf.entrata.com/x.js"></script>'
        '<a href="https://x.appfolio.com/listings">listings</a>'
        "</body></html>"
    )
    capped = detect_pms_candidates(
        "https://example.com/", page_html=html, max_candidates=2
    )
    assert len(capped) <= 2, f"expected ≤2, got {len(capped)}"
    expanded = detect_pms_candidates(
        "https://example.com/", page_html=html, max_candidates=10
    )
    assert len(expanded) >= len(capped)


def test_detect_pms_candidates_excludes_unknown() -> None:
    """``unknown`` is never offered as a retry candidate — it has no
    adapter to dispatch to."""
    cands = detect_pms_candidates("https://no-signal.example.com/")
    for c in cands:
        assert c.pms != "unknown", (
            f"`unknown` must never appear in candidates list, got {[x.pms for x in cands]}"
        )


def test_detect_pms_candidates_no_signal_returns_empty() -> None:
    """Bare URL, no HTML, no CSV → no candidates (not [DetectedPMS('unknown')])."""
    assert detect_pms_candidates("https://nowhere.example.com/") == []


def test_detect_pms_candidates_never_raises_on_bad_input() -> None:
    """Defensive: every detector helper is wrapped; bad inputs return []
    or partial list, never raise."""
    assert detect_pms_candidates("") == []
    # Garbled HTML / wrong types — must not raise.
    out = detect_pms_candidates("https://x.com/", page_html=b"not str" * 100)  # type: ignore[arg-type]
    assert isinstance(out, list)


def test_detect_pms_candidates_host_fingerprint_wins_first_slot() -> None:
    """Host-based fingerprint (e.g. ``*.rentcafe.com``) is high-trust
    and should take the first slot even when the HTML carries other
    markers."""
    html = (
        "<html><body>"
        '<iframe src="https://sightmap.com/embed/abc12345"></iframe>'
        "</body></html>"
    )
    cands = detect_pms_candidates(
        "https://prop.rentcafe.com/apartments/", page_html=html
    )
    assert cands, "expected candidates"
    assert cands[0].pms == "rentcafe", (
        f"host fingerprint should win first slot, got {cands[0].pms!r}"
    )
    # SightMap from the HTML still appears later — orchestrator can
    # retry to it if rentcafe returns empty.
    assert any(c.pms == "sightmap" for c in cands), (
        f"sightmap should still appear as a later candidate; got {[c.pms for c in cands]}"
    )



# ─────────────────────────────────────────────────────────────────────
# 2026-05-20 Engrain widget signal — TIER_3_DOM ALL_fail recovery
# (see project_tier3_dom_recovery_2026-05-20.md):
# Properties on RealPage's Engrain interactive map (SightMap) load the
# iframe dynamically post-JS, so static HTML lacks ``sightmap.com/embed/``.
# But the server-rendered HTML carries paired ``data-unit``/``data-floorplan``
# attributes (Engrain hydration placeholders) AND a ``realpage.com``
# script load. Detector must route to sightmap on this combined signal
# so SightMap's iframe-fallback discovery (cluster #5 broadening) can
# fire. Verified live against 7 of 25 TIER_3_DOM ALL_fail props:
# Sawmill Station, Headwaters Autumn Hall, Stadia Med Main, Delwyn,
# Broadstone SoBro, Millennium River Oaks, Soleste Seaside.
# ─────────────────────────────────────────────────────────────────────


def test_engrain_widget_routes_to_sightmap() -> None:
    """Paired ``data-unit`` + ``data-floorplan`` attrs + ``realpage.com``
    script → route to sightmap (Engrain hydration signal)."""
    html = (
        "<html><body>"
        '<script src="https://www.realpage.com/widget/engrain.js"></script>'
        '<div data-floorplan="3x2" data-unit="A101">Plan A1</div>'
        "</body></html>"
    )
    result = detect_pms(
        "https://www.residencesatsawmillstation.com/", page_html=html
    )
    assert result.pms == "sightmap"
    assert result.confidence >= 0.85
    assert any("Engrain widget" in e for e in result.evidence)


def test_engrain_widget_missing_realpage_does_not_fire() -> None:
    """``data-unit`` + ``data-floorplan`` WITHOUT ``realpage.com`` is too
    generic — many marketing-template CMSes use these attribute names.
    Must NOT promote sightmap without the RealPage script load."""
    html = (
        "<html><body>"
        '<div data-floorplan="3x2" data-unit="A101">Plan A1</div>'
        "</body></html>"
    )
    result = detect_pms("https://example.com/", page_html=html)
    # Should be unknown / weaker fingerprint, not strong-routed to sightmap
    assert not (
        result.pms == "sightmap"
        and any("Engrain widget" in e for e in result.evidence)
    )


def test_engrain_widget_missing_data_floorplan_does_not_fire() -> None:
    """``data-unit`` alone (no ``data-floorplan``) doesn't qualify —
    ``data-unit`` appears in unrelated CMS templates (Yardi Nestin uses
    ``data-unit-*`` for amenities) and isn't a definitive Engrain signal
    without the floorplan-pair."""
    html = (
        "<html><body>"
        '<script src="https://realpage.com/x.js"></script>'
        '<div data-unit="A101">Just a unit attr, no floorplan</div>'
        "</body></html>"
    )
    result = detect_pms("https://example.com/", page_html=html)
    assert not (
        result.pms == "sightmap"
        and any("Engrain widget" in e for e in result.evidence)
    )


def test_strict_sightmap_embed_still_wins_when_both_signals_present() -> None:
    """If ``sightmap.com/embed/`` IS in static HTML, the strict iframe
    path (confidence 0.90) outranks the Engrain widget signal (0.88).
    Validates we don't lose info to the weaker branch."""
    html = (
        "<html><body>"
        '<script src="https://realpage.com/x.js"></script>'
        '<div data-floorplan="X" data-unit="Y"></div>'
        '<iframe src="https://sightmap.com/embed/abc123"></iframe>'
        "</body></html>"
    )
    result = detect_pms("https://example.com/", page_html=html)
    assert result.pms == "sightmap"
    assert result.confidence >= 0.90
    # The strict evidence wins, not the Engrain branch
    assert any("embed iframe in HTML" in e for e in result.evidence)


def test_engrain_widget_beats_entrata_when_both_present() -> None:
    """Real chaseknollsapts-style co-resident pattern — Entrata widget
    on the page (as photo/amenities module) PLUS Engrain hydration attrs.
    The unit data lives in the SightMap-via-Engrain stack, not Entrata."""
    html = (
        "<html><body>"
        '<script src="https://commoncf.entrata.com/widgets/x.js"></script>'
        '<script src="https://www.realpage.com/widget/engrain.js"></script>'
        '<div data-floorplan="3x2" data-unit="B202">Plan</div>'
        "</body></html>"
    )
    result = detect_pms(
        "https://www.broadstonesobro.com/", page_html=html
    )
    assert result.pms == "sightmap", (
        f"expected sightmap (engrain widget wins over entrata-decoration), "
        f"got {result.pms!r}"
    )
