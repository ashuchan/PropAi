"""Parity tests for first-class area/rent ranges and source diagnostics."""

from __future__ import annotations

from datetime import UTC, datetime

from ma_poc.core.schema_v2 import (
    _format_v2_unit as core_format_unit,
)
from ma_poc.core.schema_v2 import (
    collapse_numeric_unit_id_aliases,
)
from ma_poc.scripts.runners.jugnu import (
    _emit_v2_units_for_property,
)
from ma_poc.scripts.runners.jugnu import (
    _format_v2_unit as jugnu_format_unit,
)


def _format_both(row: dict[str, object]) -> list[dict[str, object]]:
    """Format one adapter row through both output forks."""

    captured = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
    return [
        core_format_unit(row, captured, "123"),
        jugnu_format_unit(row, captured, "123"),
    ]


def test_range_area_is_published_but_scalar_stays_null() -> None:
    """A published range must not be represented by a fake midpoint."""

    row = {
        "unit_number": "A101",
        "bedrooms": "1",
        "bathrooms": "1",
        "market_rent_low": 1850,
        "market_rent_high": 2075,
        "rent_range": "$1,850 - $2,075",
        "area_low": 675,
        "area_high": 850,
        "area_range": "675-850",
        "area_provenance": "published_plan_family_range_no_midpoint",
        "area_source_url": "https://example.test/floorplans",
        "source_response_sha256": "a" * 64,
        "source_response_url": "https://example.test/floorplans",
        "source_record_locator": "caption:1;caption:2;caption:3",
        "identity_quality": "property_scoped_source_id",
    }

    for formatted in _format_both(row):
        assert formatted["area"] == -1
        assert formatted["area_sqft"] is None
        assert formatted["area_low"] == 675
        assert formatted["area_high"] == 850
        assert formatted["area_range"] == "675-850"
        assert formatted["area_value_type"] == "range"
        assert formatted["area_is_published"] is True
        assert formatted["area_absence"] is None
        assert formatted["area_absence_evidence"] is None
        assert formatted["rent_low"] == 1850
        assert formatted["rent_high"] == 2075
        assert formatted["rent_range"] == "$1,850 - $2,075"
        assert formatted["rent_range_raw"] == "$1,850 - $2,075"
        assert formatted["rent_is_range"] is True
        assert formatted["rent_provenance"] == ("numeric_fields_confirmed_by_published_range")
        assert formatted["source_response_sha256"] == "a" * 64
        assert formatted["source_record_locator"] == "caption:1;caption:2;caption:3"


def test_scalar_area_and_scalar_rent_emit_equal_bounds() -> None:
    """Exact values retain the same interval contract with equal endpoints."""

    row = {
        "unit_number": "B202",
        "bedrooms": "2",
        "bathrooms": "2",
        "sqft": "1125",
        "market_rent_low": 2500,
        "market_rent_high": 2500,
    }

    for formatted in _format_both(row):
        assert formatted["area"] == 1125
        assert formatted["area_sqft"] == 1125
        assert formatted["area_low"] == 1125
        assert formatted["area_high"] == 1125
        assert formatted["area_range"] == "1125"
        assert formatted["area_value_type"] == "exact"
        assert formatted["rent_range"] == "$2,500"
        assert formatted["rent_is_range"] is False
        assert formatted["rent_provenance"] == "numeric_fields"


def test_published_unit_rent_range_repairs_collapsed_numeric_scalar() -> None:
    """A physical unit keeps its authored interval instead of one endpoint."""

    row = {
        "unit_number": "C303",
        "market_rent_low": 1850,
        "market_rent_high": 1850,
        "rent_range": "$1,850 - $2,075",
    }

    for formatted in _format_both(row):
        assert formatted["rent_low"] == 1850
        assert formatted["rent_high"] == 2075
        assert formatted["rent_range"] == "$1,850 - $2,075"
        assert formatted["rent_range_raw"] == "$1,850 - $2,075"
        assert formatted["rent_is_range"] is True
        assert formatted["rent_provenance"] == ("published_range_reconciled_with_numeric")


def test_published_unit_rent_range_without_numeric_companions_survives() -> None:
    """Direct adapter rows that expose only formatted text keep both bounds."""

    row = {
        "unit_number": "C304",
        "rent_range": "$1,850 - $2,075",
    }

    for formatted in _format_both(row):
        assert formatted["rent_low"] == 1850
        assert formatted["rent_high"] == 2075
        assert formatted["rent_range"] == "$1,850 - $2,075"
        assert formatted["rent_is_range"] is True
        assert formatted["rent_provenance"] == "published_range_only"


def test_conflicting_rent_range_is_retained_raw_without_widening_numeric() -> None:
    """A disagreement stays visible and does not silently alter the scalar."""

    row = {
        "unit_number": "D404",
        "market_rent_low": 2200,
        "market_rent_high": 2200,
        "rent_range": "$1,850 - $2,075",
    }

    for formatted in _format_both(row):
        assert formatted["rent_low"] == 2200
        assert formatted["rent_high"] == 2200
        assert formatted["rent_range"] == "$2,200"
        assert formatted["rent_range_raw"] == "$1,850 - $2,075"
        assert formatted["rent_is_range"] is False
        assert formatted["rent_provenance"] == ("numeric_fields_conflict_with_published_range")


def test_reversed_numeric_rent_bounds_are_normalized() -> None:
    """Low/high ordering is canonical even when an adapter swaps fields."""

    row = {
        "unit_number": "E505",
        "market_rent_low": 2600,
        "market_rent_high": 2300,
    }

    for formatted in _format_both(row):
        assert formatted["rent_low"] == 2300
        assert formatted["rent_high"] == 2600
        assert formatted["rent_range"] == "$2,300 - $2,600"
        assert formatted["rent_is_range"] is True


def test_identical_leading_zero_aliases_collapse_with_lineage() -> None:
    """The Waterline 321/0321 shape keeps one row and both spellings."""

    common = {
        "beds": 1,
        "baths": 1.0,
        "floor_plan_name": "A1",
        "floor_plan_id": "fp-1",
        "area": 775,
        "area_low": 775,
        "area_high": 775,
        "rent_low": 3125,
        "rent_high": 3125,
        "available_date": "2026-08-15",
        "availability_status": "AVAILABLE",
        "building": None,
        "building_id": None,
        "is_floor_plan_level": False,
    }
    rows = [
        {
            **common,
            "unit_id": "321",
            "source_ids": {"api_unit_id": "u321"},
            "source_response_sha256": "a" * 64,
        },
        {
            **common,
            "unit_id": "0321",
            "source_ids": {"authored_unit_number": "0321"},
            "source_response_sha256": "b" * 64,
        },
    ]

    assert collapse_numeric_unit_id_aliases(rows) == 1
    assert len(rows) == 1
    assert rows[0]["unit_id"] == "0321"
    assert rows[0]["unit_id_aliases"] == ["321", "0321"]
    assert rows[0]["identity_quality"] == "verified_leading_zero_alias"
    assert {entry["unit_id"] for entry in rows[0]["unit_id_alias_sources"]} == {
        "321",
        "0321",
    }


def test_alias_collapse_rejects_rent_conflict_and_alphanumeric_ids() -> None:
    """Text similarity alone never merges conflicting or nonnumeric ids."""

    rows = [
        {"unit_id": "321", "rent_low": 3000},
        {"unit_id": "0321", "rent_low": 3100},
        {"unit_id": "A321", "rent_low": 3000},
        {"unit_id": "A0321", "rent_low": 3000},
    ]
    assert collapse_numeric_unit_id_aliases(rows) == 0
    assert len(rows) == 4


def test_jugnu_post_format_pipeline_applies_numeric_alias_rule() -> None:
    """Production's post-format dedup invokes the same conservative rule."""

    rows = [
        {"unit_id": "321", "rent_low": 3000, "rent_high": 3000, "area": 700},
        {"unit_id": "0321", "rent_low": 3000, "rent_high": 3000, "area": 700},
    ]
    emitted = _emit_v2_units_for_property(rows)
    assert [row["unit_id"] for row in emitted] == ["0321"]
    assert emitted[0]["unit_id_aliases"] == ["321", "0321"]
