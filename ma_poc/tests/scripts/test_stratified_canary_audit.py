from __future__ import annotations

import importlib.util
import sys
from datetime import date
from pathlib import Path

SCRIPT = (
    Path(__file__).resolve().parents[3]
    / "investigations"
    / "2026-08-02-stratified-1000"
    / "audit_stratified_canary.py"
)
SPEC = importlib.util.spec_from_file_location("stratified_canary_audit", SCRIPT)
assert SPEC and SPEC.loader
AUDIT = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = AUDIT
SPEC.loader.exec_module(AUDIT)


def _property() -> dict:
    return {
        "apartment_id": 1,
        "proj_name": "Evidence Apartments",
        "_meta": {
            "verdict": "SUCCESS",
            "provenance": {"adapter": "rentcafe"},
        },
    }


def _unit() -> dict:
    return {
        "unit_id": "B1:101",
        "canonical_unit_id": "B1:101",
        "source_unit_id": "101",
        "building_id": "B1",
        "building_id_source": "buildingId",
        "unit_history_key": "unitsha_" + "a" * 64,
        "unit_history_key_version": "v1",
        "area": 850,
        "area_sqft": 850,
        "area_low": 850,
        "area_high": 850,
        "area_value_type": "exact",
        "rent_low": 1500,
        "rent_high": 1700,
        "rent_range": "1500-1700",
        "rent_is_range": True,
        "rent_provenance": "numeric_endpoints",
        "available_date": "2026-08-15",
        "available_date_raw": "8/15/2026",
        "availability_date_provenance": "explicit_future",
        "availability_status": "AVAILABLE",
        "source_ids": {},
    }


def test_clean_physical_unit_satisfies_identity_area_rent_and_date_contracts() -> None:
    issues: list = []

    metrics = AUDIT.audit_unit(
        _property(),
        _unit(),
        issues,
        date(2026, 8, 2),
        set(),
    )

    assert issues == []
    assert metrics == {
        "real_id_units": 1,
        "building_id_units": 1,
        "exact_area_units": 1,
        "rent_range_units": 1,
    }


def test_negative_status_with_capture_date_is_a_critical_defect() -> None:
    row = _unit()
    row.update(
        {
            "available_date": "2026-08-02",
            "available_date_raw": None,
            "availability_date_provenance": "capture_date_default",
            "availability_status": "UNAVAILABLE",
        }
    )
    issues: list = []

    AUDIT.audit_unit(_property(), row, issues, date(2026, 8, 2), set())

    assert [(issue.severity, issue.code) for issue in issues] == [
        ("critical", "NEGATIVE_STATUS_CAPTURE_DATE")
    ]


def test_explicit_date_cannot_shift_one_day_from_raw_source() -> None:
    row = _unit()
    row.update(
        {
            "available_date": "2026-08-14",
            "available_date_raw": "2026-08-15T00:00:00-05:00",
        }
    )
    issues: list = []

    AUDIT.audit_unit(_property(), row, issues, date(2026, 8, 2), set())

    assert any(issue.code == "EXPLICIT_DATE_NORMALIZATION_SHIFT" for issue in issues)


def test_synthetic_id_with_native_source_id_is_avoidable() -> None:
    row = _unit()
    row.update(
        {
            "unit_id": "inferred_deadbeef",
            "canonical_unit_id": "inferred_deadbeef",
            "unit_history_key": None,
            "source_ids": {"sightmap_unit_id": "123"},
        }
    )
    issues: list = []

    metrics = AUDIT.audit_unit(_property(), row, issues, date(2026, 8, 2), set())

    assert metrics["avoidable_synthetic_id_units"] == 1
    assert any(issue.code == "SYNTHETIC_ID_WITH_NATIVE_ID" for issue in issues)


def test_runtime_route_aliases_match_output_adapter_names() -> None:
    prop = _property()

    assert AUDIT.target_route_exercised(prop, "rentcafe_applicant") is True
    assert AUDIT.target_route_exercised(prop, "rentcafe_layout_tab") is True
    assert AUDIT.target_route_exercised(prop, "avalonbay") is False
