"""Degraded ``LlmFieldMapping`` persistence (envelope-only, no json_paths).

Pre-PR: ``save_llm_field_mapping`` early-returned when ``json_paths`` was
empty, dropping ~99% of LLM-API tier outputs (the LLM extracts units via
semantic understanding without articulating per-field paths). Post-PR:
when ``response_envelope`` is set, the mapping persists with
``quality_score=0.5`` so the URL itself enters the cache. Drop only when
both ``json_paths`` AND ``response_envelope`` are empty.

Wrapped behind ``ENABLE_DEGRADED_MAPPING_PERSIST`` (default true) for kill
switch ability if degraded mappings start spamming the replay tier.

Test data shape: drawn from the 38 daily LLM-API winners observed in the
2026-05-10 cloud run that had non-empty ``response_envelope`` but empty
``json_paths`` (the cohort the prior code dropped silently).
"""

from __future__ import annotations

import pytest

from models.scrape_profile import ScrapeProfile
from services.profile_updater import save_llm_field_mapping


@pytest.fixture
def profile() -> ScrapeProfile:
    return ScrapeProfile(canonical_id="pr1-degraded-001")


@pytest.fixture(autouse=True)
def _reset_flag(monkeypatch):
    """Default to ON so each test starts from the documented production state."""
    monkeypatch.setenv("ENABLE_DEGRADED_MAPPING_PERSIST", "true")


def test_full_mapping_persists_unchanged(profile: ScrapeProfile) -> None:
    """Pre-fix happy path still works: full json_paths → full quality."""
    ok = save_llm_field_mapping(
        profile,
        {
            "api_url_pattern": "https://api.example.com/units",
            "json_paths": {"rent": "data.rent", "unit_id": "data.id"},
            "response_envelope": "data",
        },
    )
    assert ok is True
    assert len(profile.api_hints.llm_field_mappings) == 1
    m = profile.api_hints.llm_field_mappings[0]
    # PR 5 (2026-05-10): writer normalises URL → host/path (no scheme).
    assert m.api_url_pattern == "api.example.com/units"
    assert m.json_paths == {"rent": "data.rent", "unit_id": "data.id"}
    assert m.quality_score == 1.0


def test_degraded_mapping_persists_when_envelope_set(profile: ScrapeProfile) -> None:
    """Bug fix: empty json_paths but valid envelope → persist at quality 0.5.

    This is the exact shape that today silently drops ~38 of 41 daily
    LLM-API extractions: the LLM returns units + envelope but no per-field
    paths. The url enters the profile so the cascade can prioritise it.
    """
    ok = save_llm_field_mapping(
        profile,
        {
            "api_url_pattern": "https://api.example.com/units",
            "json_paths": {},
            "response_envelope": "data.units",
        },
    )
    assert ok is True
    assert len(profile.api_hints.llm_field_mappings) == 1
    m = profile.api_hints.llm_field_mappings[0]
    assert m.json_paths == {}
    assert m.response_envelope == "data.units"
    assert m.quality_score == 0.5, "Degraded mappings must be marked at 0.5"


def test_dropped_when_both_paths_and_envelope_empty(profile: ScrapeProfile) -> None:
    """Nothing learnable → drop. Prior behaviour was correct here."""
    ok = save_llm_field_mapping(
        profile,
        {
            "api_url_pattern": "https://api.example.com/units",
            "json_paths": {},
            "response_envelope": "",
        },
    )
    assert ok is False
    assert len(profile.api_hints.llm_field_mappings) == 0


def test_dropped_when_url_pattern_empty(profile: ScrapeProfile) -> None:
    """No URL → no addressable mapping. Drop regardless of paths/envelope."""
    ok = save_llm_field_mapping(
        profile,
        {
            "api_url_pattern": "",
            "json_paths": {"rent": "data.rent"},
            "response_envelope": "data",
        },
    )
    assert ok is False


def test_kill_switch_drops_degraded_when_flag_off(
    profile: ScrapeProfile, monkeypatch
) -> None:
    """ENABLE_DEGRADED_MAPPING_PERSIST=false → degraded path drops, full still saves."""
    monkeypatch.setenv("ENABLE_DEGRADED_MAPPING_PERSIST", "false")

    # Degraded — must drop
    ok_degraded = save_llm_field_mapping(
        profile,
        {
            "api_url_pattern": "https://api.example.com/units",
            "json_paths": {},
            "response_envelope": "data.units",
        },
    )
    assert ok_degraded is False
    assert len(profile.api_hints.llm_field_mappings) == 0

    # Full — must still save
    ok_full = save_llm_field_mapping(
        profile,
        {
            "api_url_pattern": "https://api.example.com/units",
            "json_paths": {"rent": "data.rent"},
            "response_envelope": "data",
        },
    )
    assert ok_full is True
    assert len(profile.api_hints.llm_field_mappings) == 1


def test_phase10_self_validation_skipped_for_degraded(profile: ScrapeProfile) -> None:
    """Phase 10 self-validation would replay a degraded mapping (no json_paths)
    against the body, get 0 units, and demote quality_score to 0.4. That's
    worse than our 0.5 floor and meaningless (degraded mappings can't replay
    by definition). Verify the skip.
    """
    ok = save_llm_field_mapping(
        profile,
        {
            "api_url_pattern": "https://api.example.com/units",
            "json_paths": {},
            "response_envelope": "data.units",
        },
        body_for_validation={"data": {"units": [{"id": 1}, {"id": 2}, {"id": 3}]}},
        expected_unit_count=3,
    )
    assert ok is True
    m = profile.api_hints.llm_field_mappings[0]
    assert m.quality_score == 0.5, "Phase 10 must not demote degraded mappings"


def test_full_mapping_self_validation_still_runs(profile: ScrapeProfile) -> None:
    """Sanity: Phase 10 self-validation still runs for non-degraded mappings."""
    # Mapping that won't match the body — Phase 10 demotes quality_score
    ok = save_llm_field_mapping(
        profile,
        {
            "api_url_pattern": "https://api.example.com/units",
            "json_paths": {"rent": "wrong.path"},  # doesn't match the body shape
            "response_envelope": "data.units",
        },
        body_for_validation={"data": {"units": [{"id": 1}, {"id": 2}, {"id": 3}]}},
        expected_unit_count=3,
    )
    assert ok is True
    m = profile.api_hints.llm_field_mappings[0]
    # Phase 10 demoted because the mapping didn't replay 80%+ of expected units
    assert m.quality_score < 1.0


def test_degraded_followup_does_not_overwrite_existing_full_mapping(
    profile: ScrapeProfile,
) -> None:
    """Self-review HIGH finding: a degraded follow-up arriving for an
    URL that already has a full mapping must NOT downgrade json_paths to
    empty or quality_score to 0.5. Without this, a single weak LLM call
    would silently clobber learned data on the upsert path.
    """
    # Run N: full mapping persisted at quality 1.0
    save_llm_field_mapping(
        profile,
        {
            "api_url_pattern": "https://api.example.com/units",
            "json_paths": {"rent": "data.rent", "unit_id": "data.id"},
            "response_envelope": "data",
        },
    )
    full = profile.api_hints.llm_field_mappings[0]
    assert full.json_paths == {"rent": "data.rent", "unit_id": "data.id"}
    assert full.quality_score == 1.0

    # Run N+1: a weak/degraded mapping for the SAME URL — must not clobber.
    ok = save_llm_field_mapping(
        profile,
        {
            "api_url_pattern": "https://api.example.com/units",
            "json_paths": {},
            "response_envelope": "data.units",
        },
    )
    assert ok is True

    # Existing entry's good data must be preserved.
    preserved = profile.api_hints.llm_field_mappings[0]
    assert preserved.json_paths == {"rent": "data.rent", "unit_id": "data.id"}, (
        "REGRESSION: degraded follow-up clobbered learned json_paths."
    )
    assert preserved.quality_score == 1.0, (
        "REGRESSION: degraded follow-up downgraded quality_score from 1.0 → 0.5."
    )


def test_full_followup_does_overwrite_existing_degraded(profile: ScrapeProfile) -> None:
    """Inverse: a full mapping arriving after a degraded one MUST upgrade.

    This is the happy compounding-improvement case: prior LLM gave
    envelope-only, today's LLM articulated json_paths too — we want the
    upgrade.
    """
    save_llm_field_mapping(
        profile,
        {
            "api_url_pattern": "https://api.example.com/units",
            "json_paths": {},
            "response_envelope": "data.units",
        },
    )
    assert profile.api_hints.llm_field_mappings[0].quality_score == 0.5

    save_llm_field_mapping(
        profile,
        {
            "api_url_pattern": "https://api.example.com/units",
            "json_paths": {"rent": "data.units.0.rent"},
            "response_envelope": "data.units",
        },
    )
    upgraded = profile.api_hints.llm_field_mappings[0]
    assert upgraded.json_paths == {"rent": "data.units.0.rent"}
    assert upgraded.quality_score == 1.0


def test_repeated_save_is_idempotent_on_url(profile: ScrapeProfile) -> None:
    """Same api_url_pattern saved twice → upserts, doesn't duplicate."""
    save_llm_field_mapping(
        profile,
        {
            "api_url_pattern": "https://api.example.com/units",
            "json_paths": {"rent": "data.rent"},
            "response_envelope": "data",
        },
    )
    save_llm_field_mapping(
        profile,
        {
            "api_url_pattern": "https://api.example.com/units",
            "json_paths": {"rent": "data.rent", "unit_id": "data.id"},
            "response_envelope": "data",
        },
    )
    assert len(profile.api_hints.llm_field_mappings) == 1
    # Second save's wider json_paths wins
    assert "unit_id" in profile.api_hints.llm_field_mappings[0].json_paths
