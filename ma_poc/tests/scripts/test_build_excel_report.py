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
            {"unit_id": "3100", "beds": 0, "baths": 1, "area": 464, "rent_low": 2400,
             "availability_status": "AVAILABLE", "extraction_tier": "TIER_1_DOM_ENTRATA_PP_JD_FP",
             "is_floor_plan_level": False, "source_ids": {"entrata_uid": "abc"}},
            {"unit_id": "inferred_x", "beds": 1, "extraction_tier": "TIER_4_LLM_DOM"},
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


def test_workbook_has_three_sheets():
    wb = build_workbook([_prop_with_provenance(), _prop_legacy()], report=None)
    assert wb.sheetnames == ["Summary", "Properties", "Units"]


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
    assert _cell(ws, _UNIT_HEADERS, "unit_id", 2) == "3100"
    assert _cell(ws, _UNIT_HEADERS, "extraction_tier", 2) == "TIER_1_DOM_ENTRATA_PP_JD_FP"
    assert _cell(ws, _UNIT_HEADERS, "rent_low", 2) == 2400
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


def test_empty_input_does_not_crash():
    wb = build_workbook([], report=None)
    assert wb.sheetnames == ["Summary", "Properties", "Units"]
    ws = wb["Units"]
    assert ws.cell(row=1, column=1).value == "property_id"  # header present
    assert ws.cell(row=2, column=1).value is None           # no data rows
