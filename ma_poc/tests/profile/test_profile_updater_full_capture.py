"""Pin the new profile-updater capture surface.

The previous behaviour silently dropped LLM-derived hints when a
deterministic tier (e.g. ``TIER_3_DOM`` or ``TIER_MERGED_CROSS_PAGE``)
won the run — the entire ``_llm_hints`` block was gated on
``tier in llm_tiers``. Many other signals were also model-only:
``property_amenities`` had no profile slot, raw ``navigation_hint``
was never persisted, drift hashes lived only in declarations,
``last_api_analysis_results`` was never populated.

These tests pin the new contract:
  - LLM hints persist regardless of which tier ultimately won.
  - Property-level amenities accumulate on the profile, deduplicated.
  - Raw navigation hints from the LLM are stored with newest-last
    semantics (cap = 10).
  - Drift hashes (extraction_prompt_hash / dom_structure_hash /
    api_schema_signature) round-trip when supplied.
  - ``last_api_analysis_results`` mirrors the per-URL verdict map.
  - ``cap_explored_links`` keeps newest entries (Bug 4.2 fix).
"""

from __future__ import annotations

import pytest

from models.scrape_profile import ScrapeProfile
from services.profile_store import ProfileStore
from services.profile_updater import update_profile_after_extraction


@pytest.fixture
def store(tmp_path):
    return ProfileStore(tmp_path / "profiles")


def test_llm_hints_persist_when_deterministic_tier_won(store: ProfileStore) -> None:
    """LLM ran and produced learnable signals, but a deterministic tier
    (TIER_3_DOM) ultimately won. Bug 2.6 — those hints used to be
    silently dropped because the writer block was tier-gated.
    """
    p = ScrapeProfile(canonical_id="merge-001")
    store.save(p)
    result = {
        "extraction_tier_used": "TIER_3_DOM",  # deterministic winner
        "_llm_hints": {
            "css_selectors": {"container": ".unit-card", "rent": ".price"},
            "platform_guess": "rentcafe",
            "navigation_hint": "/floorplans",
            "api_urls_with_data": ["https://api.example.com/floorplans"],
            "field_mapping_notes": "Units live in .unit-card",
        },
    }
    p = update_profile_after_extraction(p, result, 5, store)

    # CSS selectors persisted despite non-LLM tier winning.
    assert p.dom_hints.field_selectors.container == ".unit-card"
    assert p.dom_hints.field_selectors.rent == ".price"
    assert p.dom_hints.platform_detected == "rentcafe"
    assert p.api_hints.api_provider == "rentcafe"
    assert p.llm_artifacts.field_mapping_notes == "Units live in .unit-card"
    # API URL collected too:
    assert any(
        ep.url_pattern == "https://api.example.com/floorplans"
        for ep in p.api_hints.known_endpoints
    )
    # Raw nav hint stored:
    assert "/floorplans" in p.navigation.last_navigation_hints


def test_property_amenities_accumulate_deduped(store: ProfileStore) -> None:
    p = ScrapeProfile(canonical_id="amen-001")
    store.save(p)
    p.property_amenities = ["clubhouse"]  # prior run discovered one
    result = {
        "extraction_tier_used": "TIER_4_LLM",
        "property_amenities": ["pool", "Pool", "Gym", "  ", "clubhouse"],
    }
    p = update_profile_after_extraction(p, result, 3, store)
    # Lowercased, deduped, prior amenity ("clubhouse") preserved.
    assert sorted(p.property_amenities) == ["clubhouse", "gym", "pool"]


def test_drift_hashes_round_trip(store: ProfileStore) -> None:
    p = ScrapeProfile(canonical_id="drift-001")
    store.save(p)
    result = {
        "extraction_tier_used": "TIER_4_LLM",
        "_llm_hints": {
            "css_selectors": {"container": ".unit"},
            "extraction_prompt_hash": "abc123def456",
            "dom_structure_hash": "0123456789abcdef",
            "api_schema_signature": "fedcba9876543210",
        },
    }
    p = update_profile_after_extraction(p, result, 2, store)
    assert p.llm_artifacts.extraction_prompt_hash == "abc123def456"
    assert p.llm_artifacts.dom_structure_hash == "0123456789abcdef"
    assert p.llm_artifacts.api_schema_signature == "fedcba9876543210"


def test_last_api_analysis_results_mirrors_verdicts(store: ProfileStore) -> None:
    p = ScrapeProfile(canonical_id="verdict-001")
    store.save(p)
    result = {
        "extraction_tier_used": "TIER_4_LLM_API",
        "_raw_api_responses": [
            {"url": "/api/units", "body": {"units": [{"rent": 1500}]}},
            {"url": "/api/chat", "body": {"config": {"theme": "dark"}}},
        ],
        "_llm_analysis_results": {
            "/api/units": {
                "api_url_pattern": "/api/units",
                "json_paths": {"rent": "units.0.rent"},
                "response_envelope": "",
            },
            "/api/chat": "noise:chatbot_config",
        },
        "units": [{"unit_id": "U1", "rent": 1500}],
    }
    p = update_profile_after_extraction(p, result, 1, store)
    verdicts = p.llm_artifacts.last_api_analysis_results
    assert verdicts["/api/units"] == "has_units"
    assert verdicts["/api/chat"].startswith("noise:")
    assert "chatbot_config" in verdicts["/api/chat"]


def test_navigation_hint_appended_newest_last(store: ProfileStore) -> None:
    p = ScrapeProfile(canonical_id="nav-001")
    p.navigation.last_navigation_hints = ["/old-hint"]
    store.save(p)
    result = {
        "extraction_tier_used": "TIER_4_LLM",
        "_llm_hints": {"navigation_hint": "/floorplans"},
    }
    p = update_profile_after_extraction(p, result, 1, store)
    # Newest is last, so /floorplans wins the priority slot.
    assert p.navigation.last_navigation_hints[-1] == "/floorplans"
    assert "/old-hint" in p.navigation.last_navigation_hints


def test_unrecognised_analysis_value_logged_not_silently_dropped(
    store: ProfileStore, caplog
) -> None:
    """Bug 4.4 — when ``_llm_analysis_results`` carries an unrecognised
    shape, log loudly so a future shape drift surfaces immediately
    instead of being silently dropped."""
    import logging
    p = ScrapeProfile(canonical_id="shape-drift-001")
    store.save(p)
    result = {
        "extraction_tier_used": "TIER_4_LLM_API",
        "_raw_api_responses": [],
        "_llm_analysis_results": {
            "/api/weird": 42,  # int — definitely not a known verdict shape
        },
    }
    with caplog.at_level(logging.WARNING):
        update_profile_after_extraction(p, result, 0, store)
    assert any(
        "Unrecognised _llm_analysis_results" in rec.message
        for rec in caplog.records
    )


def test_cap_explored_links_keeps_newest() -> None:
    """Bug 4.2 — validator on NavigationConfig.explored_links must
    keep the **newest** 50 entries, not the oldest."""
    from models.scrape_profile import NavigationConfig
    nav = NavigationConfig(explored_links=[f"/page-{i}" for i in range(60)])
    # 50 entries kept; the cap drops the OLDEST 10, keeping /page-10..59.
    assert len(nav.explored_links) == 50
    assert nav.explored_links[0] == "/page-10"
    assert nav.explored_links[-1] == "/page-59"
