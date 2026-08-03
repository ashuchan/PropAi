from __future__ import annotations

from ma_poc.models.scrape_profile import (
    ApiEndpoint,
    LlmFieldMapping,
    ProfileMaturity,
    ScrapeProfile,
)
from ma_poc.services.profile_route_quarantine import (
    quarantine_reason,
    route_is_quarantined,
    sanitise_profile_routes,
)


def test_novi_bad_sightmap_route_is_removed_and_profile_demoted() -> None:
    bad = "https://sightmap.com/app/api/v1/yjp2415rvxl/sightmaps/104541"
    good = "https://example.test/novi-flats/floorplans"
    profile = ScrapeProfile(canonical_id="264077")
    profile.confidence.maturity = ProfileMaturity.HOT
    profile.navigation.winning_page_url = bad
    profile.navigation.availability_page_path = "/app/api/v1/yjp2415rvxl/sightmaps/104541"
    profile.navigation.availability_links = [bad, good]
    profile.api_hints.known_endpoints = [ApiEndpoint(url_pattern=bad)]
    profile.api_hints.llm_field_mappings = [LlmFieldMapping(api_url_pattern=bad)]

    _, removed = sanitise_profile_routes(profile)

    assert removed
    assert profile.navigation.winning_page_url is None
    assert profile.navigation.availability_page_path is None
    assert profile.navigation.availability_links == [good]
    assert profile.api_hints.known_endpoints == []
    assert profile.api_hints.llm_field_mappings == []
    assert profile.confidence.maturity == ProfileMaturity.COLD
    assert bad in profile.navigation.explored_links


def test_brookside_blacklist_is_property_scoped_and_keeps_correct_endpoint() -> None:
    wrong = "https://sightmap.com/app/api/v1/m9pzdr7mvk1/sightmaps/77845"
    correct = "https://sightmap.com/app/api/v1/m9pzj0k2vk1/sightmaps/117155"
    assert route_is_quarantined("49364", wrong)
    assert not route_is_quarantined("49364", correct)
    assert not route_is_quarantined("999", wrong)

    profile = ScrapeProfile(canonical_id="49364")
    profile.api_hints.known_endpoints = [
        ApiEndpoint(url_pattern=wrong),
        ApiEndpoint(url_pattern=correct),
    ]
    sanitise_profile_routes(profile)
    assert [e.url_pattern for e in profile.api_hints.known_endpoints] == [correct]


def test_confirmed_turtle_and_golfside_routes_have_documented_reasons() -> None:
    assert "The Onyx" in (quarantine_reason("222652", "/v1/property/2016765/units") or "")
    assert "sibling-community" in (
        quarantine_reason("22187", "https://mckinley.com/apartments/michigan/ann-arbor/glencoe-oaks/") or ""
    )
