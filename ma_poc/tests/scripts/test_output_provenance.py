"""Output-surfacing pass (2026-07-16): emit already-captured diagnostics.

A — per-unit extraction_tier (schema_v2._format_v2_unit)
B — property _meta.provenance block (jugnu._provenance_block + helpers)
C — run-report dashboard: verdict_distribution / field_fill_rates / data_quality
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from ma_poc.core.schema_v2 import _format_v2_unit
from ma_poc.reporting.run_report import build as build_report
from ma_poc.scripts.runners.jugnu import (
    _is_lease_up,
    _provenance_block,
    _tier_family,
)

_TS = datetime(2026, 7, 16, 12, 0, 0)


# ── A: per-unit tier ─────────────────────────────────────────────────────────

def test_unit_surfaces_extraction_tier():
    u = {"unit_id": "N115", "beds": 1, "extraction_tier": "TIER_1_DOM_ENTRATA_PP_JD_FP"}
    out = _format_v2_unit(u, _TS, "P1")
    assert out["extraction_tier"] == "TIER_1_DOM_ENTRATA_PP_JD_FP"


def test_unit_extraction_tier_none_when_absent():
    out = _format_v2_unit({"unit_id": "101"}, _TS, "P1")
    assert out["extraction_tier"] is None


# ── B: tier family + lease-up + provenance block ─────────────────────────────

def test_tier_family_classification():
    assert _tier_family("TIER_4_LLM_DOM") == "LLM"
    assert _tier_family("TIER_1_API_RENTCAFE_YSI_UNITSLIST") == "API"
    assert _tier_family("TIER_1_DOM_ENTRATA_PP_JD_FP") == "DOM"
    assert _tier_family("TIER_2_JSONLD") == "JSONLD"
    assert _tier_family("TIER_1_5_EMBEDDED") == "EMBEDDED"
    assert _tier_family("") == "NONE"


def test_is_lease_up():
    assert _is_lease_up({"type": "LEASE_UP"}) is True
    assert _is_lease_up({"Type": "lease-up"}) is True
    assert _is_lease_up({"type": "STABILISED"}) is False
    assert _is_lease_up({}) is False


class _FakeFetch:
    class _RM:
        value = "RENDER"

    render_mode = _RM()
    proxy_label = "brightdata-resi"
    elapsed_ms = 4200


def test_provenance_block_fields_and_quality():
    result = {
        "units": [
            {"unit_id": "N115", "extraction_tier": "TIER_1_DOM_ENTRATA_PP_JD_FP"},
            {"unit_id": "inferred_ab12", "extraction_tier": "TIER_4_LLM_DOM"},
            {
                "unit_id": "plan1",
                "extraction_tier": "TIER_1_DOM_REALPAGE_CWS_PLAN_LEVEL",
                "is_floor_plan_level": True,
            },
        ],
        "confidence": 0.86,
        "_adapter_used": "entrata",
        "_detected_pms": {"pms": "entrata"},
        "extraction_tier_used": "TIER_1_DOM_ENTRATA_PP_JD_FP",
        "_fallback_chain": ["api", "dom"],
        "api_calls_intercepted": [1, 2, 3],
    }
    prov = _provenance_block(result, {"type": "LEASE_UP"}, _FakeFetch(), "OK")
    assert prov["unit_count"] == 3
    assert prov["confidence"] == 0.86
    assert prov["adapter"] == "entrata"
    assert prov["detected_pms"] == "entrata"
    assert prov["tiers_attempted"] == ["api", "dom"]
    assert prov["api_calls_intercepted"] == 3
    assert prov["is_lease_up"] is True
    assert prov["fetch"] == {
        "outcome": "OK",
        "render_mode": "RENDER",
        "proxied": True,
        "page_load_ms": 4200,
        "status_code": None,
        "error_signature": None,
        "body_bytes": 0,
    }
    dq = prov["data_quality"]
    assert dq["real_id_units"] == 2          # N115 + plan1 (plan1 has a real id)
    assert dq["synthetic_id_units"] == 1     # inferred_ab12
    assert dq["plan_level_units"] == 1
    assert dq["by_tier_family"] == {"DOM": 2, "LLM": 1}


def test_provenance_block_empty_units():
    prov = _provenance_block({"units": []}, {}, _FakeFetch(), "TRANSIENT")
    assert prov["unit_count"] == 0
    assert prov["data_quality"]["total_units" if False else "real_id_units"] == 0
    assert prov["fetch"]["outcome"] == "TRANSIENT"


# ── C: run-report dashboard ──────────────────────────────────────────────────

def test_report_adds_dashboard_sections(tmp_path: Path):
    u_gold = _format_v2_unit(
        {"unit_id": "N115", "beds": 1, "baths": 1.0, "area": 665,
         "market_rent_low": 1350, "availability_status": "AVAILABLE",
         "extraction_tier": "TIER_1_DOM_ENTRATA_PP_JD_FP"},
        _TS, "P1",
    )
    u_llm = _format_v2_unit(
        {"unit_id": "inferred_x", "beds": 2, "extraction_tier": "TIER_4_LLM_DOM"},
        _TS, "P1",
    )
    props = [
        {"_meta": {"verdict": "SUCCESS"}, "units": [u_gold, u_llm]},
        {"_meta": {"verdict": "FAILED_UNREACHABLE"}, "units": []},
    ]
    rep = build_report(props, tmp_path, "2026-07-16")
    assert set(rep) >= {"verdict_distribution", "field_fill_rates", "data_quality"}
    assert rep["verdict_distribution"] == {"SUCCESS": 1, "FAILED_UNREACHABLE": 1}
    dq = rep["data_quality"]
    assert dq["total_units"] == 2
    assert dq["real_id_units"] == 1          # N115
    assert dq["synthetic_id_units"] == 1     # inferred_x
    assert dq["by_tier_family"] == {"DOM": 1, "LLM": 1}
    # fill rates: unit_id present on both, floor absent on both
    assert rep["field_fill_rates"]["unit_id"] == 100.0
    assert rep["field_fill_rates"]["floor"] == 0.0


def test_report_dashboard_zero_units(tmp_path: Path):
    props = [{"_meta": {"verdict": "FAILED_UNREACHABLE"}, "units": []}]
    rep = build_report(props, tmp_path, "2026-07-16")
    assert rep["data_quality"]["total_units"] == 0
    assert rep["field_fill_rates"]["unit_id"] == 0.0  # no divide-by-zero
