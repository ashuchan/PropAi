"""Unit tests for the canary harness scripts.

Both scripts are pure functions over CSV / JSON inputs. The tests use
synthetic fixtures so they run hermetically without depending on real
cloud-run artifacts.

The harness was added in Commit 1 of MAY13_API_TIER_PORT_PLAN.md;
these tests close the gap noted in that commit's self-review (the
scripts were smoke-tested against real 5/18 + 5/19 reports but not
unit-tested).
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

# Make scripts importable as modules.
_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT / "ma_poc"))

import pytest


# ────────────────────────────────────────────────────────────────────
# Synthetic-fixture builders
# ────────────────────────────────────────────────────────────────────


_FAILURES_HEADER = [
    "property_id", "shard", "domain", "url", "verdict",
    "terminal_tier", "pms_detected", "adapter_selected",
    "fetch_outcome", "fetch_error_signature", "fetch_status",
    "body_bytes", "captcha_detected", "bot_blocked",
    "entry_captcha_detected", "entry_bot_blocked",
    "llm_rescue_attempted", "llm_rescue_succeeded",
    "llm_cost", "link_hops_attempted", "link_hops_recovered",
    "issue_codes", "pattern_id", "pattern_sub", "final_url",
]

_SUCCESSES_HEADER = [
    "property_id", "shard", "domain", "url", "verdict",
    "terminal_tier", "pms_detected", "adapter_selected",
    "fetch_outcome", "fetch_error_signature", "fetch_status",
    "body_bytes", "units", "captcha_detected", "bot_blocked",
    "entry_captcha_detected", "entry_bot_blocked",
    "llm_rescue_attempted", "llm_rescue_succeeded",
    "llm_cost", "link_hops_attempted", "link_hops_recovered",
    "issue_codes", "final_url",
]


def _write_csv(path: Path, header: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(header)
        for r in rows:
            w.writerow([r.get(k, "") for k in header])


def _make_report_dir(
    tmp: Path,
    name: str,
    *,
    failures: list[dict],
    successes: list[dict],
    summary: dict | None = None,
) -> Path:
    """Build a fake ``cloud_run_<date>/`` directory."""
    rd = tmp / name
    _write_csv(rd / "failures.csv", _FAILURES_HEADER, failures)
    _write_csv(rd / "successes.csv", _SUCCESSES_HEADER, successes)
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "summary.json").write_text(
        json.dumps(summary or {
            "run_date": name.replace("cloud_run_", ""),
            "tier_distribution": {},
            "failure_terminal_tiers": {},
            "llm_cost_total": 0.0,
            "fetch_signatures": [],
            "properties": {"total": 0, "succeeded": 0, "failed_no_data": 0,
                            "failed_unreachable": 0, "failed_other": 0,
                            "success_rate_pct": 0.0},
        }, indent=2),
        encoding="utf-8",
    )
    return rd


# ────────────────────────────────────────────────────────────────────
# build_canary_csv.py
# ────────────────────────────────────────────────────────────────────


def test_build_canary_csv_loads_and_stratifies(tmp_path: Path) -> None:
    """End-to-end: synthetic report dir -> stratified canary CSV."""
    from ma_poc.tests.canary.build_canary_csv import (
        STRATA,
        merge_reports,
        stratify,
    )

    failures = [
        {"property_id": "F-RC-1", "domain": "rcfail1.com",
         "url": "https://rcfail1.com/", "verdict": "FAILED_NO_DATA",
         "pms_detected": "rentcafe", "terminal_tier": "TIER_1_API_RENTCAFE_NO_RESPONSE"},
        {"property_id": "F-RC-2", "domain": "rcfail2.com",
         "url": "https://rcfail2.com/", "verdict": "FAILED_NO_DATA",
         "pms_detected": "rentcafe", "terminal_tier": "TIER_1_API_RENTCAFE"},
        {"property_id": "F-EN-1", "domain": "enfail1.com",
         "url": "https://enfail1.com/", "verdict": "FAILED_NO_DATA",
         "pms_detected": "entrata", "terminal_tier": "TIER_1_API_ENTRATA"},
    ]
    successes = [
        {"property_id": f"S-{i}", "domain": f"good{i}.com",
         "url": f"https://good{i}.com/", "verdict": "SUCCESS",
         "pms_detected": "rentcafe", "terminal_tier": "TIER_1_API_RENTCAFE",
         "units": str(i)}
        for i in range(20)
    ]
    rd = _make_report_dir(tmp_path, "cloud_run_TEST",
                           failures=failures, successes=successes)

    rows = merge_reports([rd])
    assert len(rows) == 23  # 3 failures + 20 successes

    samples_50 = stratify(rows, target="50", seed=2026)
    # Known-SUCCESS bucket should get 15 (target) or 20 (population
    # cap, since all 20 successes match).
    known_success = next(
        s for s in samples_50
        if s.stratum.name == "known_success_regression_watch"
    )
    assert len(known_success.rows) == 15
    # RentCafe failure bucket: 2 in pop, target 6 -> short.
    rc = next(s for s in samples_50 if s.stratum.name == "rentcafe")
    assert len(rc.rows) == 2
    assert rc.shortfall == 4


def test_build_canary_csv_dedupes_by_domain(tmp_path: Path) -> None:
    """Two rows with the same domain collapse to one (the failure-bucket
    row wins over the success-bucket row)."""
    from ma_poc.tests.canary.build_canary_csv import _dedupe_by_domain, PropertyRow

    rows = [
        PropertyRow(property_id="A", url="x", domain="dup.com", verdict="SUCCESS",
                    pms_detected="rentcafe", terminal_tier="", pattern_id="",
                    pattern_sub="", units=5),
        PropertyRow(property_id="B", url="x", domain="dup.com", verdict="FAILED_NO_DATA",
                    pms_detected="rentcafe", terminal_tier="", pattern_id="",
                    pattern_sub="", units=0),
    ]
    deduped = _dedupe_by_domain(rows)
    assert len(deduped) == 1
    # Failure wins over success on the same domain.
    assert deduped[0].verdict == "FAILED_NO_DATA"


def test_stratify_deterministic_with_seed(tmp_path: Path) -> None:
    """Same input + same seed -> identical output."""
    from ma_poc.tests.canary.build_canary_csv import (
        PropertyRow,
        stratify,
    )

    # 30 success-eligible rows; bucket cap is 15.
    rows = [
        PropertyRow(property_id=f"S-{i}", url=f"u{i}", domain=f"d{i}.com",
                    verdict="SUCCESS", pms_detected="rentcafe",
                    terminal_tier="", pattern_id="", pattern_sub="", units=i)
        for i in range(30)
    ]
    s1 = stratify(rows, target="50", seed=12345)
    s2 = stratify(rows, target="50", seed=12345)
    assert [r.property_id for r in s1[0].rows] == [r.property_id for r in s2[0].rows]


def test_strata_declared_in_order_of_priority() -> None:
    """``STRATA[0]`` is the known-SUCCESS regression watch -- it MUST
    fire first so its 150 (500-target) reserved slots are filled
    before any failure bucket consumes the same rows."""
    from ma_poc.tests.canary.build_canary_csv import STRATA
    assert STRATA[0].name == "known_success_regression_watch"


# ────────────────────────────────────────────────────────────────────
# canary_diff.py
# ────────────────────────────────────────────────────────────────────


def test_canary_diff_baseline_vs_candidate(tmp_path: Path) -> None:
    """End-to-end: synthetic baseline + candidate dirs -> DiffReport.

    Tests the win column, regression column, and total-unit-yield gate.
    """
    sys.path.insert(0, str(_REPO_ROOT / "ma_poc" / "scripts" / "diagnostics"))
    from ma_poc.scripts.diagnostics.canary_diff import compute_diff

    base = _make_report_dir(
        tmp_path, "cloud_run_BASE",
        failures=[
            {"property_id": "F1", "domain": "f1.com", "url": "https://f1.com/",
             "verdict": "FAILED_NO_DATA", "pms_detected": "rentcafe",
             "terminal_tier": "TIER_1_API_RENTCAFE_NO_RESPONSE"},
        ],
        successes=[
            {"property_id": f"S{i}", "domain": f"s{i}.com",
             "url": f"https://s{i}.com/", "verdict": "SUCCESS",
             "pms_detected": "rentcafe", "terminal_tier": "TIER_1_API_RENTCAFE",
             "units": "5"}
            for i in range(1, 11)
        ],
        summary={
            "tier_distribution": {"TIER_1_API_RENTCAFE": 10},
            "llm_cost_total": 10.0,
        },
    )
    cand = _make_report_dir(
        tmp_path, "cloud_run_CAND",
        failures=[],  # F1 is no longer failing
        successes=[
            {"property_id": f"S{i}", "domain": f"s{i}.com",
             "url": f"https://s{i}.com/", "verdict": "SUCCESS",
             "pms_detected": "rentcafe", "terminal_tier": "TIER_1_API_RENTCAFE",
             "units": "5"}
            for i in range(1, 11)
        ] + [
            {"property_id": "F1", "domain": "f1.com", "url": "https://f1.com/",
             "verdict": "SUCCESS", "pms_detected": "rentcafe",
             "terminal_tier": "TIER_1_API_RENTCAFE", "units": "8"},
        ],
        summary={
            "tier_distribution": {"TIER_1_API_RENTCAFE": 11},
            "llm_cost_total": 9.0,
        },
    )
    report = compute_diff(base, cand)
    # F1 newly succeeded.
    assert "F1" in [r.property_id for r in report.newly_succeeded]
    # No new failures.
    assert report.newly_failed == []
    # Total unit yield went 50 -> 58 = +16%.
    yield_gate = next(g for g in report.gates if g.name == "total_unit_yield")
    assert yield_gate.status == "PASS"


def test_canary_diff_flags_success_to_failed_regression(tmp_path: Path) -> None:
    """A SUCCESS in baseline that flips to FAILED in candidate must
    appear in the regression column. Gate FAILs when regression count
    exceeds ``max(1, 0.5% of baseline-success bucket)``.

    Fixture uses 10 baseline successes + 3 regressions so the floor
    of 1 (`max(1, int(10*0.005))=1`) is exceeded -> gate FAILs.
    """
    from ma_poc.scripts.diagnostics.canary_diff import compute_diff

    base_successes = [
        {"property_id": f"WAS_OK_{i}", "domain": f"ok{i}.com",
         "url": f"https://ok{i}.com/", "verdict": "SUCCESS",
         "pms_detected": "rentcafe", "terminal_tier": "TIER_1_API_RENTCAFE",
         "units": "5"}
        for i in range(10)
    ]
    base = _make_report_dir(
        tmp_path, "cloud_run_BASE2",
        failures=[],
        successes=base_successes,
        summary={"tier_distribution": {"TIER_1_API_RENTCAFE": 10},
                 "llm_cost_total": 0.0},
    )
    # 3 properties newly fail (more than the 1-allowed floor).
    candidate_failures = [
        {"property_id": f"WAS_OK_{i}", "domain": f"ok{i}.com",
         "url": f"https://ok{i}.com/", "verdict": "FAILED_NO_DATA",
         "pms_detected": "rentcafe",
         "terminal_tier": "TIER_1_API_RENTCAFE_NO_RESPONSE"}
        for i in range(3)
    ]
    candidate_successes = [
        {"property_id": f"WAS_OK_{i}", "domain": f"ok{i}.com",
         "url": f"https://ok{i}.com/", "verdict": "SUCCESS",
         "pms_detected": "rentcafe", "terminal_tier": "TIER_1_API_RENTCAFE",
         "units": "5"}
        for i in range(3, 10)
    ]
    cand = _make_report_dir(
        tmp_path, "cloud_run_CAND2",
        failures=candidate_failures,
        successes=candidate_successes,
        summary={"tier_distribution": {"TIER_1_API_RENTCAFE": 7,
                                        "TIER_1_API_RENTCAFE_NO_RESPONSE": 3},
                 "llm_cost_total": 0.0},
    )
    report = compute_diff(base, cand)
    # All 3 newly-failed PIDs appear in the regression column.
    regressed_pids = {r.property_id for r in report.newly_failed}
    assert regressed_pids == {"WAS_OK_0", "WAS_OK_1", "WAS_OK_2"}
    # Gate FAILs because 3 > max(1, int(10*0.005))=1.
    regression_gate = next(
        g for g in report.gates if g.name == "success_to_failed_regressions"
    )
    assert regression_gate.status == "FAIL"
    assert report.failed_any


def test_canary_diff_handles_missing_summary_json(tmp_path: Path) -> None:
    """Cleanly reports missing inputs rather than crashing."""
    from ma_poc.scripts.diagnostics.canary_diff import compute_diff

    empty = tmp_path / "no_summary"
    empty.mkdir()
    with pytest.raises(FileNotFoundError):
        compute_diff(empty, empty)
