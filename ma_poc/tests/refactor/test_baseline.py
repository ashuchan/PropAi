"""Phase 0 — tests for scripts/refactor_baseline.py."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import refactor_baseline as rb  # noqa: E402

SUCCESS_REPORT = """# Scrape Report: Sunny Oaks
**Canonical ID:** `1001`
**URL:** https://example.com/sunny-oaks
**Run Date:** 2026-04-15
**Extraction Tier:** `TIER_1_API`
**Units Extracted:** 42
## Summary

| Metric | Value |
|---|---|
| Extraction Tier | `TIER_1_API` |
| Units Extracted | 42 |
| LLM Calls Made | 0 |
| LLM Total Cost | $0.00000 |
| Errors | 0 |

## Extracted Units
(...)
"""

SSL_FAILURE_REPORT = """# Scrape Report: Tides on Southern
**Canonical ID:** `5317`
**URL:** https://tidesonsouthern.com/
**Run Date:** 2026-04-15
**Extraction Tier:** `FAILED`
**Units Extracted:** 0
## Summary

| Metric | Value |
|---|---|
| Extraction Tier | `FAILED` |
| Units Extracted | 0 |
| LLM Calls Made | 2 |
| LLM Total Cost | $0.01375 |
| Errors | 1 |

## Errors

1. net::ERR_SSL_PROTOCOL_ERROR at https://tidesonsouthern.com/
"""

PLAIN_FAILURE_REPORT = """# Scrape Report: Foo
**Canonical ID:** `9002`
**Extraction Tier:** `FAILED`
**Units Extracted:** 0

| Metric | Value |
|---|---|
| LLM Calls Made | 0 |
| LLM Total Cost | $0.00000 |

## Errors

1. Timeout
"""


def _write_run(base: Path, date: str, reports: dict[str, str], issues: list[dict] | None = None) -> Path:
    run_dir = base / "data" / "runs" / date
    (run_dir / "property_reports").mkdir(parents=True, exist_ok=True)
    for cid, body in reports.items():
        (run_dir / "property_reports" / f"{cid}.md").write_text(body, encoding="utf-8")
    if issues is not None:
        with (run_dir / "issues.jsonl").open("w", encoding="utf-8") as fh:
            for issue in issues:
                fh.write(json.dumps(issue) + "\n")
    # Minimal report.json so duration loader has something to read
    (run_dir / "report.json").write_text(
        json.dumps(
            {"duration_s": 60.0, "totals": {"properties_processed": len(reports)}}
        ),
        encoding="utf-8",
    )
    return run_dir


def test_baseline_finds_latest_run(tmp_path: Path) -> None:
    (tmp_path / "data" / "runs" / "2026-04-13").mkdir(parents=True)
    (tmp_path / "data" / "runs" / "2026-04-15").mkdir(parents=True)
    latest = rb.find_latest_run(tmp_path / "data" / "runs")
    assert latest is not None
    assert latest.name == "2026-04-15"


def test_baseline_tier_distribution(tmp_path: Path) -> None:
    reports = {
        "1001": SUCCESS_REPORT,
        "1002": SUCCESS_REPORT.replace("Sunny Oaks", "Blue Oaks").replace("1001", "1002"),
        "1003": SUCCESS_REPORT.replace("TIER_1_API", "TIER_2_JSONLD").replace("1001", "1003"),
        "5317": SSL_FAILURE_REPORT,
        "9002": PLAIN_FAILURE_REPORT,
    }
    run_dir = _write_run(tmp_path, "2026-04-15", reports, issues=[])
    stats_list = rb.collect_property_stats(run_dir)
    table = rb.build_tier_table(stats_list)
    assert "TIER_1_API" in table
    assert "TIER_2_JSONLD" in table
    assert "FAILED" in table
    assert "| 2 |" in table or "2 |" in table  # two TIER_1 rows


def test_baseline_llm_wasted_calls(tmp_path: Path) -> None:
    # Mimic the property-5317 failure mode: UNITS_EMPTY issue + LLM spend.
    issues = [{"severity": "WARNING", "code": "UNITS_EMPTY", "canonical_id": "5317"}]
    run_dir = _write_run(tmp_path, "2026-04-15", {"5317": SSL_FAILURE_REPORT}, issues=issues)
    stats_list = rb.collect_property_stats(run_dir)
    wasted = rb.count_wasted_llm_calls(stats_list, rb.load_issues(run_dir))
    assert wasted == 1


def test_baseline_handles_missing_profile_dir(tmp_path: Path) -> None:
    maturity, unknown, total = rb.load_profile_maturity(tmp_path / "config" / "profiles")
    assert total == 0
    assert unknown == 0
    assert maturity == {}


def test_baseline_parses_llm_cost(tmp_path: Path) -> None:
    run_dir = _write_run(tmp_path, "2026-04-15", {"5317": SSL_FAILURE_REPORT}, issues=[])
    stats_list = rb.collect_property_stats(run_dir)
    assert stats_list[0].llm_calls_made == 2
    assert pytest.approx(stats_list[0].llm_cost_usd, rel=1e-6) == 0.01375


def test_baseline_produce_report_smoke(tmp_path: Path) -> None:
    run_dir = _write_run(
        tmp_path,
        "2026-04-15",
        {"1001": SUCCESS_REPORT, "5317": SSL_FAILURE_REPORT},
        issues=[{"code": "UNITS_EMPTY", "canonical_id": "5317"}],
    )
    profiles_dir = tmp_path / "config" / "profiles"
    profiles_dir.mkdir(parents=True)
    (profiles_dir / "1001.json").write_text(
        json.dumps(
            {
                "canonical_id": "1001",
                "confidence": {"maturity": "WARM"},
                "api_hints": {"api_provider": "entrata"},
            }
        ),
        encoding="utf-8",
    )
    (profiles_dir / "5317.json").write_text(
        json.dumps(
            {
                "canonical_id": "5317",
                "confidence": {"maturity": "COLD"},
                "api_hints": {"api_provider": None},
            }
        ),
        encoding="utf-8",
    )
    report = rb.produce_report(run_dir, profiles_dir)
    assert "TIER_1_API" in report
    assert "Properties wasting LLM spend" in report
    assert "| 1 |" in report  # the one wasted-call property
