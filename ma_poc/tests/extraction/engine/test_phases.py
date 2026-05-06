"""TDD tests for Phase1HomepageLoad, Phase2NoiseFilter, and PhaseRegistry."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from extraction.engine.phase_context import PhaseContext
from extraction.engine.phases.phase1_homepage_load import Phase1HomepageLoad
from extraction.engine.phases.phase2_noise_filter import Phase2NoiseFilter
from extraction.engine.phase_registry import PhaseRegistry


# ── Phase2NoiseFilter (pure, no browser needed) ────────────────────────────────

class TestPhase2NoiseFilter:
    def test_passes_through_when_no_profile(self):
        ctx = PhaseContext(base_url="https://example.com")
        ctx.api_responses = [
            {"url": "https://example.com/api/units", "body": []},
        ]
        Phase2NoiseFilter().run(ctx)
        assert len(ctx.filtered_responses) == 1

    def test_filters_profile_blocked_endpoint(self):
        class FakeBlockedEndpoint:
            url_pattern = "https://example.com/api/chatbot"

        class FakeApiHints:
            blocked_endpoints = [FakeBlockedEndpoint()]

        class FakeProfile:
            api_hints = FakeApiHints()

        ctx = PhaseContext(base_url="https://example.com", profile=FakeProfile())
        ctx.api_responses = [
            {"url": "https://example.com/api/chatbot", "body": {}},
            {"url": "https://example.com/api/units", "body": []},
        ]
        Phase2NoiseFilter().run(ctx)
        assert len(ctx.filtered_responses) == 1
        assert ctx.filtered_responses[0]["url"] == "https://example.com/api/units"

    def test_empty_api_responses_produces_empty_filtered(self):
        ctx = PhaseContext(base_url="https://example.com")
        Phase2NoiseFilter().run(ctx)
        assert ctx.filtered_responses == []

    def test_filtered_responses_is_independent_copy(self):
        ctx = PhaseContext(base_url="https://example.com")
        original = {"url": "https://example.com/api/units", "body": []}
        ctx.api_responses = [original]
        Phase2NoiseFilter().run(ctx)
        # Modifying filtered should not affect api_responses
        ctx.filtered_responses.clear()
        assert len(ctx.api_responses) == 1


# ── Phase1HomepageLoad (async, mocked browser) ─────────────────────────────────

class TestPhase1HomepageLoadPageUnreachable:
    @pytest.mark.asyncio
    async def test_sets_page_unreachable_on_ssl_error(self):
        ctx = PhaseContext(base_url="https://example.com")
        page = AsyncMock()
        page.goto = AsyncMock(side_effect=Exception("net::ERR_SSL_PROTOCOL_ERROR"))
        page.eval_on_selector_all = AsyncMock(return_value=[])
        page.evaluate = AsyncMock(return_value={})
        ctx.page = page

        await Phase1HomepageLoad().run(ctx)

        assert ctx.page_unreachable is True
        assert len(ctx.errors) == 1

    @pytest.mark.asyncio
    async def test_sets_page_unreachable_on_timeout(self):
        ctx = PhaseContext(base_url="https://example.com")
        page = AsyncMock()
        page.goto = AsyncMock(side_effect=Exception("Timeout exceeded"))
        page.eval_on_selector_all = AsyncMock(return_value=[])
        page.evaluate = AsyncMock(return_value={})
        ctx.page = page

        await Phase1HomepageLoad().run(ctx)

        assert ctx.page_unreachable is True

    @pytest.mark.asyncio
    async def test_does_not_set_unreachable_on_non_network_error(self):
        ctx = PhaseContext(base_url="https://example.com")
        page = AsyncMock()
        page.goto = AsyncMock(side_effect=Exception("Some script error"))
        page.eval_on_selector_all = AsyncMock(return_value=[])
        page.evaluate = AsyncMock(return_value={})
        ctx.page = page

        await Phase1HomepageLoad().run(ctx)

        assert ctx.page_unreachable is False

    @pytest.mark.asyncio
    async def test_collects_internal_links(self):
        ctx = PhaseContext(base_url="https://example.com")
        page = AsyncMock()
        page.goto = AsyncMock(return_value=None)
        page.wait_for_load_state = AsyncMock(return_value=None)
        page.evaluate = AsyncMock(return_value={})
        page.eval_on_selector_all = AsyncMock(return_value=[
            {"href": "/floor-plans", "text": "Floor Plans"},
            {"href": "/about", "text": "About Us"},
            {"href": "https://other.com/link", "text": "External"},
        ])
        ctx.page = page

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await Phase1HomepageLoad().run(ctx)

        assert "https://example.com/floor-plans" in ctx.internal_links
        assert "https://example.com/about" in ctx.internal_links
        # External link excluded
        assert "https://other.com/link" not in ctx.internal_links

    @pytest.mark.asyncio
    async def test_collects_anchor_text_links(self):
        ctx = PhaseContext(base_url="https://example.com")
        page = AsyncMock()
        page.goto = AsyncMock(return_value=None)
        page.wait_for_load_state = AsyncMock(return_value=None)
        page.evaluate = AsyncMock(return_value={})
        page.eval_on_selector_all = AsyncMock(return_value=[
            {"href": "/availability", "text": "View Availability"},
            {"href": "/contact", "text": "Contact Us"},
        ])
        ctx.page = page

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await Phase1HomepageLoad().run(ctx)

        assert "https://example.com/availability" in ctx.anchor_text_links
        assert "https://example.com/contact" not in ctx.anchor_text_links


# ── PhaseRegistry ──────────────────────────────────────────────────────────────

class TestPhaseRegistry:
    def test_default_phases_registered(self):
        registry = PhaseRegistry.default()
        names = [p.__class__.__name__ for p in registry.phases]
        assert "Phase1HomepageLoad" in names
        assert "Phase2NoiseFilter" in names

    def test_phases_in_order(self):
        registry = PhaseRegistry.default()
        names = [p.__class__.__name__ for p in registry.phases]
        p1_idx = names.index("Phase1HomepageLoad")
        p2_idx = names.index("Phase2NoiseFilter")
        assert p1_idx < p2_idx

    def test_custom_phases_registered(self):
        class DummyPhase:
            def run(self, ctx):
                pass

        registry = PhaseRegistry([DummyPhase()])
        assert len(registry.phases) == 1
        assert isinstance(registry.phases[0], DummyPhase)
