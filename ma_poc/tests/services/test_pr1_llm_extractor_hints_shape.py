"""PR 1 — Drop 1 (producer-side) fix verification.

Pre-PR: ``analyze_api_with_llm`` only populated ``api_url_pattern`` in the
returned hints dict when the LLM emitted a non-empty ``json_paths``. When
the LLM extracted units via semantic understanding without articulating
per-field paths, the hints dict was missing ``api_url_pattern`` — and the
downstream surfacing site + persistence layer had nothing to address by URL.

Post-PR: ``api_url_pattern`` is always populated when units were extracted.
``json_paths`` and ``response_envelope`` are set when present, empty when
not — but the URL itself always reaches the persistence layer.

These tests exercise the helper that builds the hints dict (the construction
is the bug surface; we don't need to call out to a real LLM provider).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest


@pytest.mark.asyncio
async def test_units_with_full_json_paths_carry_full_mapping_triplet() -> None:
    """Happy path: LLM returns units + json_paths + envelope → all three present."""
    from services.llm_extractor import analyze_api_with_llm

    async def fake_complete(self, system, prompt, max_tokens):
        return (
            '{"has_unit_data": true, "units": [{"unit_id": "U1", "rent": 1500}],'
            '"json_paths": {"unit_id": "id", "rent": "rent"},'
            '"response_envelope": "data.units"}'
        )

    with patch("llm.factory.get_text_provider") as gp:
        gp.return_value.complete = fake_complete.__get__(gp.return_value)
        gp.return_value._last_usage = {"input_tokens": 100, "output_tokens": 50, "model": "x", "provider": "y"}
        units, hints, is_noise, _ = await analyze_api_with_llm(
            {"url": "https://api.example.com/units?cursor=abc", "body": {"data": {"units": []}}},
            {"property_name": "Test", "city": "X", "state": "Y", "pmc": "", "website": ""},
        )

    assert is_noise is False
    assert len(units) == 1
    assert hints is not None
    assert hints["api_url_pattern"] == "https://api.example.com/units?cursor=abc"
    assert hints["json_paths"] == {"unit_id": "id", "rent": "rent"}
    assert hints["response_envelope"] == "data.units"


@pytest.mark.asyncio
async def test_units_with_empty_json_paths_still_carry_url_pattern() -> None:
    """Bug fix (Drop 1): LLM returns units but no json_paths → url MUST persist.

    This is the cohort the prior code dropped silently — units extracted
    via semantic understanding rather than path-tracing. Affects ~38 of
    41 daily LLM-API winners on the 2026-05-10 cloud run.
    """
    from services.llm_extractor import analyze_api_with_llm

    async def fake_complete(self, system, prompt, max_tokens):
        return (
            '{"has_unit_data": true, "units": [{"unit_id": "U1", "rent": 1500}],'
            '"response_envelope": "data.units"}'  # no json_paths
        )

    with patch("llm.factory.get_text_provider") as gp:
        gp.return_value.complete = fake_complete.__get__(gp.return_value)
        gp.return_value._last_usage = {"input_tokens": 100, "output_tokens": 50, "model": "x", "provider": "y"}
        units, hints, is_noise, _ = await analyze_api_with_llm(
            {"url": "https://api.example.com/units?token=xyz", "body": {"data": {"units": []}}},
            {"property_name": "Test", "city": "X", "state": "Y", "pmc": "", "website": ""},
        )

    assert is_noise is False
    assert len(units) == 1
    assert hints is not None
    assert hints["api_url_pattern"] == "https://api.example.com/units?token=xyz", (
        "Drop 1 regression: api_url_pattern was lost when json_paths is empty."
    )
    assert hints["json_paths"] == {}
    assert hints["response_envelope"] == "data.units"


@pytest.mark.asyncio
async def test_units_with_neither_paths_nor_envelope_still_carry_url() -> None:
    """Edge: LLM gave only units (no paths, no envelope). URL still persists.

    Persistence layer drops this at save time (both empty → nothing learnable),
    but the producer side is honest about what was analysed.
    """
    from services.llm_extractor import analyze_api_with_llm

    async def fake_complete(self, system, prompt, max_tokens):
        return '{"has_unit_data": true, "units": [{"unit_id": "U1"}]}'

    with patch("llm.factory.get_text_provider") as gp:
        gp.return_value.complete = fake_complete.__get__(gp.return_value)
        gp.return_value._last_usage = {"input_tokens": 100, "output_tokens": 50, "model": "x", "provider": "y"}
        _, hints, is_noise, _ = await analyze_api_with_llm(
            {"url": "https://api.example.com/units", "body": {}},
            {"property_name": "Test", "city": "X", "state": "Y", "pmc": "", "website": ""},
        )

    assert is_noise is False
    assert hints is not None
    assert hints["api_url_pattern"] == "https://api.example.com/units"
    assert hints["json_paths"] == {}
    assert hints["response_envelope"] == ""


@pytest.mark.asyncio
async def test_noise_path_unaffected() -> None:
    """Noise classification still returns the noise hints dict unchanged.

    Producer fix is gated on the units-extracted path; noise path keeps
    its existing shape.
    """
    from services.llm_extractor import analyze_api_with_llm

    async def fake_complete(self, system, prompt, max_tokens):
        return '{"has_unit_data": false, "noise_reason": "chatbot_config"}'

    with patch("llm.factory.get_text_provider") as gp:
        gp.return_value.complete = fake_complete.__get__(gp.return_value)
        gp.return_value._last_usage = {"input_tokens": 100, "output_tokens": 50, "model": "x", "provider": "y"}
        units, hints, is_noise, _ = await analyze_api_with_llm(
            {"url": "https://api.example.com/chatbot", "body": {}},
            {"property_name": "Test", "city": "X", "state": "Y", "pmc": "", "website": ""},
        )

    assert is_noise is True
    assert units == []
    assert hints is not None
    assert hints.get("noise_reason") == "chatbot_config"
    # Noise path does NOT promise api_url_pattern (it's a noise verdict, not a mapping).
    assert "api_url_pattern" not in hints
