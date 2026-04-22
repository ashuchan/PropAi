"""Tests for ma_poc.services.llm_diagnostics (F1 + F2)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from ma_poc.services.llm_diagnostics import (
    AdapterDiagnosis,
    FieldRecovery,
    _build_parser_logic_summary,
    adapter_debugger,
    get_adapter_parser_source,
    null_field_recovery,
)

# ---------------------------------------------------------------------------
# F1 tests
# ---------------------------------------------------------------------------


def test_dg_t01_adapter_diagnosis_model_validates() -> None:
    data = {
        "property_id": "35534",
        "adapter_name": "rentcafe",
        "payload_url": "https://example.com/api",
        "diagnosis_summary": "Case mismatch between parser and payload.",
        "failure_category": "case_mismatch",
        "wrapper_fix": None,
        "field_fixes": [
            {
                "original_key": "floorplanname",
                "actual_key": "FloorplanName",
                "fix_type": "case_normalise",
                "example_value": "1BR",
                "code_change": "item_lc.get('floorplanname')",
            }
        ],
        "can_auto_fix": True,
        "estimated_units_recoverable": 1,
        "adapter_code_patch": "- old\n+ new",
    }
    diagnosis = AdapterDiagnosis.model_validate(data)
    assert diagnosis.failure_category == "case_mismatch"
    assert diagnosis.can_auto_fix is True
    assert len(diagnosis.field_fixes) == 1


def test_dg_t02_parser_source_rentcafe_nonempty() -> None:
    source = get_adapter_parser_source("rentcafe")
    assert "def parse_rentcafe_floorplans" in source


def test_dg_t03_parser_source_unknown_fallback() -> None:
    source = get_adapter_parser_source("unknown_pms")
    assert source.startswith("# No parser source map entry")


async def test_dg_t04_adapter_debugger_returns_none_on_llm_error(tmp_path: Path) -> None:
    """adapter_debugger catches provider exceptions and returns None."""
    fake_provider = AsyncMock()
    fake_provider.complete = AsyncMock(side_effect=RuntimeError("boom"))
    fake_provider._last_usage = {}

    with patch("llm.factory.get_text_provider", return_value=fake_provider):
        result = await adapter_debugger(
            property_id="P001",
            adapter_name="rentcafe",
            adapter_parser_source="def foo(): pass",
            api_response={"url": "https://example.com/api", "body": {"data": []}},
            property_context={"property_name": "Test"},
            output_dir=tmp_path,
        )
    assert result is None


async def test_dg_t04b_adapter_debugger_returns_valid_diagnosis_on_good_llm_response(
    tmp_path: Path,
) -> None:
    """Happy-path unit test with mocked provider — no live LLM call."""
    llm_json = {
        "property_id": "P002",
        "adapter_name": "rentcafe",
        "payload_url": "https://example.com/api",
        "diagnosis_summary": "Parser expects lowercase keys but payload uses PascalCase.",
        "failure_category": "case_mismatch",
        "wrapper_fix": {"wrapper_key_path": ["data"], "evidence": '{"data": [...]}'},
        "field_fixes": [
            {
                "original_key": "floorplanname",
                "actual_key": "FloorplanName",
                "fix_type": "case_normalise",
                "example_value": "1BR",
                "code_change": "item_lc.get('floorplanname')",
            },
            {
                "original_key": "minimumrent",
                "actual_key": "MinimumRent",
                "fix_type": "case_normalise",
                "example_value": "2100.00",
                "code_change": "item_lc.get('minimumrent')",
            },
        ],
        "can_auto_fix": True,
        "estimated_units_recoverable": 1,
        "adapter_code_patch": "- old_line\n+ new_line",
    }
    fake_provider = AsyncMock()
    fake_provider.complete = AsyncMock(return_value=json.dumps(llm_json))
    fake_provider._last_usage = {
        "input_tokens": 100,
        "output_tokens": 50,
        "model": "gpt-4o-mini",
        "provider": "azure",
    }

    with patch("llm.factory.get_text_provider", return_value=fake_provider):
        diagnosis = await adapter_debugger(
            property_id="P002",
            adapter_name="rentcafe",
            adapter_parser_source="def parse_rentcafe_floorplans(): pass",
            api_response={
                "url": "https://example.com/api",
                "body": {"data": [{"FloorplanName": "1BR", "MinimumRent": "2100.00"}]},
            },
            property_context={"property_name": "Test"},
            output_dir=tmp_path,
        )

    assert diagnosis is not None
    assert diagnosis.failure_category == "case_mismatch"
    assert diagnosis.can_auto_fix is True
    assert diagnosis.cost_usd > 0.0
    out_path = tmp_path / "P002_adapter_debug.json"
    assert out_path.exists()
    payload = json.loads(out_path.read_text())
    assert payload["_llm_interaction"]["feature"] == "adapter_debugger"


@pytest.mark.llm
async def test_dg_t05_adapter_debugger_rentcafe_pascalcase_live(tmp_path: Path) -> None:
    """Live-LLM: PascalCase RentCafe payload → case_mismatch diagnosis."""
    if not (os.environ.get("AZURE_OPENAI_DEPLOYMENT") or os.environ.get("ANTHROPIC_API_KEY")):
        pytest.skip("No LLM provider configured")
    payload = {
        "data": [
            {
                "FloorplanName": "1BR",
                "FloorplanId": "FP1",
                "MinimumRent": "2100.00",
                "MaximumRent": "2400.00",
                "AvailableUnitsCount": 2,
                "Beds": 1,
                "Baths": 1,
            }
        ]
    }
    diagnosis = await adapter_debugger(
        property_id="LIVE001",
        adapter_name="rentcafe",
        adapter_parser_source=get_adapter_parser_source("rentcafe"),
        api_response={"url": "https://example.com/rentcafe/api", "body": payload},
        property_context={"property_name": "Test Live", "website": "https://example.com"},
        output_dir=tmp_path,
    )
    assert diagnosis is not None
    assert diagnosis.failure_category in ("case_mismatch", "multiple_issues")
    assert len(diagnosis.field_fixes) >= 2
    assert diagnosis.can_auto_fix is True
    assert diagnosis.estimated_units_recoverable >= 1
    assert "-" in diagnosis.adapter_code_patch and "+" in diagnosis.adapter_code_patch


@pytest.mark.llm
async def test_dg_t06_adapter_debugger_amenities_payload_live(tmp_path: Path) -> None:
    """Live-LLM: amenities-only payload → genuinely_no_data or wrong_endpoint_type."""
    if not (os.environ.get("AZURE_OPENAI_DEPLOYMENT") or os.environ.get("ANTHROPIC_API_KEY")):
        pytest.skip("No LLM provider configured")
    payload = {
        "data": {
            "amenities": [{"id": 1, "name": "Pool"}, {"id": 2, "name": "Gym"}],
            "units": [],
            "floor_plans": [],
        }
    }
    diagnosis = await adapter_debugger(
        property_id="LIVE002",
        adapter_name="sightmap",
        adapter_parser_source=get_adapter_parser_source("sightmap"),
        api_response={"url": "https://example.com/sightmap/amenities", "body": payload},
        property_context={"property_name": "Amenities Only"},
        output_dir=tmp_path,
    )
    assert diagnosis is not None
    assert diagnosis.failure_category in ("genuinely_no_data", "wrong_endpoint_type", "empty_payload")
    assert diagnosis.can_auto_fix is False
    assert diagnosis.estimated_units_recoverable == 0


# ---------------------------------------------------------------------------
# F2 tests
# ---------------------------------------------------------------------------


def test_nf_t01_field_recovery_model_validates() -> None:
    data = {
        "property_id": "P001",
        "unit_fragment_hash": "abc123",
        "tier_used": "TIER_1_API_RENTCAFE",
        "recovered_fields": [
            {
                "field_name": "rent_low",
                "recovered_value": 2100.0,
                "confidence": 0.9,
                "source_path": "$.minimumrent",
                "parser_fix": "item_lc.get('minimumrent')",
            }
        ],
        "recovery_summary": "Recovered rent_low from minimumrent.",
        "profile_hint": {"rent_low": "minimumrent"},
    }
    recovery = FieldRecovery.model_validate(data)
    assert len(recovery.recovered_fields) == 1
    assert recovery.recovered_fields[0].field_name == "rent_low"


def test_nf_t02_parser_logic_summary_rentcafe() -> None:
    summary = _build_parser_logic_summary("rentcafe", "TIER_1_API_RENTCAFE")
    assert "minimumrent" in summary
    assert "floorplanid" in summary


async def test_nf_t03_null_field_recovery_returns_none_when_no_nulls(
    tmp_path: Path,
) -> None:
    """If partial_unit has no null rent_low / unit_id, no LLM call is made."""
    fake_provider = AsyncMock()
    fake_provider.complete = AsyncMock(return_value="{}")
    fake_provider._last_usage = {}

    with patch("llm.factory.get_text_provider", return_value=fake_provider):
        recovery = await null_field_recovery(
            property_id="P001",
            partial_unit={"rent_low": 1800.0, "unit_id": "FP1"},
            source_fragment={},
            tier_used="TIER_1_API_RENTCAFE",
            parser_logic_summary="summary",
            property_context={"property_name": "Test"},
            output_dir=tmp_path,
        )
    assert recovery is None
    fake_provider.complete.assert_not_called()


async def test_nf_t04_null_field_recovery_returns_none_on_llm_error(
    tmp_path: Path,
) -> None:
    fake_provider = AsyncMock()
    fake_provider.complete = AsyncMock(side_effect=RuntimeError("boom"))
    fake_provider._last_usage = {}

    with patch("llm.factory.get_text_provider", return_value=fake_provider):
        recovery = await null_field_recovery(
            property_id="P001",
            partial_unit={"rent_low": None, "unit_id": None},
            source_fragment={"FloorplanName": "Aspen"},
            tier_used="TIER_1_API_RENTCAFE",
            parser_logic_summary="summary",
            property_context={"property_name": "Test"},
            output_dir=tmp_path,
        )
    assert recovery is None


@pytest.mark.llm
async def test_nf_t05_null_field_recovery_rentcafe_pascalcase_live(
    tmp_path: Path,
) -> None:
    if not (os.environ.get("AZURE_OPENAI_DEPLOYMENT") or os.environ.get("ANTHROPIC_API_KEY")):
        pytest.skip("No LLM provider configured")
    partial_unit = {
        "beds": 1,
        "baths": 1,
        "floor_plan_name": None,
        "area": 700,
        "unit_id": None,
        "rent_low": None,
        "rent_high": None,
    }
    source_fragment = {
        "FloorplanName": "Aspen",
        "FloorplanId": "A1",
        "Beds": 1,
        "Baths": 1,
        "MinimumRent": "2195.00",
        "MaximumRent": "2395.00",
        "AvailableUnitsCount": 3,
        "MinimumSQFT": "685",
    }
    recovery = await null_field_recovery(
        property_id="LIVE_NF_005",
        partial_unit=partial_unit,
        source_fragment=source_fragment,
        tier_used="TIER_1_API_RENTCAFE",
        parser_logic_summary=_build_parser_logic_summary("rentcafe", "TIER_1_API_RENTCAFE"),
        property_context={"property_name": "Test Live"},
        output_dir=tmp_path,
    )
    assert recovery is not None
    assert len(recovery.recovered_fields) >= 2
    rent_low_fields = [f for f in recovery.recovered_fields if f.field_name == "rent_low"]
    assert rent_low_fields, "Expected at least one rent_low recovery"
    rf = rent_low_fields[0]
    assert rf.confidence >= 0.85
    assert rf.recovered_value is not None
    assert abs(float(rf.recovered_value) - 2195.0) < 1.0
    unit_id_fields = [f for f in recovery.recovered_fields if f.field_name == "unit_id"]
    if unit_id_fields:
        assert "floorplanid" in unit_id_fields[0].source_path.lower()
    out_path = tmp_path / "LIVE_NF_005_field_recovery.json"
    assert out_path.exists()


async def test_nf_t06_high_confidence_recovery_applied_in_jugnu_hook(
    tmp_path: Path,
) -> None:
    """The _run_null_field_recovery helper applies confidence>=0.85 recoveries in place."""
    from ma_poc.scripts.jugnu_runner import _run_null_field_recovery

    fake_recovery = FieldRecovery(
        property_id="P001",
        unit_fragment_hash="abc",
        tier_used="TIER_1_API_RENTCAFE",
        recovered_fields=[
            {  # type: ignore[list-item]
                "field_name": "rent_low",
                "recovered_value": 2195.0,
                "confidence": 0.9,
                "source_path": "$.MinimumRent",
                "parser_fix": "item_lc.get('minimumrent')",
            }
        ],
        recovery_summary="test",
        profile_hint={},
    )

    unit = {"unit_id": "FP1", "rent_low": None, "rent_high": None}
    formatted = {
        "proj_name": "Test",
        "website": "https://example.com",
        "city": "Scottsdale",
        "state": "AZ",
        "units": [unit],
    }

    class _FakeExtract:
        tier_used = "TIER_1_API_RENTCAFE"

    scrape_result = {
        "_meta": {"scrape_tier_used": "TIER_1_API_RENTCAFE"},
        "_extract_result": _FakeExtract(),
        "_raw_api_responses": [
            {"url": "https://example.com/api", "body": {"data": [{"FloorplanName": "Aspen"}]}}
        ],
    }

    with patch(
        "ma_poc.services.llm_diagnostics.null_field_recovery",
        AsyncMock(return_value=fake_recovery),
    ):
        await _run_null_field_recovery(
            scrape_result,
            formatted,
            tmp_path,
            "P001",
        )

    assert unit["rent_low"] == 2195
