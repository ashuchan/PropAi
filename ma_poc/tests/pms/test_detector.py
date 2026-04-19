"""Phase 1 — detector tests. See claude_refactor.md Phase 1."""
from __future__ import annotations

import typing as t

from ma_poc.pms.detector import (
    _STRATEGY_BY_PMS,
    MGMT_TO_PMS_PRIOR,
    DetectedPMS,
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
    result = detect_pms(
        "https://livecommonplace.appfolio.com/listings/rental_applications/new"
    )
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
    html = (
        "<html><head>"
        '<script src="https://static1.squarespace.com/x.js"></script>'
        '<img src="https://cdngeneralcf.rentcafe.com/foo.jpg">'
        "</head></html>"
    )
    r = detect_pms("https://vanity.example/", page_html=html)
    assert r.pms == "squarespace_nopms"
