"""Tests for profile updater — claude-scrapper-arch.md Step 6.4."""
from __future__ import annotations

import pytest

from models.scrape_profile import ProfileMaturity, ScrapeProfile
from services.profile_store import ProfileStore
from services.profile_updater import update_profile_after_extraction


@pytest.fixture
def store(tmp_path):
    return ProfileStore(tmp_path / "profiles")


def test_update_after_tier1_success_records_api_urls(store: ProfileStore) -> None:
    p = ScrapeProfile(canonical_id="t1-001")
    store.save(p)
    result = {
        "extraction_tier_used": "TIER_1_API",
        "_raw_api_responses": [
            {"url": "/api/units", "body": {"units": [{"rent": 1200}]}},
        ],
    }
    updated = update_profile_after_extraction(p, result, 5, store)
    assert len(updated.api_hints.known_endpoints) == 1
    assert updated.api_hints.known_endpoints[0].url_pattern == "/api/units"


def test_update_after_llm_success_writes_css_selectors(store: ProfileStore) -> None:
    p = ScrapeProfile(canonical_id="llm-001")
    store.save(p)
    result = {
        "extraction_tier_used": "TIER_4_LLM",
        "_llm_hints": {
            "css_selectors": {"container": ".unit-row", "rent": ".price"},
            "platform_guess": "entrata",
            "field_mapping_notes": "Data in table rows",
        },
    }
    updated = update_profile_after_extraction(p, result, 10, store)
    assert updated.dom_hints.field_selectors.container == ".unit-row"
    assert updated.dom_hints.field_selectors.rent == ".price"
    assert updated.dom_hints.platform_detected == "entrata"


def test_update_after_llm_success_writes_json_paths(store: ProfileStore) -> None:
    p = ScrapeProfile(canonical_id="llm-002")
    store.save(p)
    result = {
        "extraction_tier_used": "TIER_4_LLM",
        "_llm_hints": {
            "api_urls_with_data": ["/api/v1/units"],
            "json_paths": {"rent": "$.data.rent", "unit_id": "$.data.id"},
        },
    }
    updated = update_profile_after_extraction(p, result, 5, store)
    assert len(updated.api_hints.known_endpoints) == 1
    assert updated.api_hints.known_endpoints[0].json_paths["rent"] == "$.data.rent"


def test_maturity_promotion_cold_to_warm_after_1_success(store: ProfileStore) -> None:
    p = ScrapeProfile(canonical_id="promo-001")
    store.save(p)
    result = {"extraction_tier_used": "TIER_3_DOM"}
    updated = update_profile_after_extraction(p, result, 5, store)
    assert updated.confidence.maturity == ProfileMaturity.WARM


def test_maturity_promotion_warm_to_hot_after_3_successes(store: ProfileStore) -> None:
    p = ScrapeProfile(canonical_id="promo-002")
    store.save(p)
    result = {"extraction_tier_used": "TIER_1_API", "_raw_api_responses": []}
    for _ in range(3):
        p = update_profile_after_extraction(p, result, 10, store)
    assert p.confidence.maturity == ProfileMaturity.HOT
    assert p.confidence.consecutive_successes == 3


def test_consecutive_failures_resets_on_success(store: ProfileStore) -> None:
    p = ScrapeProfile(canonical_id="reset-001")
    p.confidence.consecutive_failures = 5
    store.save(p)
    result = {"extraction_tier_used": "TIER_3_DOM"}
    updated = update_profile_after_extraction(p, result, 3, store)
    assert updated.confidence.consecutive_failures == 0
    assert updated.confidence.consecutive_successes == 1


def test_navigation_hints_recorded_from_crawled_urls(store: ProfileStore) -> None:
    p = ScrapeProfile(canonical_id="nav-001")
    store.save(p)
    result = {
        "extraction_tier_used": "TIER_3_DOM",
        "property_links_crawled": [
            "https://example.com/gallery",
            "https://example.com/floor-plans",
            "https://example.com/contact",
        ],
    }
    updated = update_profile_after_extraction(p, result, 5, store)
    assert updated.navigation.availability_page_path == "/floor-plans"
