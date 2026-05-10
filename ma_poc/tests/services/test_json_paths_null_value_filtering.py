"""Regression: ``json_paths`` with null values must not crash the writer.

The LLM emits ``null`` for fields it could not map (e.g. ``"lease_term": null``,
``"move_in_date": null``). Pre-fix the unfiltered dict reached the
``ApiEndpoint(json_paths: dict[str, str])`` and ``LlmFieldMapping(json_paths:
dict[str, str])`` writers, where Pydantic's ``string_type`` validator rejected
the ``None`` values. The ``except Exception`` in ``jugnu._process_property``
swallowed the trace, the property's profile silently never updated, and the
self-learning loop died for the affected canonical_id.

In the 2026-05-10 cloud run this fired 199 times across ~199 distinct properties,
breaking the daily ``profile_replay_hit_rate`` SLO.

Two defensive layers, both tested here:

  Option C (upstream): ``llm_extractor.{extract_with_llm, analyze_api_with_llm}``
    scrub null values from ``json_paths`` so on-disk artifacts (LLM reports,
    saved mappings) stay clean for downstream replay matchers.

  Option A (construction-site): ``profile_updater.update_profile_after_extraction``
    and ``save_llm_field_mapping`` re-clean before each Pydantic constructor
    so any future caller path that bypasses Option C still doesn't crash.

The shared filter ``llm_extractor.clean_string_value_dict`` is exercised
directly so both consumers stay aligned on semantics.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from models.scrape_profile import ApiEndpoint, LlmFieldMapping, ScrapeProfile
from services.llm_extractor import (
    analyze_api_with_llm,
    clean_string_value_dict,
    extract_with_llm,
)
from services.profile_store import ProfileStore
from services.profile_updater import (
    save_llm_field_mapping,
    update_profile_after_extraction,
)

# ── clean_string_value_dict — the shared helper ─────────────────────────────


class TestCleanStringValueDict:
    def test_drops_none_values(self) -> None:
        assert clean_string_value_dict({"a": "x", "b": None, "c": "y"}) == {"a": "x", "c": "y"}

    def test_drops_empty_string_values(self) -> None:
        assert clean_string_value_dict({"a": "x", "b": "", "c": "  "}) == {"a": "x"}

    def test_drops_non_string_values(self) -> None:
        # int, list, dict, bool — the LLM has emitted all of these in the wild
        # when it free-styled the schema. Keep only string values.
        assert clean_string_value_dict({"a": 1, "b": [], "c": {}, "d": True, "e": "ok"}) == {"e": "ok"}

    def test_strips_whitespace(self) -> None:
        # Matches the convention already used for ``api_urls_with_data`` and
        # ``property_amenities`` in analyze_dom_with_llm.
        assert clean_string_value_dict({"a": "  data.units  "}) == {"a": "data.units"}

    def test_non_dict_input_returns_empty_dict(self) -> None:
        # Defensive: LLM occasionally emits ``json_paths`` as a list or null
        # despite the prompt schema. Empty dict beats a TypeError that
        # would propagate to the writer.
        assert clean_string_value_dict(None) == {}
        assert clean_string_value_dict([]) == {}
        assert clean_string_value_dict("not a dict") == {}
        assert clean_string_value_dict(42) == {}

    def test_empty_dict_returns_empty_dict(self) -> None:
        assert clean_string_value_dict({}) == {}


# ── Production crash repro — ApiEndpoint / LlmFieldMapping accept clean dict ─


class TestPydanticWritersAcceptCleanedJsonPaths:
    """Without the fix, these constructors raise ValidationError."""

    def test_api_endpoint_accepts_cleaned_dict(self) -> None:
        cleaned = clean_string_value_dict({"rent": None, "unit_id": "data.id", "lease_term": None})
        # Must not raise.
        ep = ApiEndpoint(url_pattern="https://api.example.com/units", json_paths=cleaned)
        assert ep.json_paths == {"unit_id": "data.id"}

    def test_llm_field_mapping_accepts_cleaned_dict(self) -> None:
        cleaned = clean_string_value_dict({"rent": None, "unit_id": "data.id"})
        # Must not raise.
        m = LlmFieldMapping(api_url_pattern="api.example.com/units", json_paths=cleaned)
        assert m.json_paths == {"unit_id": "data.id"}


# ── Option C — analyze_api_with_llm scrubs json_paths upstream ──────────────


@pytest.mark.asyncio
async def test_analyze_api_with_llm_drops_null_json_paths_values() -> None:
    """LLM returns ``json_paths`` with null values → emitted hints are clean.

    Repro of the 2026-05-10 production trace where the LLM mapped only
    ``unit_id`` and returned null for the rest.
    """

    async def fake_complete(self, system, prompt, max_tokens):
        return (
            '{"has_unit_data": true,'
            ' "units": [{"unit_id": "U1"}],'
            ' "json_paths": {"unit_id": "data.id", "rent": null, "lease_term": null},'
            ' "response_envelope": "data"}'
        )

    with patch("llm.factory.get_text_provider") as gp:
        gp.return_value.complete = fake_complete.__get__(gp.return_value)
        gp.return_value._last_usage = {"input_tokens": 100, "output_tokens": 50, "model": "x", "provider": "y"}
        _, hints, _, _ = await analyze_api_with_llm(
            {"url": "https://api.example.com/units", "body": {}},
            {"property_name": "Test", "city": "X", "state": "Y", "pmc": "", "website": ""},
        )

    assert hints is not None
    assert hints["json_paths"] == {"unit_id": "data.id"}, (
        "Null-valued keys must be dropped before reaching ApiEndpoint/LlmFieldMapping writers"
    )


@pytest.mark.asyncio
async def test_extract_with_llm_drops_null_json_paths_values() -> None:
    """Monolithic ``extract_with_llm`` (the LLM_TIER_4 fallback) emits its
    ``json_paths`` from the LLM's ``profile_hints`` envelope. The same null
    leakage as the targeted variant — same fix, same need for direct test
    coverage so a future refactor of either call site can't silently
    regress without breaking a test.
    """

    async def fake_complete(self, system, prompt, max_tokens):
        return (
            '{"units": [{"unit_id": "U1"}],'
            ' "profile_hints": {'
            '   "json_paths": {"unit_id": "data.id", "rent": null, "lease_term": null}'
            ' }}'
        )

    with patch("llm.factory.get_text_provider") as gp:
        gp.return_value.complete = fake_complete.__get__(gp.return_value)
        gp.return_value._last_usage = {"input_tokens": 100, "output_tokens": 50, "model": "x", "provider": "y"}
        # extract_with_llm takes a prepared input dict — minimal shape is fine
        # for the prompt-build branch (we mock the provider output anyway).
        llm_input = {
            "prompt": "...",
            "content_type": "HTML",
            "trimmed_content": "",
            "property_context": {"property_name": "Test"},
        }
        _, hints, _, _ = await extract_with_llm(llm_input)

    assert hints["json_paths"] == {"unit_id": "data.id"}, (
        "Null-valued keys must be dropped before reaching ApiEndpoint/LlmFieldMapping writers"
    )


@pytest.mark.asyncio
async def test_analyze_api_with_llm_all_null_paths_collapses_to_degraded() -> None:
    """When every json_path is null → empty dict (degraded mapping).

    Replay matcher will treat this as envelope-only, the existing
    save_llm_field_mapping degraded-path machinery handles it, and the URL
    still enters the profile for next-run prioritisation.
    """

    async def fake_complete(self, system, prompt, max_tokens):
        return (
            '{"has_unit_data": true,'
            ' "units": [{"unit_id": "U1"}],'
            ' "json_paths": {"rent": null, "unit_id": null},'
            ' "response_envelope": "data.units"}'
        )

    with patch("llm.factory.get_text_provider") as gp:
        gp.return_value.complete = fake_complete.__get__(gp.return_value)
        gp.return_value._last_usage = {"input_tokens": 100, "output_tokens": 50, "model": "x", "provider": "y"}
        _, hints, _, _ = await analyze_api_with_llm(
            {"url": "https://api.example.com/units", "body": {}},
            {"property_name": "Test", "city": "X", "state": "Y", "pmc": "", "website": ""},
        )

    assert hints is not None
    assert hints["json_paths"] == {}
    assert hints["response_envelope"] == "data.units"


# ── Option A — defensive at construction sites ──────────────────────────────


@pytest.fixture
def _reset_degraded_flag(monkeypatch):
    monkeypatch.setenv("ENABLE_DEGRADED_MAPPING_PERSIST", "true")


def test_save_llm_field_mapping_handles_null_json_paths_values(
    _reset_degraded_flag,
) -> None:
    """Construction-site defense: null values in ``mapping_dict["json_paths"]``
    must not crash. The dict is cleaned BEFORE the ``is_degraded`` classifier,
    so all-null becomes empty and the existing degraded-path machinery
    correctly persists at quality_score=0.5.
    """
    profile = ScrapeProfile(canonical_id="json-paths-null-001")
    ok = save_llm_field_mapping(
        profile,
        {
            "api_url_pattern": "https://api.example.com/units",
            "json_paths": {"rent": None, "unit_id": None},
            "response_envelope": "data",
        },
    )
    assert ok is True
    assert len(profile.api_hints.llm_field_mappings) == 1
    m = profile.api_hints.llm_field_mappings[0]
    # All-null collapses to empty → degraded path → quality_score=0.5.
    assert m.json_paths == {}
    assert m.quality_score == 0.5


def test_save_llm_field_mapping_drops_partial_null_values(
    _reset_degraded_flag,
) -> None:
    """Mixed null + valid values → only valid entries persist (full mapping)."""
    profile = ScrapeProfile(canonical_id="json-paths-null-002")
    ok = save_llm_field_mapping(
        profile,
        {
            "api_url_pattern": "https://api.example.com/units",
            "json_paths": {"unit_id": "data.id", "rent": None, "sqft": "data.sqft"},
            "response_envelope": "data",
        },
    )
    assert ok is True
    m = profile.api_hints.llm_field_mappings[0]
    assert m.json_paths == {"unit_id": "data.id", "sqft": "data.sqft"}
    # Partial cleaning still leaves a usable mapping → full quality.
    assert m.quality_score == 1.0


def test_update_profile_after_extraction_handles_null_json_paths_values(
    monkeypatch, tmp_path,
) -> None:
    """End-to-end: scrape_result with null-valued json_paths must not raise.

    Reproduces the exact production failure (jugnu.py:667) — the runner
    called update_profile_after_extraction and got a Pydantic ValidationError
    on ApiEndpoint(json_paths=...). With the defensive cleaning, the call
    succeeds and the URL is persisted to known_endpoints.
    """
    monkeypatch.setenv("ENABLE_DEGRADED_MAPPING_PERSIST", "true")
    profile = ScrapeProfile(canonical_id="e2e-null-001")
    store = ProfileStore(tmp_path / "profiles")
    store.save(profile)
    scrape_result = {
        "extraction_tier_used": "TIER_4_LLM",
        "units": [{"unit_id": "U1"}],
        "_llm_hints": {
            "api_urls_with_data": ["https://api.example.com/units"],
            "json_paths": {"unit_id": "data.id", "rent": None, "lease_term": None},
        },
    }

    updated = update_profile_after_extraction(profile, scrape_result, 1, store)
    assert updated is not None
    # The URL must reach known_endpoints — that's the whole point of the
    # learning loop. Pre-fix this never happened because ApiEndpoint
    # construction crashed first.
    assert any(
        ep.url_pattern == "https://api.example.com/units"
        for ep in updated.api_hints.known_endpoints
    )
    # And the persisted endpoint must have the cleaned mapping (no None values).
    ep = next(
        ep for ep in updated.api_hints.known_endpoints
        if ep.url_pattern == "https://api.example.com/units"
    )
    assert ep.json_paths == {"unit_id": "data.id"}
