"""Tests for all-cohort endpoint-discovery route planning."""

from __future__ import annotations

from ma_poc.models.scrape_profile import ApiEndpoint, ScrapeProfile
from ma_poc.scripts.diagnostics.cohort_endpoint_route_plan import (
    DiscoveryRoute,
    _public_urls,
    make_route_record,
    normalize_public_url,
    select_route,
)


def _profile(platform: str = "unknown") -> ScrapeProfile:
    """Return a profile with a representative public availability path."""
    profile = ScrapeProfile(canonical_id="43")
    profile.dom_hints.platform_detected = platform
    profile.navigation.winning_page_url = "https://example.test/floorplans/"
    return profile


def test_route_prioritizes_verified_endpoint_revalidation() -> None:
    """A saved endpoint still receives fresh strict revalidation first."""
    profile = _profile("appfolio")
    profile.api_hints.known_endpoints.append(ApiEndpoint(url_pattern="https://api.example/units"))
    assert select_route(profile, _public_urls("https://example.test/", profile)) == (
        DiscoveryRoute.KNOWN_ENDPOINT_REVALIDATE
    )
    record = make_route_record({"property_id": "43", "url": "https://example.test/"}, profile)
    assert record.known_endpoint_templates == ("https://api.example/units",)


def test_route_omits_stale_or_cookie_bearing_endpoint_templates() -> None:
    """Route checkpoints cannot carry transient replay state into a worker."""
    profile = _profile()
    profile.api_hints.known_endpoints.extend(
        [
            ApiEndpoint(url_pattern="https://api.example/units?move_in_date=2026-07-27"),
            ApiEndpoint(url_pattern="https://api.example/units?cookie=transient"),
        ]
    )
    record = make_route_record({"property_id": "43", "url": "https://example.test/"}, profile)
    assert record.known_endpoint_templates == ()


def test_route_detects_each_supported_portal_family() -> None:
    """Platform-specific public portals never fall through to generic routing."""
    assert select_route(_profile("resman"), ()) == DiscoveryRoute.RESMAN_PORTAL_DISCOVERY
    assert select_route(_profile("rentcafe"), ()) == DiscoveryRoute.RENTCAFE_PORTAL_DISCOVERY
    assert select_route(_profile("entrata"), ()) == DiscoveryRoute.ENTRATA_BROWSER_XHR_DISCOVERY
    assert select_route(_profile("appfolio"), ()) == DiscoveryRoute.APPFOLIO_BROWSER_XHR_DISCOVERY
    assert select_route(_profile("realpage"), ()) == DiscoveryRoute.REALPAGE_BROWSER_XHR_DISCOVERY


def test_route_keeps_only_public_urls_and_bounds_navigation_candidates() -> None:
    """The durable queue excludes malformed values and cannot grow unbounded."""
    profile = _profile()
    profile.navigation.availability_links = [
        "javascript:alert(1)",
        "https://example.test/availability/",
        "https://example.test/availability/",
        "ftp://not-public.test/data",
        *[f"https://example.test/floorplans/{index}" for index in range(10)],
    ]
    urls = _public_urls("https://example.test/", profile)
    assert urls[0] == "https://example.test/floorplans/"
    assert "javascript:alert(1)" not in urls
    assert len(urls) == 8


def test_route_record_uses_only_the_requested_cohort_row() -> None:
    """A record retains its cohort id/source and makes no success claim."""
    record = make_route_record({"property_id": "602-1", "url": "https://x.test/"}, _profile())
    assert record.canonical_id == "602-1"
    assert record.source_url == "https://x.test/"
    assert record.route == DiscoveryRoute.GENERIC_BROWSER_XHR_DISCOVERY
    assert record.known_endpoint_count == 0


def test_route_normalizes_a_bare_marketing_host_but_not_relative_text() -> None:
    """A scheme-less source URL remains routable without admitting relative links."""
    source = "www.reserveatlenoxpark.net/floorplans/#/"
    assert normalize_public_url(source) == f"https://{source}"
    assert normalize_public_url("/floorplans/") is None
    record = make_route_record({"property_id": "14174", "url": source}, None)
    assert record.source_url == f"https://{source}"
    assert record.public_url_candidates == (f"https://{source}",)
