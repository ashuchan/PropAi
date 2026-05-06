"""TDD tests for extraction.engine.noise_filter.

Written before the module exists — these drive the extraction of noise-filter
logic out of scripts/entrata.py into a focused, testable module.
"""
from __future__ import annotations

import pytest

from extraction.engine.noise_filter import (
    _ALLOWLIST_OVERRIDES,
    _FALSE_POSITIVE_HOSTS,
    _FALSE_POSITIVE_PATH_FRAGMENTS,
    filter_network_noise,
    looks_like_availability_api,
)


# ── looks_like_availability_api ────────────────────────────────────────────────

class TestLooksLikeAvailabilityApi:
    def test_accepts_floor_plans_url(self):
        assert looks_like_availability_api("https://example.com/api/floor-plans") is True

    def test_accepts_availabilities_url(self):
        assert looks_like_availability_api("https://example.com/availabilities") is True

    def test_accepts_units_url(self):
        assert looks_like_availability_api("https://example.com/api/units") is True

    def test_rejects_google_analytics(self):
        assert looks_like_availability_api("https://google-analytics.com/api/collect") is False

    def test_rejects_googletagmanager(self):
        assert looks_like_availability_api("https://www.googletagmanager.com/gtag/js") is False

    def test_rejects_hotjar(self):
        assert looks_like_availability_api("https://hotjar.com/api/info") is False

    def test_rejects_sentry(self):
        assert looks_like_availability_api("https://sentry.io/api/0/envelope/") is False

    def test_rejects_meetelise_chatbot(self):
        assert looks_like_availability_api("https://app.meetelise.com/api/units") is False

    def test_rejects_tag_manager_path(self):
        assert looks_like_availability_api("https://example.com/tag-manager/script.js") is False

    def test_rejects_analytics_path(self):
        assert looks_like_availability_api("https://example.com/analytics/track") is False

    def test_rejects_beacon_path(self):
        assert looks_like_availability_api("https://example.com/beacon/ping") is False

    def test_allowlist_overrides_blocked_host(self):
        # api.ws.realpage.com is in the allowlist despite realpage pattern match
        url = "https://api.ws.realpage.com/v2/property/123/floorplans"
        assert looks_like_availability_api(url) is True

    def test_rejects_subdomain_of_blocked_host(self):
        assert looks_like_availability_api("https://api.hotjar.com/api/units") is False

    def test_rejects_unknown_url_with_no_patterns(self):
        assert looks_like_availability_api("https://example.com/contact") is False

    def test_accepts_getfloorplans(self):
        assert looks_like_availability_api("https://example.com/getFloorPlans") is True

    def test_case_insensitive_pattern_match(self):
        assert looks_like_availability_api("https://example.com/FloorPlan/list") is True


# ── filter_network_noise ───────────────────────────────────────────────────────

class TestFilterNetworkNoise:
    def test_returns_all_when_no_profile(self):
        responses = [
            {"url": "https://example.com/api/units", "body": []},
            {"url": "https://example.com/floor-plans", "body": []},
        ]
        result = filter_network_noise(responses, profile=None)
        assert result == responses

    def test_filters_profile_blocked_endpoint_exact(self):
        blocked_url = "https://example.com/api/chatbot"

        class FakeBlockedEndpoint:
            url_pattern = blocked_url

        class FakeApiHints:
            blocked_endpoints = [FakeBlockedEndpoint()]

        class FakeProfile:
            api_hints = FakeApiHints()

        responses = [
            {"url": blocked_url, "body": {}},
            {"url": "https://example.com/api/units", "body": []},
        ]
        result = filter_network_noise(responses, profile=FakeProfile())
        assert len(result) == 1
        assert result[0]["url"] == "https://example.com/api/units"

    def test_filters_profile_blocked_endpoint_substring(self):
        class FakeBlockedEndpoint:
            url_pattern = "/api/chatbot"

        class FakeApiHints:
            blocked_endpoints = [FakeBlockedEndpoint()]

        class FakeProfile:
            api_hints = FakeApiHints()

        responses = [
            {"url": "https://example.com/api/chatbot/config", "body": {}},
            {"url": "https://example.com/api/units", "body": []},
        ]
        result = filter_network_noise(responses, profile=FakeProfile())
        assert len(result) == 1
        assert result[0]["url"] == "https://example.com/api/units"

    def test_passes_through_when_no_blocked_endpoints(self):
        class FakeApiHints:
            blocked_endpoints = []

        class FakeProfile:
            api_hints = FakeApiHints()

        responses = [{"url": "https://example.com/api/units", "body": []}]
        result = filter_network_noise(responses, profile=FakeProfile())
        assert result == responses

    def test_passes_through_when_profile_has_no_api_hints(self):
        class FakeProfile:
            api_hints = None

        responses = [{"url": "https://example.com/api/units", "body": []}]
        result = filter_network_noise(responses, profile=FakeProfile())
        assert result == responses

    def test_empty_responses_returns_empty(self):
        result = filter_network_noise([], profile=None)
        assert result == []


# ── Constants ──────────────────────────────────────────────────────────────────

class TestConstants:
    def test_false_positive_hosts_is_nonempty_set(self):
        assert isinstance(_FALSE_POSITIVE_HOSTS, (set, frozenset))
        assert len(_FALSE_POSITIVE_HOSTS) > 0

    def test_false_positive_path_fragments_is_nonempty_set(self):
        assert isinstance(_FALSE_POSITIVE_PATH_FRAGMENTS, (set, frozenset))
        assert len(_FALSE_POSITIVE_PATH_FRAGMENTS) > 0

    def test_allowlist_overrides_contains_realpage(self):
        assert "api.ws.realpage.com" in _ALLOWLIST_OVERRIDES

    def test_googleapis_in_false_positive_hosts(self):
        assert "googleapis.com" in _FALSE_POSITIVE_HOSTS

    def test_tag_manager_in_false_positive_paths(self):
        assert "/tag-manager/" in _FALSE_POSITIVE_PATH_FRAGMENTS
