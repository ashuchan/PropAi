"""Phase 7 — Channel-4 field_patches tests."""

from __future__ import annotations

import pytest

from models.scrape_profile import FieldPatch, ScrapeProfile
from services.profile_store import ProfileStore
from services.profile_updater import save_field_patch, update_profile_after_extraction


@pytest.fixture
def store(tmp_path):
    return ProfileStore(tmp_path / "profiles")


def test_save_field_patch_strips_dollar_prefix() -> None:
    p = ScrapeProfile(canonical_id="phase7-001")
    ok = save_field_patch(
        p,
        {
            "api_url_pattern": "/api/x",
            "field_name": "rent_low",
            "json_path": "$.bedroom_range.0",
            "confidence": 0.9,
        },
    )
    assert ok is True
    assert p.api_hints.field_patches[0].json_path == "bedroom_range.0"


def test_save_field_patch_rejects_missing_url() -> None:
    p = ScrapeProfile(canonical_id="phase7-002")
    ok = save_field_patch(
        p,
        {"api_url_pattern": "", "field_name": "rent_low", "json_path": "x"},
    )
    assert ok is False


def test_save_field_patch_rejects_missing_field_name() -> None:
    p = ScrapeProfile(canonical_id="phase7-003")
    ok = save_field_patch(
        p,
        {"api_url_pattern": "/api/x", "field_name": "", "json_path": "x"},
    )
    assert ok is False


def test_field_patch_upsert_by_url_and_field() -> None:
    p = ScrapeProfile(canonical_id="phase7-004")
    save_field_patch(
        p,
        {"api_url_pattern": "/api/x", "field_name": "rent_low", "json_path": "a", "confidence": 0.7},
    )
    save_field_patch(
        p,
        {"api_url_pattern": "/api/x", "field_name": "rent_low", "json_path": "b", "confidence": 0.95},
    )
    # Same (url, field_name) → upsert (1 entry total)
    assert len(p.api_hints.field_patches) == 1
    assert p.api_hints.field_patches[0].json_path == "b"
    assert p.api_hints.field_patches[0].confidence == 0.95


def test_field_patch_persisted_through_orchestrator(store: ProfileStore) -> None:
    p = ScrapeProfile(canonical_id="phase7-005")
    store.save(p)
    scrape_result = {
        "extraction_tier_used": "TIER_4_LLM",
        "_field_patches": [
            {
                "api_url_pattern": "/api/availability",
                "field_name": "rent_low",
                "json_path": "$.rent_range.min",
                "confidence": 0.92,
                "_envelope_hash": "abc1234567890def",
            }
        ],
    }
    p = update_profile_after_extraction(p, scrape_result, 8, store)
    assert len(p.api_hints.field_patches) == 1
    fp = p.api_hints.field_patches[0]
    assert fp.field_name == "rent_low"
    assert fp.json_path == "rent_range.min"
    assert fp.source_envelope_hash == "abc1234567890def"


def test_field_patch_eviction_at_3_failures(store: ProfileStore) -> None:
    p = ScrapeProfile(canonical_id="phase7-006")
    p.api_hints.field_patches = [
        FieldPatch(
            api_url_pattern="/api/dead",
            field_name="rent_low",
            json_path="x",
            consecutive_replay_failures=3,
        )
    ]
    store.save(p)
    p = update_profile_after_extraction(
        p, {"extraction_tier_used": "TIER_3_DOM"}, 5, store
    )
    assert len(p.api_hints.field_patches) == 0


def test_field_patch_capped_at_50() -> None:
    p = ScrapeProfile(canonical_id="phase7-007")
    for i in range(60):
        save_field_patch(
            p,
            {
                "api_url_pattern": f"/api/x{i}",
                "field_name": "rent_low",
                "json_path": f"path_{i}",
            },
        )
    assert len(p.api_hints.field_patches) <= 50


def test_unsupported_field_name_rejected_by_pydantic() -> None:
    p = ScrapeProfile(canonical_id="phase7-008")
    # `field_name` is constrained by Literal — pydantic raises on save_field_patch.
    # Our save_field_patch wraps in try/except; verify graceful False return.
    ok = save_field_patch(
        p,
        {
            "api_url_pattern": "/api/x",
            "field_name": "BOGUS_FIELD",
            "json_path": "y",
        },
    )
    assert ok is False
    assert len(p.api_hints.field_patches) == 0
