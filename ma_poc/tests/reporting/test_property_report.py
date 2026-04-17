"""Tests for reporting.property_report module."""

from __future__ import annotations

import sys
import os

# Ensure the ma_poc package root is importable
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from reporting.property_report import generate_property_report


def _base_result(**overrides):
    """Helper to build a minimal scrape_result dict with overrides."""
    result = {
        "property_name": "Sunset Apartments",
        "units": [],
        "errors": [],
        "scrape_duration_s": 4.2,
        "llm_cost": 0.0012,
    }
    result.update(overrides)
    return result


class TestVerdict:
    """Tests for verdict determination."""

    def test_report_verdict_success(self):
        result = _base_result(units=[{"unit_number": "101", "asking_rent": 1450}])
        report = generate_property_report(result, "P001", "2026-04-17")
        assert "| **Verdict** | SUCCESS |" in report

    def test_report_verdict_unreachable(self):
        result = _base_result(
            units=[],
            errors=["net::ERR_SSL_PROTOCOL_ERROR"],
        )
        report = generate_property_report(result, "P002", "2026-04-17")
        assert "| **Verdict** | FAILED_UNREACHABLE |" in report

    def test_report_verdict_no_data(self):
        result = _base_result(units=[], errors=[])
        report = generate_property_report(result, "P003", "2026-04-17")
        assert "| **Verdict** | FAILED_NO_DATA |" in report

    def test_report_verdict_carry_forward(self):
        result = _base_result(_carry_forward=True, units=[])
        report = generate_property_report(result, "P004", "2026-04-17")
        assert "| **Verdict** | CARRY_FORWARD |" in report


class TestLLMSection:
    """Tests for LLM calls section rendering."""

    def test_report_omits_llm_section_when_no_calls(self):
        result = _base_result(
            units=[{"unit_number": "101"}],
            llm_calls=None,
        )
        report = generate_property_report(result, "P001", "2026-04-17")
        assert "## LLM calls" not in report

    def test_report_collapses_transcripts(self):
        result = _base_result(
            units=[{"unit_number": "101"}],
            llm_calls=[
                {
                    "model": "gpt-4o-mini",
                    "prompt": "Extract units from HTML",
                    "response": '{"units": []}',
                }
            ],
        )
        report = generate_property_report(result, "P001", "2026-04-17")
        assert "## LLM calls" in report
        assert "<details>" in report
        assert "<summary>Show LLM transcripts</summary>" in report
        assert "</details>" in report
        assert "gpt-4o-mini" in report
        assert "Extract units from HTML" in report


class TestDetection:
    """Tests for detection section rendering."""

    def test_report_shows_detection_evidence(self):
        result = _base_result(
            units=[{"unit_number": "101"}],
            _detected_pms={
                "name": "RentCafe",
                "confidence": 0.95,
                "evidence": ["meta tag", "API pattern /rentcafe/"],
                "client_account_id": "ACCT-123",
                "adapter": "rentcafe_v2",
            },
        )
        report = generate_property_report(result, "P001", "2026-04-17")
        assert "## Detection" in report
        assert "RentCafe" in report
        assert "0.95" in report
        assert "meta tag" in report
        assert "API pattern /rentcafe/" in report
        assert "ACCT-123" in report
        assert "rentcafe_v2" in report

    def test_report_renders_without_detection_for_legacy_input(self):
        """Legacy scrape results without _detected_pms should omit the section."""
        result = _base_result(units=[{"unit_number": "101"}])
        # No _detected_pms key at all
        report = generate_property_report(result, "P001", "2026-04-17")
        assert "## Detection" not in report
        # Report should still render successfully
        assert "## Status" in report
        assert "| **Verdict** | SUCCESS |" in report


class TestChanges:
    """Tests for changes-since-last-run section."""

    def test_report_shows_changes_since_last_run(self):
        prior = [
            {"unit_id": "U101", "asking_rent": 1400, "sqft": 750},
            {"unit_id": "U102", "asking_rent": 1500, "sqft": 800},
        ]
        current_units = [
            {"unit_id": "U101", "asking_rent": 1450, "sqft": 750},
            {"unit_id": "U103", "asking_rent": 1600, "sqft": 900},
        ]
        result = _base_result(units=current_units)
        report = generate_property_report(
            result, "P001", "2026-04-17", prior_units=prior
        )
        assert "## Changes since last run" in report
        assert "New units" in report
        assert "U103" in report
        assert "Removed units" in report
        assert "U102" in report
        assert "asking_rent: 1400 -> 1450" in report

    def test_report_omits_changes_for_new_property(self):
        result = _base_result(units=[{"unit_number": "101"}])
        report = generate_property_report(
            result, "P001", "2026-04-17", prior_units=None
        )
        assert "## Changes since last run" not in report


class TestPipeline:
    """Tests for pipeline and adapter fallback rendering."""

    def test_report_shows_fallback_chain_when_adapter_failed(self):
        result = _base_result(
            units=[{"unit_number": "101"}],
            pipeline_steps=[
                {"name": "Tier 1 API", "outcome": "FAILED", "notes": "no API found"},
                {"name": "Tier 2 JSON-LD", "outcome": "FAILED", "notes": "no schema"},
                {
                    "name": "Tier 3 Template",
                    "outcome": "SUCCESS",
                    "notes": "RentCafe adapter",
                },
            ],
        )
        report = generate_property_report(result, "P001", "2026-04-17")
        assert "## Pipeline" in report
        assert "Tier 1 API" in report
        assert "FAILED" in report
        assert "Tier 3 Template" in report
        assert "RentCafe adapter" in report
