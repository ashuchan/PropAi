"""Tests for pms/adapters/_merge_fns.py — pure unit-merge helpers (PR-2)."""

from __future__ import annotations

import pytest

from ma_poc.pms.adapters._merge_fns import (
    AMBIGUITY_RANKS,
    MERGE_MUTABLE_FIELDS,
    MERGE_PHYSICAL_FIELDS,
    MERGE_UNION_FIELDS,
    RANK_LADDER,
    aggregate_quality,
    availability_count_aware_merge,
    find_unit_list,
    has_unit_signals,
    merge_field_present,
    merge_field_values,
    merge_float_or_none,
    merge_int_or_none,
    merge_into_result_units,
    merge_norm,
    merge_rank_signature,
    rank_matches,
)


# ── merge_norm ─────────────────────────────────────────────────────────────────

class TestMergeNorm:
    def test_lowercases_and_strips(self):
        assert merge_norm("  Studio BR  ") == "studio br"

    def test_none_returns_empty(self):
        assert merge_norm(None) == ""

    def test_int_input(self):
        assert merge_norm(42) == "42"


# ── merge_int_or_none ──────────────────────────────────────────────────────────

class TestMergeIntOrNone:
    def test_plain_int(self):
        assert merge_int_or_none(2) == 2

    def test_float_string(self):
        assert merge_int_or_none("1.9") == 1

    def test_comma_string(self):
        assert merge_int_or_none("1,200") == 1200

    def test_none_returns_none(self):
        assert merge_int_or_none(None) is None

    def test_empty_string_returns_none(self):
        assert merge_int_or_none("") is None

    def test_minus_one_sentinel_returns_none(self):
        assert merge_int_or_none(-1) is None

    def test_non_numeric_returns_none(self):
        assert merge_int_or_none("N/A") is None


# ── merge_float_or_none ────────────────────────────────────────────────────────

class TestMergeFloatOrNone:
    def test_plain_float(self):
        assert merge_float_or_none(1.5) == 1.5

    def test_float_string(self):
        assert merge_float_or_none("2.0") == 2.0

    def test_none_returns_none(self):
        assert merge_float_or_none(None) is None

    def test_empty_string_returns_none(self):
        assert merge_float_or_none("") is None

    def test_non_numeric_returns_none(self):
        assert merge_float_or_none("N/A") is None


# ── merge_field_present ────────────────────────────────────────────────────────

class TestMergeFieldPresent:
    def test_first_key_wins(self):
        assert merge_field_present({"a": "1", "b": "2"}, "a", "b") == "1"

    def test_skips_none(self):
        assert merge_field_present({"a": None, "b": "x"}, "a", "b") == "x"

    def test_skips_empty_string(self):
        assert merge_field_present({"a": "", "b": "x"}, "a", "b") == "x"

    def test_skips_minus_one(self):
        assert merge_field_present({"a": -1, "b": 5}, "a", "b") == 5

    def test_returns_none_when_nothing_present(self):
        assert merge_field_present({"a": None}, "a", "b") is None


# ── merge_rank_signature ───────────────────────────────────────────────────────

class TestMergeRankSignature:
    def _full_unit(self) -> dict:
        return {
            "floor_plan_id": "FP1",
            "unit_id": "101",
            "floor_plan_name": "1BR Studio",
            "beds": 1,
            "baths": 1.0,
            "sqft": 750,
        }

    def test_full_unit_signature(self):
        sig = merge_rank_signature(self._full_unit())
        assert sig["fp_id"] == "FP1"
        assert sig["uid"] == "101"
        assert sig["fp_name"] == "1br studio"
        assert sig["beds"] == 1
        assert sig["baths"] == 1.0
        assert sig["sqft"] == 750

    def test_empty_unit_has_empty_strings(self):
        sig = merge_rank_signature({})
        assert sig["fp_id"] == ""
        assert sig["uid"] == ""
        assert sig["fp_name"] == ""
        assert sig["beds"] is None
        assert sig["baths"] is None
        assert sig["sqft"] is None

    def test_fallback_keys_for_uid(self):
        sig = merge_rank_signature({"unit_number": "202"})
        assert sig["uid"] == "202"

    def test_fp_name_normalized_to_lowercase(self):
        sig = merge_rank_signature({"floor_plan_name": "2BR/2BA DELUXE"})
        assert sig["fp_name"] == "2br/2ba deluxe"


# ── rank_matches ───────────────────────────────────────────────────────────────

class TestRankMatches:
    def _sig(self, **kw) -> dict:
        base = {"fp_id": "", "uid": "", "fp_name": "", "beds": None, "baths": None, "sqft": None}
        base.update(kw)
        return base

    def test_R0_matches_on_fp_id(self):
        ex = self._sig(fp_id="FP1")
        inc = self._sig(fp_id="FP1")
        assert rank_matches("R0", ex, inc) is True

    def test_R0_no_match_when_fp_id_empty(self):
        ex = self._sig(fp_id="")
        inc = self._sig(fp_id="")
        assert rank_matches("R0", ex, inc) is False

    def test_R0a_matches_on_uid(self):
        ex = self._sig(uid="101")
        inc = self._sig(uid="101")
        assert rank_matches("R0a", ex, inc) is True

    def test_R1a_requires_all_fields(self):
        ex = self._sig(fp_name="1br", beds=1, baths=1.0, sqft=750)
        inc = self._sig(fp_name="1br", beds=1, baths=1.0, sqft=750)
        assert rank_matches("R1a", ex, inc) is True

    def test_R1a_fails_when_sqft_differs(self):
        ex = self._sig(fp_name="1br", beds=1, baths=1.0, sqft=750)
        inc = self._sig(fp_name="1br", beds=1, baths=1.0, sqft=800)
        assert rank_matches("R1a", ex, inc) is False

    def test_R1b_matches_beds_baths_sqft_no_name(self):
        ex = self._sig(beds=1, baths=1.0, sqft=750)
        inc = self._sig(beds=1, baths=1.0, sqft=750)
        assert rank_matches("R1b", ex, inc) is True

    def test_R1c_matches_beds_baths_name_no_sqft(self):
        ex = self._sig(beds=1, baths=1.0, fp_name="a1")
        inc = self._sig(beds=1, baths=1.0, fp_name="a1")
        assert rank_matches("R1c", ex, inc) is True

    def test_R1d_matches_beds_baths_no_sqft_no_name(self):
        ex = self._sig(beds=2, baths=2.0)
        inc = self._sig(beds=2, baths=2.0)
        assert rank_matches("R1d", ex, inc) is True

    def test_R1e_matches_beds_with_missing_baths_sqft(self):
        ex = self._sig(beds=1)
        inc = self._sig(beds=1)
        assert rank_matches("R1e", ex, inc) is True

    def test_R1f_matches_beds_name_missing_baths_sqft(self):
        ex = self._sig(beds=1, fp_name="studio")
        inc = self._sig(beds=1, fp_name="studio")
        assert rank_matches("R1f", ex, inc) is True

    def test_unknown_rank_returns_false(self):
        ex = self._sig(uid="x")
        inc = self._sig(uid="x")
        assert rank_matches("RZZZ", ex, inc) is False


# ── merge_field_values ─────────────────────────────────────────────────────────

class TestMergeFieldValues:
    def test_mutable_field_latest_wins(self):
        target = {"market_rent_low": 1200.0}
        source = {"market_rent_low": 1300.0}
        merge_field_values(target, source, rank="R0", conflict_log=[])
        assert target["market_rent_low"] == 1300.0

    def test_physical_field_existing_wins(self):
        target = {"floor_plan_name": "1BR"}
        source = {"floor_plan_name": "2BR"}
        log: list = []
        merge_field_values(target, source, rank="R0a", conflict_log=log)
        assert target["floor_plan_name"] == "1BR"
        assert len(log) == 1
        assert log[0]["field"] == "floor_plan_name"

    def test_fill_when_missing(self):
        target = {}
        source = {"some_custom_field": "value"}
        merge_field_values(target, source, rank="R0", conflict_log=[])
        assert target["some_custom_field"] == "value"

    def test_skips_none_values(self):
        target = {"x": "existing"}
        source = {"x": None}
        merge_field_values(target, source, rank="R0", conflict_log=[])
        assert target["x"] == "existing"

    def test_skips_underscore_keys(self):
        target = {}
        source = {"_provenance": "tier1"}
        merge_field_values(target, source, rank="R0", conflict_log=[])
        assert "_provenance" not in target

    def test_union_field_deduplicates(self):
        target = {"amenities": ["Pool"]}
        source = {"amenities": ["Pool", "Gym"]}
        merge_field_values(target, source, rank="R0", conflict_log=[])
        assert target["amenities"] == ["Pool", "Gym"]

    def test_union_field_preserves_order(self):
        target = {"amenities": ["A", "B"]}
        source = {"amenities": ["B", "C"]}
        merge_field_values(target, source, rank="R0", conflict_log=[])
        assert target["amenities"] == ["A", "B", "C"]


# ── availability_count_aware_merge ─────────────────────────────────────────────

class TestAvailabilityCountAwareMerge:
    def test_sums_at_R0(self):
        target = {"availability_count": 3}
        source = {"availability_count": 2}
        availability_count_aware_merge(target, source, "R0")
        assert target["availability_count"] == 5

    def test_latest_wins_below_R1a(self):
        target = {"availability_count": 10}
        source = {"availability_count": 2}
        availability_count_aware_merge(target, source, "R1b")
        assert target["availability_count"] == 2

    def test_no_count_in_source_is_noop(self):
        target = {"availability_count": 5}
        availability_count_aware_merge(target, {}, "R0")
        assert target["availability_count"] == 5

    def test_none_existing_treated_as_zero(self):
        target = {}
        availability_count_aware_merge(target, {"availability_count": 3}, "R0a")
        assert target["availability_count"] == 3


# ── merge_into_result_units ────────────────────────────────────────────────────

class TestMergeIntoResultUnits:
    def _unit(self, **kw) -> dict:
        base = {"unit_id": "101", "floor_plan_name": "1BR", "market_rent_low": 1500.0}
        base.update(kw)
        return base

    def test_empty_existing_returns_incoming(self):
        inc = [self._unit()]
        result = merge_into_result_units([], inc)
        assert result == inc

    def test_empty_incoming_returns_existing(self):
        ex = [self._unit()]
        result = merge_into_result_units(ex, [])
        assert result == ex

    def test_R0_merge_by_floor_plan_id(self):
        ex = [{"floor_plan_id": "FP1", "floor_plan_name": "1BR", "market_rent_low": 1400.0}]
        inc = [{"floor_plan_id": "FP1", "market_rent_low": 1500.0}]
        result = merge_into_result_units(ex, inc)
        assert len(result) == 1
        assert result[0]["market_rent_low"] == 1500.0

    def test_R0a_merge_by_unit_id(self):
        ex = [{"unit_id": "101", "market_rent_low": 1400.0}]
        inc = [{"unit_id": "101", "market_rent_low": 1550.0, "beds": 1}]
        result = merge_into_result_units(ex, inc)
        assert len(result) == 1
        assert result[0]["market_rent_low"] == 1550.0
        assert result[0]["beds"] == 1

    def test_no_match_appends_as_new_record(self):
        ex = [{"unit_id": "101", "market_rent_low": 1400.0}]
        inc = [{"unit_id": "202", "market_rent_low": 1600.0}]
        result = merge_into_result_units(ex, inc)
        assert len(result) == 2

    def test_H8_ambiguous_fails_closed(self):
        # Two existing units with same beds/baths/sqft — incoming is ambiguous.
        ex = [
            {"beds": 1, "baths": 1.0, "sqft": 750, "market_rent_low": 1300.0},
            {"beds": 1, "baths": 1.0, "sqft": 750, "market_rent_low": 1350.0},
        ]
        inc = [{"beds": 1, "baths": 1.0, "sqft": 750, "market_rent_low": 1400.0}]
        result = merge_into_result_units(ex, inc)
        # Fails closed → appended, not merged
        assert len(result) == 3

    def test_physical_conflict_does_not_overwrite(self):
        ex = [{"unit_id": "101", "floor_plan_name": "1BR"}]
        inc = [{"unit_id": "101", "floor_plan_name": "2BR"}]
        result = merge_into_result_units(ex, inc)
        assert result[0]["floor_plan_name"] == "1BR"


# ── aggregate_quality ──────────────────────────────────────────────────────────

class TestAggregateQuality:
    class _Mapping:
        def __init__(self, qs: float):
            self.quality_score = qs

    def test_empty_returns_one(self):
        assert aggregate_quality([]) == 1.0

    def test_single_mapping(self):
        assert aggregate_quality([self._Mapping(0.8)]) == 0.8

    def test_returns_min(self):
        ms = [self._Mapping(0.9), self._Mapping(0.6), self._Mapping(0.75)]
        assert aggregate_quality(ms) == 0.6

    def test_none_quality_treated_as_one(self):
        class _M:
            quality_score = None
        assert aggregate_quality([_M()]) == 1.0


# ── find_unit_list ─────────────────────────────────────────────────────────────

class TestFindUnitList:
    def test_direct_list_of_dicts(self):
        body = [{"id": 1}, {"id": 2}]
        assert find_unit_list(body) == body

    def test_dict_with_units_key(self):
        body = {"units": [{"id": 1}]}
        assert find_unit_list(body) == [{"id": 1}]

    def test_dict_with_floor_plans_key(self):
        body = {"floorPlans": [{"id": "FP1"}]}
        assert find_unit_list(body) == [{"id": "FP1"}]

    def test_nested_data_envelope(self):
        body = {"data": {"units": [{"id": 1}]}}
        assert find_unit_list(body) == [{"id": 1}]

    def test_nested_response_envelope(self):
        body = {"response": {"floorplans": [{"id": "FP1"}]}}
        assert find_unit_list(body) == [{"id": "FP1"}]

    def test_response_is_list_directly(self):
        body = {"response": [{"id": 1}]}
        assert find_unit_list(body) == [{"id": 1}]

    def test_empty_list_returns_empty(self):
        assert find_unit_list([]) == []

    def test_scalar_returns_empty(self):
        assert find_unit_list("string") == []

    def test_none_returns_empty(self):
        assert find_unit_list(None) == []

    def test_dict_with_no_matching_key_returns_empty(self):
        assert find_unit_list({"color": "blue"}) == []


# ── has_unit_signals ───────────────────────────────────────────────────────────

class TestHasUnitSignals:
    def test_empty_list_returns_false(self):
        assert has_unit_signals([]) is False

    def test_two_signal_keys_returns_true(self):
        items = [{"rent": 1500, "sqft": 750}]
        assert has_unit_signals(items) is True

    def test_one_signal_key_returns_false(self):
        items = [{"rent": 1500, "color": "blue"}]
        assert has_unit_signals(items) is False

    def test_noise_keys_return_false(self):
        items = [{"id": 1, "name": "property"}]
        assert has_unit_signals(items) is False

    def test_unit_number_plus_rent_returns_true(self):
        items = [{"unitNumber": "101", "minRent": 1200}]
        assert has_unit_signals(items) is True


# ── Constants ──────────────────────────────────────────────────────────────────

class TestConstants:
    def test_rank_ladder_order(self):
        assert RANK_LADDER[0] == "R0"
        assert RANK_LADDER[1] == "R0a"
        assert RANK_LADDER[-1] == "R1f"

    def test_ambiguity_ranks_excludes_strong_identity(self):
        assert "R0" not in AMBIGUITY_RANKS
        assert "R0a" not in AMBIGUITY_RANKS
        assert "R1a" in AMBIGUITY_RANKS

    def test_mutable_contains_rent_fields(self):
        assert "market_rent_low" in MERGE_MUTABLE_FIELDS
        assert "available_date" in MERGE_MUTABLE_FIELDS

    def test_physical_contains_identity_fields(self):
        assert "unit_id" in MERGE_PHYSICAL_FIELDS
        assert "floor_plan_name" in MERGE_PHYSICAL_FIELDS

    def test_union_contains_amenities(self):
        assert "amenities" in MERGE_UNION_FIELDS
