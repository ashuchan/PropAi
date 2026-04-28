"""Tests for extraction_quality.py — quality gates and unit-merge dedupe."""

from __future__ import annotations

from ma_poc.services.extraction_quality import (
    QualityVerdict,
    aggregate_quality,
    has_important_fields,
    meets_minimum_fields,
    merge_units,
)


def test_minimum_met_with_canonical_v2_keys() -> None:
    assert meets_minimum_fields({"beds": 2, "baths": 1, "area": 950}) is True


def test_minimum_met_with_legacy_v1_aliases() -> None:
    assert meets_minimum_fields({"bedrooms": 1, "bathrooms": 1.0, "sqft": 700}) is True


def test_minimum_met_mixed_alias_shapes() -> None:
    assert meets_minimum_fields({"beds": 3, "bathrooms": 2, "sqft": 1100}) is True


def test_minimum_fails_when_baths_missing() -> None:
    assert meets_minimum_fields({"beds": 2, "area": 800}) is False


def test_minimum_fails_when_sqft_is_minus_one_sentinel() -> None:
    # -1 is the sentinel used by jugnu_runner._format_area when sqft is absent;
    # quality gate must treat it as missing.
    assert meets_minimum_fields({"beds": 1, "baths": 1, "area": -1}) is False


def test_minimum_fails_on_empty_string_fields() -> None:
    assert meets_minimum_fields({"beds": "", "baths": 1, "sqft": 700}) is False


def test_minimum_fails_on_empty_unit() -> None:
    assert meets_minimum_fields({}) is False


def test_important_met_with_rent_low_and_available_date() -> None:
    assert has_important_fields({"rent_low": 1500, "available_date": "2026-05-01"}) is True


def test_important_met_with_legacy_keys() -> None:
    assert has_important_fields({"market_rent_low": 1700, "availability_date": "2026-06-01"}) is True


def test_important_fails_when_only_rent_present() -> None:
    assert has_important_fields({"rent_low": 1500}) is False


def test_important_fails_when_only_date_present() -> None:
    assert has_important_fields({"available_date": "2026-05-01"}) is False


def test_important_fails_on_empty_unit() -> None:
    assert has_important_fields({}) is False


def test_aggregate_quality_empty_fails_both() -> None:
    v = aggregate_quality([])
    assert isinstance(v, QualityVerdict)
    assert v.minimum_met is False
    assert v.important_met is False
    assert v.confidence == 0.0
    assert v.unit_count == 0


def test_aggregate_quality_all_pass_default_threshold() -> None:
    units = [
        {"beds": 1, "baths": 1, "sqft": 700, "rent_low": 1500, "available_date": "2026-05-01"},
        {"beds": 2, "baths": 2, "sqft": 950, "rent_low": 1900, "available_date": "2026-06-01"},
    ]
    v = aggregate_quality(units)
    assert v.minimum_met is True
    assert v.important_met is True
    assert v.minimum_pass_rate == 1.0
    assert v.important_pass_rate == 1.0
    assert v.unit_count == 2


def test_aggregate_quality_majority_below_threshold_fails_minimum() -> None:
    # Default threshold = 0.7 — 1/3 passing the minimum gate is below.
    units = [
        {"beds": 1, "baths": 1, "sqft": 700},
        {"beds": 2},
        {"beds": 3},
    ]
    v = aggregate_quality(units)
    assert v.minimum_met is False
    assert v.minimum_pass_rate < 0.7


def test_aggregate_quality_threshold_overridable() -> None:
    units = [
        {"beds": 1, "baths": 1, "sqft": 700},
        {"beds": 2},
    ]
    assert aggregate_quality(units, threshold=0.7).minimum_met is False
    assert aggregate_quality(units, threshold=0.5).minimum_met is True


def test_aggregate_quality_confidence_averages_per_unit_scores() -> None:
    units = [
        {"beds": 1, "baths": 1, "sqft": 700, "_confidence": 0.9},
        {"beds": 2, "baths": 2, "sqft": 900, "_confidence": 0.5},
    ]
    v = aggregate_quality(units)
    assert v.confidence == 0.7


def test_aggregate_quality_confidence_zero_when_none_reported() -> None:
    units = [{"beds": 1, "baths": 1, "sqft": 700}]
    assert aggregate_quality(units).confidence == 0.0


def test_aggregate_quality_minimum_can_pass_while_important_fails() -> None:
    # Common scenario: API gave us beds/baths/sqft but no rent or date —
    # this is exactly when the explorer should re-rank for availability.
    units = [
        {"beds": 1, "baths": 1, "sqft": 700},
        {"beds": 2, "baths": 2, "sqft": 950},
    ]
    v = aggregate_quality(units)
    assert v.minimum_met is True
    assert v.important_met is False


def test_merge_units_collapses_on_unit_id() -> None:
    a = [{"unit_id": "101", "beds": 1, "baths": 1, "sqft": 700, "rent_low": 1500}]
    b = [{"unit_id": "101", "beds": 1, "baths": 1, "sqft": 700, "available_date": "2026-05-01"}]
    merged = merge_units(a, b)
    assert len(merged) == 1
    assert merged[0]["rent_low"] == 1500
    assert merged[0]["available_date"] == "2026-05-01"


def test_merge_units_collapses_on_floor_plan_fingerprint_when_no_unit_id() -> None:
    a = [{"floor_plan_name": "A1", "beds": 1, "sqft": 700, "rent_low": 1500}]
    b = [{"floor_plan_name": "A1", "beds": 1, "sqft": 700, "rent_low": 1500, "available_date": "2026-05-01"}]
    merged = merge_units(a, b)
    assert len(merged) == 1
    assert merged[0]["available_date"] == "2026-05-01"


def test_merge_units_appends_when_keys_differ() -> None:
    a = [{"unit_id": "101", "beds": 1}]
    b = [{"unit_id": "102", "beds": 2}]
    merged = merge_units(a, b)
    assert len(merged) == 2


def test_merge_units_richer_record_wins_collision() -> None:
    sparse = {"unit_id": "101", "beds": 1}
    rich = {
        "unit_id": "101",
        "beds": 1,
        "baths": 1,
        "sqft": 700,
        "rent_low": 1500,
        "available_date": "2026-05-01",
    }
    merged = merge_units([sparse], [rich])
    assert merged[0]["sqft"] == 700
    assert merged[0]["rent_low"] == 1500


def test_merge_units_does_not_overwrite_present_with_absent() -> None:
    winner = {"unit_id": "101", "beds": 1, "baths": 1, "sqft": 700, "rent_low": 1500}
    loser = {"unit_id": "101", "beds": 1, "baths": 1, "sqft": 700, "rent_low": None}
    merged = merge_units([winner], [loser])
    assert merged[0]["rent_low"] == 1500


def test_merge_units_confidence_breaks_substantive_tie() -> None:
    a = {"unit_id": "101", "beds": 1, "baths": 1, "sqft": 700, "_confidence": 0.6, "source": "a"}
    b = {"unit_id": "101", "beds": 1, "baths": 1, "sqft": 700, "_confidence": 0.9, "source": "b"}
    merged = merge_units([a], [b])
    assert merged[0]["source"] == "b"


def test_merge_units_existing_wins_total_tie() -> None:
    a = {"unit_id": "101", "beds": 1, "baths": 1, "sqft": 700, "tag": "first"}
    b = {"unit_id": "101", "beds": 1, "baths": 1, "sqft": 700, "tag": "second"}
    merged = merge_units([a], [b])
    assert merged[0]["tag"] == "first"


def test_merge_units_three_way_idempotent_on_repeat() -> None:
    base = [{"unit_id": "101", "beds": 1, "baths": 1, "sqft": 700}]
    add = [{"unit_id": "102", "beds": 2, "baths": 2, "sqft": 950}]
    once = merge_units(base, add)
    twice = merge_units(once, add)
    assert len(once) == len(twice) == 2


def test_merge_units_legacy_alias_keys_collapse_with_canonical() -> None:
    a = [{"unit_id": "101", "bedrooms": 1, "bathrooms": 1, "sqft": 700}]
    b = [{"unit_id": "101", "beds": 1, "baths": 1, "area": 700}]
    merged = merge_units(a, b)
    assert len(merged) == 1
