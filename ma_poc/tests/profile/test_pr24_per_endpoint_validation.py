"""PR24 Fix 3 — per-endpoint validation skipped on multi-source extraction."""

from __future__ import annotations

from ma_poc.models.scrape_profile import ScrapeProfile
from ma_poc.services.profile_updater import save_llm_field_mapping


def _make_profile() -> ScrapeProfile:
    return ScrapeProfile(canonical_id="test-multi-source-001")


def test_single_source_runs_validation() -> None:
    """Single source: body_for_validation + expected_unit_count triggers quality_score."""
    profile = _make_profile()
    # body with no unit data → replay produces 0 → quality_score < 1.0
    mapping = {
        "api_url_pattern": "/api/units",
        "json_paths": {"unit_id": "nonexistent_key"},
        "response_envelope": "",
    }
    body = {"items": [{"id": "1", "rent": 1500}]}
    result = save_llm_field_mapping(
        profile,
        mapping,
        body_for_validation=body,
        expected_unit_count=5,
        multi_source=False,
    )
    assert result is True
    saved = profile.api_hints.llm_field_mappings[0]
    # quality_score should be < 1.0 because replay produced 0/5 = 0 units
    assert saved.quality_score < 1.0, f"expected degraded quality_score, got {saved.quality_score}"


def test_multi_source_skips_validation() -> None:
    """Multi-source: quality_score stays 1.0 even with body+count provided."""
    profile = _make_profile()
    mapping = {
        "api_url_pattern": "/api/units",
        "json_paths": {"unit_id": "nonexistent_key"},
        "response_envelope": "",
    }
    body = {"items": [{"id": "1", "rent": 1500}]}
    result = save_llm_field_mapping(
        profile,
        mapping,
        body_for_validation=body,
        expected_unit_count=5,
        multi_source=True,  # <- should skip validation
    )
    assert result is True
    saved = profile.api_hints.llm_field_mappings[0]
    assert saved.quality_score == 1.0, \
        f"multi_source=True must skip self-validation, but got quality_score={saved.quality_score}"


def test_multi_source_default_is_false() -> None:
    """multi_source defaults to False — must not change existing single-source behavior."""
    import inspect
    sig = inspect.signature(save_llm_field_mapping)
    assert sig.parameters["multi_source"].default is False
