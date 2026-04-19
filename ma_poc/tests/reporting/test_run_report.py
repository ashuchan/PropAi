"""Tests for run_report — run-level markdown + JSON generation."""
from __future__ import annotations

import json
from pathlib import Path

from ma_poc.reporting.run_report import build


def _make_prop(tier: str = "TIER_1_API") -> dict:
    return {"_meta": {"scrape_tier_used": tier, "canonical_id": "p1"}}


def test_run_report_writes_both_files(tmp_path: Path) -> None:
    props = [_make_prop("TIER_1_API") for _ in range(5)]
    report = build(props, tmp_path, "2026-04-18")
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
