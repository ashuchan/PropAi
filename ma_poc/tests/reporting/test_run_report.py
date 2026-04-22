"""Tests for run_report — run-level markdown + JSON generation."""

from __future__ import annotations

from pathlib import Path

from ma_poc.reporting.run_report import build


def _make_prop(tier: str = "TIER_1_API") -> dict:
    return {"_meta": {"scrape_tier_used": tier, "canonical_id": "p1"}}


def test_run_report_writes_both_files(tmp_path: Path) -> None:
    props = [_make_prop("TIER_1_API") for _ in range(5)]
    build(props, tmp_path, "2026-04-18")
    assert (tmp_path / "report.json").exists()
    assert (tmp_path / "report.md").exists()


def test_run_report_totals_correct(tmp_path: Path) -> None:
    props = [_make_prop("TIER_1_API")] * 3 + [_make_prop("FAILED")] * 2
    report = build(props, tmp_path, "2026-04-18")
    assert report["totals"]["properties"] == 5
    assert report["totals"]["failed"] == 2
    assert report["totals"]["succeeded"] == 3


def test_run_report_tier_distribution(tmp_path: Path) -> None:
    props = [_make_prop("TIER_1_API")] * 3 + [_make_prop("TIER_3_DOM")] * 2
    report = build(props, tmp_path, "2026-04-18")
    assert report["tier_distribution"]["TIER_1_API"] == 3
    assert report["tier_distribution"]["TIER_3_DOM"] == 2


def test_run_report_includes_slo_section(tmp_path: Path) -> None:
    props = [_make_prop()]
    build(props, tmp_path, "2026-04-18")
    md = (tmp_path / "report.md").read_text()
    assert "## SLO Status" in md


def test_run_report_reads_jugnu_extract_result_tier(tmp_path: Path) -> None:
    """Jugnu emits tier on ``_extract_result.tier_used`` (not on _meta).

    Regression: the 2026-04-19 run shipped properties without
    ``_extract_result`` attached, so the report bucketed all 100
    properties under UNKNOWN despite tier_won events being recorded.
    """
    props = [
        {
            "_meta": {"verdict": "SUCCESS", "canonical_id": f"p{i}"},
            "_extract_result": {"tier_used": "TIER_3_DOM", "llm_cost_usd": 0.0},
        }
        for i in range(3)
    ] + [
        {
            "_meta": {"verdict": "FAILED_NO_DATA", "canonical_id": "p3"},
            "_extract_result": {"tier_used": "TIER_4_LLM", "llm_cost_usd": 0.05},
        }
    ]
    report = build(props, tmp_path, "2026-04-19")
    assert report["tier_distribution"].get("TIER_3_DOM") == 3
    assert report["tier_distribution"].get("TIER_4_LLM") == 1
    assert "UNKNOWN" not in report["tier_distribution"]
    assert report["totals"]["failed"] == 1
