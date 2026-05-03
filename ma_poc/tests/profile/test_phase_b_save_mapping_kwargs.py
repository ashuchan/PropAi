"""Phase B tests — save_llm_field_mapping receives envelope_hash + quality_score."""

from __future__ import annotations

import pytest

from models.scrape_profile import ScrapeProfile
from models.source import envelope_hash_of
from services.profile_store import ProfileStore
from services.profile_updater import update_profile_after_extraction


@pytest.fixture
def store(tmp_path):
    return ProfileStore(tmp_path / "profiles")


def test_b_save_mapping_called_with_envelope_hash(store: ProfileStore) -> None:
    """update_profile_after_extraction must populate source_envelope_hash."""
    p = ScrapeProfile(canonical_id="phase_b_001")
    store.save(p)

    body = [{"id": "U1", "rent": 1500}, {"id": "U2", "rent": 1700}]
    scrape_result = {
        "extraction_tier_used": "TIER_4_LLM_API",
        "units": body,
        "_raw_api_responses": [{"url": "https://x.com/api/units", "body": body}],
        "_llm_analysis_results": {
            "https://x.com/api/units": {
                "api_url_pattern": "/api/units",
                "json_paths": {"unit_id": "id", "asking_rent": "rent"},
                "response_envelope": "",
            }
        },
    }
    p = update_profile_after_extraction(p, scrape_result, 2, store)
    assert len(p.api_hints.llm_field_mappings) == 1
    m = p.api_hints.llm_field_mappings[0]
    assert m.source_envelope_hash != "", "envelope_hash must be populated from _raw_api_responses"
    assert m.source_envelope_hash == envelope_hash_of(body)


def test_b_save_mapping_full_replay_quality_score_one(store: ProfileStore) -> None:
    """When the body replays to the same unit count, quality_score == 1.0."""
    p = ScrapeProfile(canonical_id="phase_b_003")
    store.save(p)

    body = [{"id": "U1", "rent": 1500}, {"id": "U2", "rent": 1700}]
    scrape_result = {
        "extraction_tier_used": "TIER_4_LLM_API",
        "units": [{"unit_id": "U1", "asking_rent": 1500}, {"unit_id": "U2", "asking_rent": 1700}],
        "_raw_api_responses": [{"url": "https://x.com/api/units", "body": body}],
        "_llm_analysis_results": {
            "https://x.com/api/units": {
                "api_url_pattern": "/api/units",
                "json_paths": {"unit_id": "id", "asking_rent": "rent"},
                "response_envelope": "",
            }
        },
    }
    p = update_profile_after_extraction(p, scrape_result, 2, store)
    assert p.api_hints.llm_field_mappings[0].quality_score == 1.0


def test_b_save_mapping_partial_replay_demotes_quality(store: ProfileStore) -> None:
    """When self-validation produces fewer units than expected, quality_score drops."""
    p = ScrapeProfile(canonical_id="phase_b_002")
    store.save(p)

    # body has 1 entry but cascade emitted 5 units (mapping mis-targets)
    body = [{"id": "U1", "rent": 1500}]
    scrape_result = {
        "extraction_tier_used": "TIER_4_LLM_API",
        "units": [{"unit_id": f"U{i}", "asking_rent": 1500 + i} for i in range(5)],
        "_raw_api_responses": [{"url": "https://x.com/api/u", "body": body}],
        "_llm_analysis_results": {
            "https://x.com/api/u": {
                "api_url_pattern": "/api/u",
                "json_paths": {"unit_id": "id", "asking_rent": "rent"},
                "response_envelope": "",
            }
        },
    }
    p = update_profile_after_extraction(p, scrape_result, 5, store)
    assert len(p.api_hints.llm_field_mappings) == 1
    qs = p.api_hints.llm_field_mappings[0].quality_score
    assert qs < 1.0, f"quality_score should be < 1.0 (partial replay), got {qs}"
    assert qs >= 0.4, f"quality_score floor is 0.4, got {qs}"


def test_b_no_matching_response_empty_hash(store: ProfileStore) -> None:
    """When no captured response URL matches the pattern, envelope_hash is ''."""
    p = ScrapeProfile(canonical_id="phase_b_004")
    store.save(p)

    scrape_result = {
        "extraction_tier_used": "TIER_4_LLM_API",
        "units": [{"unit_id": "U1"}],
        "_raw_api_responses": [{"url": "https://x.com/api/OTHER", "body": []}],
        "_llm_analysis_results": {
            "https://x.com/api/units": {
                "api_url_pattern": "/api/units",
                "json_paths": {"unit_id": "id"},
                "response_envelope": "",
            }
        },
    }
    p = update_profile_after_extraction(p, scrape_result, 1, store)
    assert len(p.api_hints.llm_field_mappings) == 1
    # No match → hash is empty
    assert p.api_hints.llm_field_mappings[0].source_envelope_hash == ""
