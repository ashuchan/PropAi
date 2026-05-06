"""TDD tests for extraction.engine.phase_context.PhaseContext."""
from __future__ import annotations

import pytest

from extraction.engine.phase_context import PhaseContext


class TestPhaseContext:
    def test_minimal_construction(self):
        ctx = PhaseContext(base_url="https://example.com")
        assert ctx.base_url == "https://example.com"

    def test_defaults(self):
        ctx = PhaseContext(base_url="https://example.com")
        assert ctx.profile is None
        assert ctx.expected_total_units is None
        assert ctx.property_city is None
        assert ctx.page is None
        assert ctx.api_responses == []
        assert ctx.filtered_responses == []
        assert ctx.internal_links == set()
        assert ctx.anchor_text_links == []
        assert ctx.property_metadata == {}
        assert ctx.property_name == ""
        assert ctx.page_unreachable is False
        assert ctx.errors == []
        assert ctx.units == []
        assert ctx.extraction_tier_used is None
        assert ctx.llm_analysis_results == {}
        assert ctx.explored_links_status == {}
        assert ctx.winning_page_url is None

    def test_base_url_https_normalization(self):
        ctx = PhaseContext(base_url="http://example.com")
        assert ctx.base_url == "https://example.com"

    def test_base_url_already_https_unchanged(self):
        ctx = PhaseContext(base_url="https://example.com/path")
        assert ctx.base_url == "https://example.com/path"

    def test_profile_stored(self):
        profile = object()
        ctx = PhaseContext(base_url="https://example.com", profile=profile)
        assert ctx.profile is profile

    def test_expected_total_units_stored(self):
        ctx = PhaseContext(base_url="https://example.com", expected_total_units=50)
        assert ctx.expected_total_units == 50

    def test_property_city_stored(self):
        ctx = PhaseContext(base_url="https://example.com", property_city="Scottsdale")
        assert ctx.property_city == "Scottsdale"
