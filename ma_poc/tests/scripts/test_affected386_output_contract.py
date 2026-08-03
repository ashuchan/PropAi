"""Regression contract grounded in the 2026-08-02 affected-386 canary."""

from __future__ import annotations

import gzip
import json
from datetime import UTC, datetime

import pytest

from ma_poc.core.schema_v2 import _format_v2_unit as _core_format_v2_unit
from ma_poc.scripts.runners.jugnu import (
    _archive_raw_api_responses,
    _archive_raw_source_responses,
    _format_v2,
    _format_v2_unit,
    _provenance_block,
)

_TS = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize("formatter", [_core_format_v2_unit, _format_v2_unit])
def test_formatter_prefers_natural_number_over_prior_synthetic_id(formatter) -> None:
    row = formatter(
        {
            "unit_id": "inferred_deadbeefdeadbeef",
            "unit_number": "1001",
            "floor_plan_name": "A1",
            "bedrooms": 1,
            "bathrooms": 1,
            "sqft": 750,
            "market_rent_low": 1650,
        },
        _TS,
        "241798",
    )

    assert row["unit_id"] == "1001"
    assert row["source_unit_id"] == "1001"


def test_explicit_provider_plan_names_survive_without_weakening_generic_hygiene() -> None:
    camden = {
        "floor_plan_name": "2.1",
        "unit_number": "106",
        "bedrooms": "2",
        "bathrooms": "1",
        "_floor_plan_name_provenance": "camden.floorPlan.name",
    }
    rentcafe = {
        "floor_plan_name": "1 Bed 1 Bath",
        "unit_number": "01_15",
        "bedrooms": "1",
        "bathrooms": "1",
        "_floor_plan_name_provenance": "rentcafe.layout-tab.plan-label",
    }

    assert _format_v2_unit(camden, _TS, "14062")["floor_plan_name"] == "2.1"
    assert _format_v2_unit(rentcafe, _TS, "232538")["floor_plan_name"] == "1 Bed 1 Bath"
    assert (
        _format_v2_unit(
            {**camden, "_floor_plan_name_provenance": "forged.provider.name"},
            _TS,
            "14062",
        )["floor_plan_name"]
        is None
    )
    assert (
        _format_v2_unit(
            {**rentcafe, "_floor_plan_name_provenance": None},
            _TS,
            "232538",
        )["floor_plan_name"]
        is None
    )


def test_output_preserves_source_identity_and_adds_collision_safe_history_key() -> None:
    result = {
        "units": [
            {
                "floor_plan_name": "A1",
                "unit_number": "106",
                "building": "North",
                "bedrooms": "1",
                "bathrooms": "1",
                "sqft": "700",
                "market_rent_low": 1500,
            },
            {
                "floor_plan_name": "A1",
                "unit_number": "106",
                "building": "South",
                "bedrooms": "1",
                "bathrooms": "1",
                "sqft": "700",
                "market_rent_low": 1550,
            },
        ],
        "plan_summaries": [],
    }
    prop = _format_v2(result, {"apartmentid": "999"})
    assert {row["unit_id"] for row in prop["units"]} == {"North-106", "South-106"}
    assert {row["source_unit_id"] for row in prop["units"]} == {"106"}
    assert {row["building_id"] for row in prop["units"]} == {"North", "South"}
    assert all(row["canonical_unit_id"] == row["unit_id"] for row in prop["units"])
    assert len({row["unit_history_key"] for row in prop["units"]}) == 2
    assert all(row["unit_history_key"].startswith("unitsha_") for row in prop["units"])
    assert all(
        row["unit_history_key_quality"] == "building_and_floor_plan_scoped_source_id" for row in prop["units"]
    )
    assert all(row["area_sqft"] == 700 and row["area_is_published"] for row in prop["units"])


def test_nullable_area_companion_keeps_legacy_sentinel_auditable() -> None:
    row = _format_v2_unit(
        {"floor_plan_name": "A1", "unit_number": "101", "bedrooms": "1"},
        _TS,
        "999",
    )
    assert row["area"] == -1
    assert row["area_sqft"] is None
    assert row["area_is_published"] is False
    assert row["area_absence"] is not None


def test_history_key_is_rent_and_availability_stable_but_property_scoped() -> None:
    def one(property_id: str, rent: int, date: str) -> str:
        prop = _format_v2(
            {
                "units": [
                    {
                        "floor_plan_name": "A1",
                        "unit_number": "106",
                        "building": "North",
                        "bedrooms": "1",
                        "bathrooms": "1",
                        "sqft": "700",
                        "market_rent_low": rent,
                        "availability_date": date,
                    }
                ],
                "plan_summaries": [],
            },
            {"apartmentid": property_id},
        )
        return prop["units"][0]["unit_history_key"]

    first = one("999", 1500, "2026-08-02")
    changed_snapshot = one("999", 1650, "2026-09-15")
    other_property = one("1000", 1500, "2026-08-02")
    assert first == changed_snapshot
    assert first != other_property


def test_sightmap_catalogue_marker_moves_out_of_physical_units() -> None:
    result = {
        "extraction_tier_used": "TIER_1_API_SIGHTMAP",
        "units": [
            {
                "floor_plan_name": "A1",
                "unit_number": "101",
                "bedrooms": "1",
                "bathrooms": "1",
                "sqft": "700",
                "market_rent_low": 1500,
            },
            {
                "floor_plan_name": "B2",
                "bedrooms": "2",
                "bathrooms": "2",
                "availability_status": "UNAVAILABLE",
                "available_units": "0",
                "data_quality_flag": "SIGHTMAP_PLAN_PRESENCE",
            },
        ],
        "plan_summaries": [],
    }
    prop = _format_v2(result, {"apartmentid": "777"})
    assert [row["unit_id"] for row in prop["units"]] == ["101"]
    assert len(prop["floor_plans"]) == 1
    assert prop["floor_plans"][0]["floor_plan_name"] == "B2"
    assert prop["floor_plans"][0]["unit_id"] is None
    assert prop["floor_plans"][0]["is_floor_plan_level"] is True


def test_provenance_emits_traceable_stage_and_identity_contract() -> None:
    result = {
        "units": [
            {
                "floor_plan_name": "A1",
                "unit_number": "101",
                "bedrooms": "1",
                "market_rent_low": 1500,
                "available_date": "2026-09-01",
            }
        ],
        "plan_summaries": [],
        "_raw_api_responses": [{"url": "https://api.test/units", "body": {"units": [1]}}],
        "_unit_source_provenance": [
            {
                "provider": "test",
                "source_url": "https://api.test/units",
                "identity": {"status": "MATCH"},
                "unit_count": 1,
            }
        ],
    }
    formatted = _format_v2(result, {"apartmentid": "888"})
    result["_v2_formatted"] = formatted
    provenance = _provenance_block(result, {}, None, "SUCCESS")

    assert provenance["raw_source_count"] == 1
    assert provenance["parser_count"] == 1
    assert provenance["formatted_count"] == 1
    assert provenance["final_admitted_count"] == 1
    assert provenance["canonical_id_uniqueness"]["passed"] is True
    assert provenance["property_identity_verdict"]["status"] == "MATCH"
    assert provenance["unit_source_provenance"] == result["_unit_source_provenance"]
    assert provenance["availability_date_provenance"] == {"explicit_future": 1}


def test_full_raw_api_archive_is_compressed_complete_and_secret_redacted(tmp_path) -> None:
    result = {
        "_raw_api_responses": [
            {
                "url": "https://api.test/units?api_key=secret&property=42",
                "status": 200,
                "content_type": "application/json",
                "body": {
                    "accessToken": "secret-token",
                    "units": [{"id": str(index), "rent": 1000 + index} for index in range(250)],
                },
            }
        ]
    }
    metadata = _archive_raw_api_responses(result, tmp_path, "42", "TIER_1_API_TEST")
    assert metadata is not None
    archive = tmp_path / metadata["path"]
    with gzip.open(archive, "rt", encoding="utf-8") as handle:
        payload = json.load(handle)

    assert payload["response_count"] == 1
    assert len(payload["responses"][0]["body"]["units"]) == 250
    assert payload["responses"][0]["body"]["accessToken"] == "<redacted>"
    assert "secret" not in payload["responses"][0]["url"]
    assert "property=42" in payload["responses"][0]["url"]


def test_content_addressed_sources_and_extraction_snapshot_are_replayable(
    tmp_path,
) -> None:
    """API, authored HTML, assets and row stages survive as offline artifacts."""

    result = {
        "units": [
            {
                "unit_number": "101",
                "sqft": "700",
                "source_response_sha256": "source-pointer",
            }
        ],
        "plan_summaries": [],
        "_v2_formatted": {"units": [{"unit_id": "101", "area": 700}]},
        "_area_enrichment_diagnostic": {"matched_units": 1},
        "_raw_api_responses": [
            {
                "url": "https://api.test/units?token=secret",
                "status": 200,
                "body": {"authorization": "secret", "units": [{"id": "101"}]},
            }
        ],
        "_raw_html_responses": [
            {
                "url": "https://property.test/floorplans",
                "status": 200,
                "body": '<div data-unit="101" data-area="700"></div>',
                "response_kind": "unit_area_enrichment_html",
            }
        ],
        "_raw_asset_responses": [
            {
                "url": "https://property.test/images/101.jpg",
                "status": 200,
                "body": b"jpeg-bytes",
                "content_type": "image/jpeg",
            }
        ],
    }

    metadata = _archive_raw_source_responses(
        result,
        tmp_path,
        "42",
        "TIER_TEST",
    )
    assert metadata is not None
    assert metadata["source_kind_counts"] == {"api": 1, "html": 1, "asset": 1}

    with gzip.open(tmp_path / metadata["manifest_path"], "rt", encoding="utf-8") as handle:
        manifest = json.load(handle)
    assert manifest["source_count"] == 3
    assert len({record["source_response_sha256"] for record in manifest["responses"]}) == 3
    for record in manifest["responses"]:
        assert (tmp_path / record["archive_body_path"]).exists()

    api_record = next(record for record in manifest["responses"] if record["kind"] == "api")
    with gzip.open(tmp_path / api_record["archive_body_path"], "rt", encoding="utf-8") as handle:
        archived_api = json.load(handle)
    assert archived_api["authorization"] == "<redacted>"

    snapshot = manifest["extraction_snapshot"]
    with gzip.open(tmp_path / snapshot["path"], "rt", encoding="utf-8") as handle:
        extracted = json.load(handle)
    assert extracted["units_pre_format"][0]["unit_number"] == "101"
    assert extracted["formatted_property"]["units"][0]["area"] == 700
    assert extracted["area_enrichment_diagnostic"] == {"matched_units": 1}
