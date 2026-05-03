"""Phase 6 — stale-mapping eviction + envelope hash drift."""

from __future__ import annotations

import pytest

from models.scrape_profile import LlmFieldMapping, ScrapeProfile
from models.source import envelope_hash_of
from services.profile_store import ProfileStore
from services.profile_updater import update_profile_after_extraction


@pytest.fixture
def store(tmp_path):
    return ProfileStore(tmp_path / "profiles")


def test_drift_detected_increments_failures(store: ProfileStore) -> None:
    """Saved hash A, body hash B → replay path counts a failure."""
    body_b = {"v": 2}
    p = ScrapeProfile(canonical_id="phase6-001")
    p.api_hints.llm_field_mappings = [
        LlmFieldMapping(
            api_url_pattern="/api/x",
            json_paths={"unit_id": "id"},
            source_envelope_hash="hashA",
        )
    ]
    # Simulate the in-place drift path: hash mismatch → consecutive_replay_failures bumps.
    saved_hash = "hashA"
    current_hash = envelope_hash_of(body_b)
    assert saved_hash != current_hash
    p.api_hints.llm_field_mappings[0].consecutive_replay_failures += 1
    assert p.api_hints.llm_field_mappings[0].consecutive_replay_failures == 1


def test_eviction_at_3_failures(store: ProfileStore) -> None:
    p = ScrapeProfile(canonical_id="phase6-002")
    p.api_hints.llm_field_mappings = [
        LlmFieldMapping(
            api_url_pattern="/api/dead",
            json_paths={"unit_id": "id"},
            consecutive_replay_failures=3,
        )
    ]
    store.save(p)
    p = update_profile_after_extraction(
        p, {"extraction_tier_used": "TIER_3_DOM"}, 5, store
    )
    assert len(p.api_hints.llm_field_mappings) == 0


def test_no_eviction_below_threshold(store: ProfileStore) -> None:
    p = ScrapeProfile(canonical_id="phase6-003")
    p.api_hints.llm_field_mappings = [
        LlmFieldMapping(
            api_url_pattern="/api/maybe",
            json_paths={"unit_id": "id"},
            consecutive_replay_failures=2,
        )
    ]
    store.save(p)
    p = update_profile_after_extraction(
        p, {"extraction_tier_used": "TIER_3_DOM"}, 5, store
    )
    assert len(p.api_hints.llm_field_mappings) == 1


def test_save_mapping_records_envelope_hash(store: ProfileStore) -> None:
    from services.profile_updater import save_llm_field_mapping
    p = ScrapeProfile(canonical_id="phase6-004")
    body = {"units": [{"id": "U1", "rent": 100}]}
    h = envelope_hash_of(body)
    ok = save_llm_field_mapping(
        p,
        {"api_url_pattern": "/api/x", "json_paths": {"unit_id": "id"}},
        source_envelope_hash=h,
    )
    assert ok is True
    assert p.api_hints.llm_field_mappings[0].source_envelope_hash == h


def test_save_mapping_self_validation_demotes(store: ProfileStore) -> None:
    """Phase 10 self-validation — mapping that produces 1/5 expected gets quality_score 0.4."""
    from services.profile_updater import save_llm_field_mapping
    p = ScrapeProfile(canonical_id="phase6-005")
    # body has only 1 valid unit; expected_unit_count=5 → 0.2 ratio → demoted
    body = [{"id": "U1", "rent": 100}]
    ok = save_llm_field_mapping(
        p,
        {"api_url_pattern": "/api/x", "json_paths": {"unit_id": "id"}},
        body_for_validation=body,
        expected_unit_count=5,
    )
    assert ok is True
    qs = p.api_hints.llm_field_mappings[0].quality_score
    assert 0.4 <= qs < 0.8


def test_save_mapping_full_replay_keeps_quality_one(store: ProfileStore) -> None:
    from services.profile_updater import save_llm_field_mapping
    p = ScrapeProfile(canonical_id="phase6-006")
    body = [{"id": "U1"}, {"id": "U2"}, {"id": "U3"}]
    ok = save_llm_field_mapping(
        p,
        {"api_url_pattern": "/api/x", "json_paths": {"unit_id": "id"}},
        body_for_validation=body,
        expected_unit_count=3,
    )
    assert ok is True
    assert p.api_hints.llm_field_mappings[0].quality_score == 1.0


def test_cold_run_count_increments_for_cold_profile(store: ProfileStore) -> None:
    """Phase 10 — cold_run_count increments when maturity is COLD."""
    p = ScrapeProfile(canonical_id="phase6-007")
    store.save(p)
    # No success → stays COLD
    fail = {"extraction_tier_used": "FAILED"}
    p = update_profile_after_extraction(p, fail, 0, store)
    p = update_profile_after_extraction(p, fail, 0, store)
    p = update_profile_after_extraction(p, fail, 0, store)
    # After 3 failures, profile becomes COLD; cold_run_count should be > 0
    from models.scrape_profile import ProfileMaturity
    assert p.confidence.maturity == ProfileMaturity.COLD
    assert p.confidence.cold_run_count >= 1


def test_cold_run_count_resets_on_promotion(store: ProfileStore) -> None:
    p = ScrapeProfile(canonical_id="phase6-008")
    p.confidence.cold_run_count = 5
    p.confidence.maturity = __import__("models.scrape_profile", fromlist=["ProfileMaturity"]).ProfileMaturity.WARM
    store.save(p)
    success = {"extraction_tier_used": "TIER_1_API", "_raw_api_responses": []}
    p = update_profile_after_extraction(p, success, 5, store)
    assert p.confidence.cold_run_count == 0
