"""Excel report builder — sheet assembly from run output.

Covers the provenance-present (post-#95) shape and the report-absent path
(summary recomputed from properties), plus the defensive fallbacks for older
runs that predate per-unit tier / _meta.provenance.
"""

from __future__ import annotations

from typing import Any

from ma_poc.scripts.build_excel_report import (
    _PROP_HEADERS,
    _UNIT_HEADERS,
    _RP_HEADERS,
    _recompute_report,
    build_workbook,
)


def _prop_with_provenance() -> dict[str, Any]:
    return {
        "apartment_id": "P1",
        "proj_name": "Anthem Everett",
        "city": "Everett",
        "state": "WA",
        "zip_code": "98201",
        "address": "1234 Oak Street",
        "website": "https://anthemeverett.com",
        "concessions": {"text": "6 weeks free"},
        "_meta": {
            "canonical_id": "P1",
            "verdict": "SUCCESS",
            "verdict_reason": "all checks passed",
            "provenance": {
                "unit_count": 2,
                "confidence": 0.86,
                "adapter": "entrata",
                "detected_pms": "entrata",
                "winning_tier": "TIER_1_DOM_ENTRATA_PP_JD_FP",
                "is_lease_up": True,
                "fetch": {"outcome": "OK", "render_mode": "RENDER", "proxied": True, "page_load_ms": 4200},
                "data_quality": {"real_id_units": 1, "synthetic_id_units": 1, "plan_level_units": 0},
            },
        },
        "_extract_result": {"tier_used": "TIER_1_DOM_ENTRATA_PP_JD_FP", "llm_cost_usd": 0.0},
        "units": [
            {
                "unit_id": "B1::3100",
                "source_unit_id": "3100",
                "canonical_unit_id": "B1::3100",
                "unit_history_key": "unitsha_abc",
                "unit_history_key_quality": "building_scoped_source_id",
                "unit_history_key_version": "v1",
                "beds": 0,
                "baths": 1,
                "area": 464,
                "area_sqft": 464,
                "area_is_published": True,
                "rent_low": 2400,
                "rent_high": 2600,
                "rent_range": "$2,400 - $2,600",
                "rent_range_raw": "$2,400 - $2,600",
                "rent_is_range": True,
                "rent_provenance": "numeric_fields_confirmed_by_published_range",
                "availability_status": "AVAILABLE",
                "extraction_tier": "TIER_1_DOM_ENTRATA_PP_JD_FP",
                "available_date": "2026-09-01",
                "available_date_raw": "9/1/2026",
                "availability_date_provenance": "explicit_future",
                "floor_plan_name": "Studio A",
                "floor_plan_id": "fp_1",
                "building": "Building One",
                "building_id": "B1",
                "building_id_source": "source_ids.entrata_building_id",
                "is_floor_plan_level": False,
                "source_ids": {"entrata_uid": "abc"},
            },
            {
                "unit_id": "inferred_x",
                "beds": 1,
                "area": -1,
                "availability_status": "UNAVAILABLE",
                "area_sqft": None,
                "area_is_published": False,
                "extraction_tier": "TIER_4_LLM_DOM",
            },
        ],
    }


def _prop_legacy() -> dict[str, Any]:
    # Older run: no provenance, no per-unit tier.
    return {
        "apartment_id": "P2",
        "proj_name": "Estates on Main",
        "_meta": {"verdict": "FAILED_UNREACHABLE", "verdict_reason": "fetch outcome: TRANSIENT"},
        "_extract_result": {"tier_used": "FAILED", "llm_cost_usd": 0.0},
        "units": [],
    }


def _cell(ws: Any, header_row: list[str], col_name: str, data_row: int) -> Any:
    c = header_row.index(col_name) + 1
    return ws.cell(row=data_row, column=c).value


def test_workbook_has_four_sheets():
    wb = build_workbook([_prop_with_provenance(), _prop_legacy()], report=None)
    assert wb.sheetnames == ["Summary", "Properties", "Units", "RP_Format"]


def test_properties_sheet_surfaces_provenance():
    wb = build_workbook([_prop_with_provenance()], report=None)
    ws = wb["Properties"]
    # header row is row 1, first data row is row 2
    assert _cell(ws, _PROP_HEADERS, "property_id", 2) == "P1"
    assert _cell(ws, _PROP_HEADERS, "confidence", 2) == 0.86
    assert _cell(ws, _PROP_HEADERS, "adapter", 2) == "entrata"
    assert _cell(ws, _PROP_HEADERS, "winning_tier", 2) == "TIER_1_DOM_ENTRATA_PP_JD_FP"
    assert _cell(ws, _PROP_HEADERS, "fetch_outcome", 2) == "OK"
    assert _cell(ws, _PROP_HEADERS, "page_load_ms", 2) == 4200
    assert _cell(ws, _PROP_HEADERS, "real_id_units", 2) == 1
    assert _cell(ws, _PROP_HEADERS, "concession_banner", 2) == "6 weeks free"


def test_properties_legacy_falls_back_gracefully():
    wb = build_workbook([_prop_legacy()], report=None)
    ws = wb["Properties"]
    # provenance absent → confidence/adapter blank, but winning_tier from _extract_result
    assert _cell(ws, _PROP_HEADERS, "confidence", 2) is None
    assert _cell(ws, _PROP_HEADERS, "verdict", 2) == "FAILED_UNREACHABLE"
    assert _cell(ws, _PROP_HEADERS, "winning_tier", 2) == "FAILED"


def test_units_sheet_flattens_with_tier():
    wb = build_workbook([_prop_with_provenance()], report=None)
    ws = wb["Units"]
    assert ws.max_row == 3  # header + 2 units
    assert _cell(ws, _UNIT_HEADERS, "unit_id", 2) == "B1::3100"
    assert _cell(ws, _UNIT_HEADERS, "source_unit_id", 2) == "3100"
    assert _cell(ws, _UNIT_HEADERS, "canonical_unit_id", 2) == "B1::3100"
    assert _cell(ws, _UNIT_HEADERS, "unit_history_key", 2) == "unitsha_abc"
    assert _cell(ws, _UNIT_HEADERS, "building_id", 2) == "B1"
    assert _cell(ws, _UNIT_HEADERS, "available_date_raw", 2) == "9/1/2026"
    assert _cell(ws, _UNIT_HEADERS, "availability_date_provenance", 2) == "explicit_future"
    assert _cell(ws, _UNIT_HEADERS, "area_sqft", 2) == 464
    assert _cell(ws, _UNIT_HEADERS, "area_sqft", 3) is None
    assert _cell(ws, _UNIT_HEADERS, "extraction_tier", 2) == "TIER_1_DOM_ENTRATA_PP_JD_FP"
    assert _cell(ws, _UNIT_HEADERS, "rent_low", 2) == 2400
    assert _cell(ws, _UNIT_HEADERS, "rent_high", 2) == 2600
    assert _cell(ws, _UNIT_HEADERS, "rent_range", 2) == "$2,400 - $2,600"
    assert _cell(ws, _UNIT_HEADERS, "rent_provenance", 2) == ("numeric_fields_confirmed_by_published_range")
    assert _cell(ws, _UNIT_HEADERS, "source_ids", 2) == '{"entrata_uid": "abc"}'
    assert _cell(ws, _UNIT_HEADERS, "extraction_tier", 3) == "TIER_4_LLM_DOM"


def test_recompute_report_when_absent():
    rep = _recompute_report([_prop_with_provenance(), _prop_legacy()])
    assert rep["verdict_distribution"] == {"SUCCESS": 1, "FAILED_UNREACHABLE": 1}
    assert rep["totals"]["properties"] == 2
    assert rep["totals"]["succeeded"] == 1
    assert rep["data_quality"]["total_units"] == 2
    # fill rate: unit_id present on both units of P1, none on P2 (no units)
    assert rep["field_fill_rates"]["unit_id"] == 100.0


def test_summary_uses_supplied_report():
    report = {
        "run_date": "2026-07-16",
        "totals": {"properties": 5, "succeeded": 4, "failed": 1, "success_rate_pct": 80.0},
        "verdict_distribution": {"SUCCESS": 4, "FAILED_UNREACHABLE": 1},
        "tier_distribution": {"TIER_1_API": 3},
        "data_quality": {"total_units": 100, "real_id_units": 90},
        "field_fill_rates": {"unit_id": 99.0},
    }
    wb = build_workbook([_prop_with_provenance()], report=report)
    ws = wb["Summary"]
    # title row references the run date
    assert "2026-07-16" in str(ws.cell(row=1, column=1).value)


def test_rp_sheet_is_rp_format_plus_meta():
    report = {"run_date": "2026-07-16"}
    wb = build_workbook([_prop_with_provenance()], report=report)
    ws = wb["RP_Format"]
    assert _cell(ws, _RP_HEADERS, "apartmentid", 2) == "P1"
    assert _cell(ws, _RP_HEADERS, "scrapeddate", 2) == "2026-07-16"
    assert _cell(ws, _RP_HEADERS, "floorplannumber", 2) == 1
    assert _cell(ws, _RP_HEADERS, "floorplanname", 2) == "Studio A"
    assert _cell(ws, _RP_HEADERS, "unit_or_plan_level", 2) == "UNIT"
    assert _cell(ws, _RP_HEADERS, "area", 2) == 464
    assert _cell(ws, _RP_HEADERS, "unitid", 2) == "B1::3100"
    assert _cell(ws, _RP_HEADERS, "marketrentlow", 2) == 2400
    assert _cell(ws, _RP_HEADERS, "marketrenthigh", 2) == 2600
    assert _cell(ws, _RP_HEADERS, "availabledate", 2) == "2026-09-01"
    assert _cell(ws, _RP_HEADERS, "property_name", 2) == "Anthem Everett"
    assert _cell(ws, _RP_HEADERS, "address", 2) == "1234 Oak Street"
    assert _cell(ws, _RP_HEADERS, "url", 2) == "https://anthemeverett.com"
    assert ws.max_row == 2  # available-only row


def test_rp_sheet_marks_plan_vs_unit_level():
    wb = build_workbook(
        [
            {
                "apartment_id": "P3",
                "proj_name": "Level Sample",
                "units": [
                    {
                        "unit_id": "PLAN_1",
                        "availability_status": "AVAILABLE",
                        "is_floor_plan_level": True,
                        "floor_plan_name": "Garden 2B",
                        "rent_low": 2300,
                        "rent_high": 2500,
                    },
                    {
                        "unit_id": "UNIT_1",
                        "availability_status": "AVAILABLE",
                        "is_floor_plan_level": False,
                        "floor_plan_name": "Studio 2",
                        "rent_low": 1500,
                        "rent_high": 1600,
                    },
                ],
            }
        ],
        report={},
    )
    ws = wb["RP_Format"]
    assert ws.max_row == 3
    assert _cell(ws, _RP_HEADERS, "unit_or_plan_level", 2) == "PLAN"
    assert _cell(ws, _RP_HEADERS, "unit_or_plan_level", 3) == "UNIT"
    assert _cell(ws, _RP_HEADERS, "unitid", 2) == ""


def test_canonical_floor_plans_array_is_fully_exported_even_when_unavailable():
    prop = {
        "apartment_id": "PLAN-PROP",
        "proj_name": "Plan Only",
        "_meta": {"verdict": "SUCCESS_PLAN_LEVEL"},
        "units": [],
        "floor_plans": [
            {
                "floor_plan_id": "A1",
                "floor_plan_name": "A1",
                "rent_low": 1200,
                "rent_high": 1400,
                "availability_status": "UNAVAILABLE",
            },
            {
                "floor_plan_id": "B1",
                "floor_plan_name": "B1",
                "rent_low": 1600,
                "available_date": "2026-10-01",
                "availability_status": "WAITLIST",
            },
        ],
    }
    wb = build_workbook([prop], report={})
    units = wb["Units"]
    rp = wb["RP_Format"]
    assert units.max_row == 3
    assert rp.max_row == 3
    assert [_cell(rp, _RP_HEADERS, "unit_or_plan_level", row) for row in (2, 3)] == ["PLAN", "PLAN"]
    assert [_cell(rp, _RP_HEADERS, "unitid", row) for row in (2, 3)] == ["", ""]
    assert _recompute_report([prop])["data_quality"]["total_units"] == 2


def test_rp_sheet_uses_public_knock_label_without_replacing_canonical_identity():
    unit = {
        "unit_id": "knock_unit_id-7254a4ab-b615-4bc7-b088-4824a25fc03a",
        "unit_name": "4122",
        "availability_status": "AVAILABLE",
        "is_floor_plan_level": False,
    }
    prop = {"apartment_id": "64945", "proj_name": "Alcove", "units": [unit]}
    wb = build_workbook([prop], report={})

    assert _cell(wb["RP_Format"], _RP_HEADERS, "unitid", 2) == "4122"
    assert unit["unit_id"] == "knock_unit_id-7254a4ab-b615-4bc7-b088-4824a25fc03a"


def test_rp_sheet_prefers_explicit_public_unit_number():
    prop = {
        "apartment_id": "2899",
        "proj_name": "River Ridge",
        "units": [{
            "unit_id": "12925-county-road-5-208-burnsville-mn-55337",
            "unit_number": "208",
            "unit_name": "12925 County Road 5, 208, Burnsville, MN 55337",
            "availability_status": "AVAILABLE",
        }],
    }
    wb = build_workbook([prop], report={})
    assert _cell(wb["RP_Format"], _RP_HEADERS, "unitid", 2) == "208"


def test_empty_input_does_not_crash():
    wb = build_workbook([], report=None)
    assert wb.sheetnames == ["Summary", "Properties", "Units", "RP_Format"]
    ws = wb["Units"]
    assert ws.cell(row=1, column=1).value == "property_id"  # header present
    assert ws.cell(row=2, column=1).value is None  # no data rows
