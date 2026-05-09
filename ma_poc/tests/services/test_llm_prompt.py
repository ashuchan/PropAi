"""Tests for services/llm_prompt.py — pure LLM helper functions."""

from __future__ import annotations

import json

import pytest

from ma_poc.services.llm_prompt import (
    UNIT_SIGNAL_KEYS,
    normalize_units,
    parse_llm_response,
    rank_api_responses,
)


# ── rank_api_responses ─────────────────────────────────────────────────────────

class TestRankApiResponses:
    def test_empty_returns_empty(self):
        assert rank_api_responses([]) == []

    def test_returns_at_most_three(self):
        responses = [
            {"url": f"https://api.example.com/{i}", "body": {"rent": i * 100, "unit": i}}
            for i in range(1, 7)
        ]
        result = rank_api_responses(responses)
        assert len(result) <= 3

    def test_high_signal_ranked_first(self):
        low = {"url": "low", "body": {"color": "blue"}}
        high = {"url": "high", "body": {"rent": 1500, "unit_number": "101", "sqft": 750, "bedrooms": 1}}
        result = rank_api_responses([low, high])
        assert result[0] is high

    def test_zero_signal_responses_excluded(self):
        responses = [{"url": "noise", "body": {"color": "blue", "id": 1}}]
        assert rank_api_responses(responses) == []

    def test_list_body_scored(self):
        responses = [{"url": "u", "body": [{"rent": 1200, "unit": "101"}]}]
        result = rank_api_responses(responses)
        assert len(result) == 1

    def test_none_body_excluded(self):
        responses = [{"url": "u", "body": None}]
        assert rank_api_responses(responses) == []

    def test_preserves_original_dicts(self):
        resp = {"url": "u", "body": {"rent": 1500}}
        result = rank_api_responses([resp])
        assert result[0] is resp


# ── parse_llm_response ─────────────────────────────────────────────────────────

class TestParseLlmResponse:
    def test_valid_json(self):
        payload = {"units": [{"unit_id": "101"}], "profile_hints": {}}
        assert parse_llm_response(json.dumps(payload)) == payload

    def test_markdown_json_fence(self):
        text = '```json\n{"units": [], "profile_hints": {}}\n```'
        result = parse_llm_response(text)
        assert result == {"units": [], "profile_hints": {}}

    def test_markdown_fence_no_language(self):
        text = '```\n{"units": [], "profile_hints": {}}\n```'
        result = parse_llm_response(text)
        assert result == {"units": [], "profile_hints": {}}

    def test_json_embedded_in_prose(self):
        text = 'Here is the result: {"units": [{"unit_id": "A1"}], "profile_hints": {}}'
        result = parse_llm_response(text)
        assert result["units"][0]["unit_id"] == "A1"

    def test_unparseable_returns_fallback(self):
        result = parse_llm_response("not json at all!")
        assert result == {"units": [], "profile_hints": {}}

    def test_empty_string_returns_fallback(self):
        result = parse_llm_response("")
        assert result == {"units": [], "profile_hints": {}}

    def test_partial_fence_no_crash(self):
        result = parse_llm_response("```json\nnot valid json")
        assert isinstance(result, dict)


# ── normalize_units ────────────────────────────────────────────────────────────

class TestNormalizeUnits:
    def _basic(self) -> dict:
        return {
            "unit_id": "101",
            "floor_plan_name": "1BR",
            "bedrooms": 1,
            "bathrooms": 1.0,
            "sqft": 750,
            "market_rent_low": 1500,
            "market_rent_high": 1600,
            "available_date": "2026-06-01",
            "availability_status": "AVAILABLE",
            "confidence": 0.9,
        }

    def test_full_unit_passes_through(self):
        units = normalize_units([self._basic()])
        assert len(units) == 1
        u = units[0]
        assert u["unit_id"] == "101"
        assert u["floor_plan_name"] == "1BR"
        assert u["bedrooms"] == 1
        assert u["bathrooms"] == 1.0
        assert u["sqft"] == 750
        assert u["market_rent_low"] == 1500.0
        assert u["market_rent_high"] == 1600.0
        assert u["available_date"] == "2026-06-01"
        assert u["availability_status"] == "AVAILABLE"
        assert u["confidence"] == 0.9

    def test_beds_coerced_from_string(self):
        u = normalize_units([{**self._basic(), "bedrooms": "2"}])
        assert u[0]["bedrooms"] == 2

    def test_beds_coerced_from_float(self):
        u = normalize_units([{**self._basic(), "bedrooms": 1.9}])
        assert u[0]["bedrooms"] == 1

    def test_invalid_beds_becomes_none(self):
        u = normalize_units([{**self._basic(), "bedrooms": "N/A"}])
        assert u[0]["bedrooms"] is None

    def test_sqft_coerced_from_float_string(self):
        u = normalize_units([{**self._basic(), "sqft": "850.0"}])
        assert u[0]["sqft"] == 850

    def test_rent_below_sanity_min_nulled(self):
        u = normalize_units([{**self._basic(), "market_rent_low": 100, "market_rent_high": 150}])
        assert u[0]["market_rent_low"] is None
        assert u[0]["market_rent_high"] is None

    def test_rent_above_sanity_max_nulled(self):
        u = normalize_units([{**self._basic(), "market_rent_low": 100_000}])
        assert u[0]["market_rent_low"] is None

    def test_asking_rent_fallback(self):
        raw = {"unit_id": "A1", "asking_rent": 1300}
        u = normalize_units([raw])
        assert u[0]["market_rent_low"] == 1300.0

    def test_rent_high_defaults_to_low(self):
        raw = {"unit_id": "A1", "market_rent_low": 1500}
        u = normalize_units([raw])
        assert u[0]["market_rent_high"] == 1500.0

    def test_unknown_status_normalised(self):
        u = normalize_units([{**self._basic(), "availability_status": "pending"}])
        assert u[0]["availability_status"] == "UNKNOWN"

    def test_status_uppercased(self):
        u = normalize_units([{**self._basic(), "availability_status": "available"}])
        assert u[0]["availability_status"] == "AVAILABLE"

    def test_confidence_clamped_to_zero_one(self):
        u = normalize_units([{**self._basic(), "confidence": 1.5}])
        assert u[0]["confidence"] == 1.0
        u2 = normalize_units([{**self._basic(), "confidence": -0.5}])
        assert u2[0]["confidence"] == 0.0

    def test_invalid_confidence_defaults_to_half(self):
        u = normalize_units([{**self._basic(), "confidence": "unknown"}])
        assert u[0]["confidence"] == 0.5

    def test_no_signal_unit_excluded(self):
        raw = {"bedrooms": 1, "sqft": 750}  # no unit_id, floor_plan, or rent
        assert normalize_units([raw]) == []

    def test_floor_plan_only_included(self):
        raw = {"floor_plan_name": "Studio"}
        u = normalize_units([raw])
        assert len(u) == 1

    def test_unit_number_fallback_for_unit_id(self):
        raw = {"unit_number": "202", "floor_plan_name": "2BR", "market_rent_low": 2000}
        u = normalize_units([raw])
        assert u[0]["unit_id"] == "202"

    def test_floor_plan_type_fallback(self):
        raw = {"unit_id": "A1", "floor_plan_type": "Loft"}
        u = normalize_units([raw])
        assert u[0]["floor_plan_name"] == "Loft"

    def test_empty_list_returns_empty(self):
        assert normalize_units([]) == []

    def test_multiple_units(self):
        raws = [self._basic(), {**self._basic(), "unit_id": "102", "market_rent_low": 1600}]
        u = normalize_units(raws)
        assert len(u) == 2

    def test_default_confidence_when_missing(self):
        raw = {"unit_id": "A1"}
        u = normalize_units([raw])
        assert u[0]["confidence"] == 0.7


# ── UNIT_SIGNAL_KEYS constant ──────────────────────────────────────────────────

class TestUnitSignalKeys:
    def test_is_frozenset(self):
        assert isinstance(UNIT_SIGNAL_KEYS, frozenset)

    def test_core_keys_present(self):
        for key in ("rent", "price", "sqft", "unit", "bed", "bath", "available"):
            assert key in UNIT_SIGNAL_KEYS

    def test_camel_case_variants_present(self):
        assert "unitNumber" in UNIT_SIGNAL_KEYS
        assert "floorPlan" in UNIT_SIGNAL_KEYS
        assert "availableDate" in UNIT_SIGNAL_KEYS
