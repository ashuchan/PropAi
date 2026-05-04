"""Phase 3 integration tests for the post-extraction CSV snap."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ma_poc.services import floorplan_catalog as fc
from ma_poc.services.floorplan_snap import snap_units


@pytest.fixture
def catalog(tmp_path: Path) -> fc.FloorplanCatalog:
    csv_path = tmp_path / "Floorplan- comparisons.csv"
    csv_path.write_text(
        "apartmentid,floorplannumber,description,bed,bath,area\n"
        # Two unique 1BR plans with different sqft.
        "P1,1,Aspen,1,1,750\n"
        "P1,2,Birch,1,1,900\n"
        "P1,3,Cedar,2,2,1100\n"
        # Property without sqft on file.
        "P2,1,Quail,0,1,-1\n",
        encoding="utf-8",
    )
    map_path = tmp_path / "csv_floorplan_mapping.json"
    map_path.write_text(json.dumps({"chosen_field": "apartmentid"}), encoding="utf-8")
    return fc.FloorplanCatalog(csv_path=csv_path, mapping_path=map_path)


def test_snap_overwrites_floor_plan_name_with_canonical(catalog) -> None:
    units = [
        {
            "unit_id": "101",
            "floor_plan_name": "1BR / 1BA #1",
            "beds": 1,
            "baths": 1.0,
            "sqft": 750,
        }
    ]
    out = snap_units(units, "P1", catalog)
    assert len(out) == 1
    assert out[0]["floor_plan_name"] == "Aspen"
    assert out[0]["floor_plan_id"] == "P1_1"
    assert out[0]["floor_plan_snap_reason"] == "exact_three_field"


def test_snap_preserves_extracted_name_in_audit_field(catalog) -> None:
    units = [
        {
            "unit_id": "101",
            "floor_plan_name": "1BR / 1BA #1",
            "beds": 1,
            "baths": 1.0,
            "sqft": 750,
        }
    ]
    out = snap_units(units, "P1", catalog)
    assert out[0]["floor_plan_name_extracted"] == "1BR / 1BA #1"


def test_csv_snap_runs_post_extraction(catalog) -> None:
    """H4: the snap mutates the *output* of extraction, not its input."""
    raw_units = [{"unit_id": "U", "beds": 2, "baths": 2.0, "sqft": 1100, "floor_plan_name": "X"}]
    pre = [dict(u) for u in raw_units]
    out = snap_units(raw_units, "P1", catalog)
    assert raw_units == pre  # input untouched
    assert out[0]["floor_plan_id"] == "P1_3"


def test_unsnapped_records_not_dropped(catalog) -> None:
    """H4: unsnapped records survive — they fall through to the merge cascade."""
    units = [
        # Beds match no plan in the catalog → no_match (record survives).
        {"unit_id": "X", "beds": 5, "baths": 3.0, "sqft": 9999, "floor_plan_name": "Mystery"},
        # Strict-sqft mismatch → no_match (H2).
        {"unit_id": "Y", "beds": 1, "baths": 1.0, "sqft": 753, "floor_plan_name": "Aspen-ish"},
    ]
    out = snap_units(units, "P1", catalog)
    assert len(out) == 2
    assert all("floor_plan_id" not in u for u in out)
    assert all(u["floor_plan_snap_reason"] == "no_match" for u in out)
    # Original names survive.
    assert out[0]["floor_plan_name"] == "Mystery"
    assert out[1]["floor_plan_name"] == "Aspen-ish"


def test_snap_does_not_run_when_csv_low_coverage(tmp_path: Path) -> None:
    csv_path = tmp_path / "Floorplan- comparisons.csv"
    csv_path.write_text(
        "apartmentid,floorplannumber,description,bed,bath,area\nP1,1,Aspen,1,1,750\n",
        encoding="utf-8",
    )
    map_path = tmp_path / "csv_floorplan_mapping.json"
    map_path.write_text(json.dumps({"chosen_field": None}), encoding="utf-8")
    bypass_cat = fc.FloorplanCatalog(csv_path=csv_path, mapping_path=map_path)

    units = [
        {"unit_id": "101", "beds": 1, "baths": 1.0, "sqft": 750, "floor_plan_name": "Original"}
    ]
    out = snap_units(units, "P1", bypass_cat)
    assert out[0]["floor_plan_name"] == "Original"
    assert "floor_plan_id" not in out[0]
    assert out[0]["floor_plan_snap_reason"] == "low_coverage_bypass"


def test_strict_sqft_no_tolerance_in_snap(catalog) -> None:
    """H2: 750 vs 753 must NOT snap together — even though only 3 sqft apart."""
    units = [
        {"unit_id": "Z", "beds": 1, "baths": 1.0, "sqft": 753, "floor_plan_name": "Aspen"}
    ]
    out = snap_units(units, "P1", catalog)
    assert out[0]["floor_plan_snap_reason"] == "no_match"
    assert "floor_plan_id" not in out[0]


def test_snap_handles_multi_alias_unit_dict(catalog) -> None:
    """The snap should recognise both v1 and v2 field aliases on the input."""
    units = [
        {
            "unit_id": "201",
            "_floor_plan": "Aspen-ish",
            "_bedrooms": 1,
            "_bathrooms": 1.0,
            "_sqft": 750,
        }
    ]
    out = snap_units(units, "P1", catalog)
    assert out[0]["floor_plan_id"] == "P1_1"
    assert out[0]["floor_plan_name"] == "Aspen"


def test_snap_passes_through_when_units_empty(catalog) -> None:
    assert snap_units([], "P1", catalog) == []


def test_snap_treats_string_minus_one_as_null_sqft(catalog) -> None:
    """Bug fix regression test: the `"-1"` string sentinel must behave the
    same as the `-1` integer sentinel under H2."""
    units = [
        {"unit_id": "Z", "beds": 0, "baths": 1.0, "sqft": "-1", "floor_plan_name": "Quail"}
    ]
    out = snap_units(units, "P2", catalog)
    # Property P2's only plan is 0BR/1BA/sqft=-1 — null-sqft snap should hit.
    assert out[0]["floor_plan_id"] == "P2_1"
    assert out[0]["floor_plan_snap_reason"] == "unique_two_field"
