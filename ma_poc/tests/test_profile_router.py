"""Tests for profile router — claude-scrapper-arch.md Step 6.6."""
from __future__ import annotations

from models.scrape_profile import ProfileMaturity, ScrapeProfile
from services.profile_router import RouteDecision, route


def _make_profile(maturity: ProfileMaturity, preferred_tier: int | None = None) -> ScrapeProfile:
    p = ScrapeProfile(canonical_id="route-test")
    p.confidence.maturity = maturity
    if preferred_tier is not None:
        p.confidence.preferred_tier = preferred_tier
    return p


def test_hot_profile_skips_to_preferred_tier() -> None:
    p = _make_profile(ProfileMaturity.HOT, preferred_tier=1)
    decision = route(p)
    assert decision.skip_to_tier == 1
    assert decision.run_full_cascade is False


def test_warm_profile_tries_preferred_then_cascade() -> None:
    p = _make_profile(ProfileMaturity.WARM, preferred_tier=2)
    decision = route(p)
    assert decision.skip_to_tier == 2
    assert decision.run_full_cascade is True


def test_cold_profile_runs_full_cascade() -> None:
    p = _make_profile(ProfileMaturity.COLD)
    decision = route(p)
    assert decision.skip_to_tier is None
    assert decision.run_full_cascade is True


def test_custom_timeout_from_profile() -> None:
    p = _make_profile(ProfileMaturity.HOT, preferred_tier=1)
    p.navigation.timeout_ms = 30000
    decision = route(p)
    assert decision.custom_timeout_ms == 30000


def test_block_domains_from_profile() -> None:
    p = _make_profile(ProfileMaturity.WARM)
    p.navigation.block_resource_domains = ["analytics.example.com", "tracker.io"]
    decision = route(p)
    assert "analytics.example.com" in decision.block_domains
    assert "tracker.io" in decision.block_domains


def test_entry_url_override() -> None:
    p = _make_profile(ProfileMaturity.HOT, preferred_tier=3)
    p.navigation.entry_url = "https://example.com/floor-plans"
    decision = route(p)
    assert decision.entry_url == "https://example.com/floor-plans"
