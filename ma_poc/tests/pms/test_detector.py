"""Phase 1 — detector tests. See claude_refactor.md Phase 1."""

from __future__ import annotations

import typing as t

import ma_poc.pms.adapters  # noqa: F401  # register adapters for confirm_detection
from ma_poc.pms.detector import (
    _STRATEGY_BY_PMS,
    MGMT_TO_PMS_PRIOR,
    DetectedPMS,
    confirm_detection,
    detect_pms,
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
    # Vanity domain with .aspx path — heuristic match (0.70).
    result = detect_pms("https://fairwaysatfayetteville.apartments/floorplans.aspx")
    assert result.pms == "rentcafe"
    assert result.confidence >= 0.70


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
