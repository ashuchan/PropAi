"""Phase 6 — amenities observation report tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ma_poc.reporting.observation_reports import (
    aggregate_property_amenities,
    build_amenities_report,
)
from ma_poc.validation.schema_gate import check


def test_amenities_no_validation_gating() -> None:
    """H7: an empty amenities list does not affect schema-gate acceptance."""
    rec_no = check({"unit_id": "U1", "asking_rent": 1500, "amenities": []})
    rec_yes = check({"unit_id": "U2", "asking_rent": 1500, "amenities": ["pool"]})
    assert rec_no.accepted is not None
    assert rec_yes.accepted is not None


def test_amenities_lowercased_and_deduped() -> None:
    units = [
        {"amenities": ["Pool", "GYM ", "  Dog Park "]},
        {"amenities": ["pool", "gym", "EV charging"]},
    ]
    out = aggregate_property_amenities(units)
    assert out == ["dog park", "ev charging", "gym", "pool"]


def test_property_amenities_aggregated_from_units() -> None:
    units = [
        {"amenities": ["pool"]},
        {"amenities": ["gym"]},
    ]
    out = aggregate_property_amenities(units, explicit=["clubhouse"])
    assert "clubhouse" in out
    assert "pool" in out
    assert "gym" in out


def test_amenities_report_emitted_to_correct_path(tmp_path: Path) -> None:
    properties = [
        {
            "_property_id": "P1",
            "extraction_tier_used": "TIER_4_LLM_DOM",
            "units": [{"amenities": ["pool", "gym"]}],
        },
        {
            "_property_id": "P2",
            "extraction_tier_used": "TIER_1_API",
            "units": [{"amenities": []}],
            "property_amenities": ["clubhouse"],
        },
    ]
    report = build_amenities_report(properties, tmp_path, run_date="2026-05-05")
    out_path = tmp_path / "amenities_report.json"
    assert out_path.exists()
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert payload["summary"]["properties_total"] == 2
    assert payload["summary"]["properties_with_amenities"] == 2
    assert any(p["property_id"] == "P1" for p in payload["per_property"])
    assert any(p["property_id"] == "P2" for p in payload["per_property"])


def test_amenities_report_includes_extraction_source_breakdown(tmp_path: Path) -> None:
    properties = [
        {
            "_property_id": "P1",
            "extraction_tier_used": "TIER_4_LLM_DOM",
            "units": [{"amenities": ["pool"]}],
        },
        {
            "_property_id": "P2",
            "extraction_tier_used": "TIER_4_LLM_DOM",
            "units": [{"amenities": ["gym"]}],
        },
        {
            "_property_id": "P3",
            "extraction_tier_used": "TIER_1_API",
            "units": [{"amenities": ["dog park"]}],
        },
    ]
    report = build_amenities_report(properties, tmp_path, run_date="2026-05-05")
    breakdown = report["summary"]["extraction_source_breakdown"]
    assert breakdown["TIER_4_LLM_DOM"] == 2
    assert breakdown["TIER_1_API"] == 1


def test_amenities_report_top_50_capped(tmp_path: Path) -> None:
    properties = [
        {"_property_id": f"P{i}", "extraction_tier_used": "TIER_1_API",
         "units": [{"amenities": [f"amenity_{j}" for j in range(60)]}]}
        for i in range(3)
    ]
    report = build_amenities_report(properties, tmp_path, run_date="2026-05-05")
    assert len(report["summary"]["top_50_amenities"]) == 50
