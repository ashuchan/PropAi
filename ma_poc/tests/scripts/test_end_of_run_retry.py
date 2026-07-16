"""End-of-run transient retry pass — pure selection/merge helpers.

The runner (run_jugnu) re-drives ONLY transient-class fetch failures through
the same _process_one path once, at the end of the run, then overlays the
retry results onto the first pass. These tests cover the pure decision helpers
that decide *what* gets retried and *whether* a retry replaces a record.
"""

from __future__ import annotations

from typing import Any

from ma_poc.discovery.contracts import CrawlTask, TaskReason
from ma_poc.fetch.contracts import RenderMode
from ma_poc.scripts.runners.jugnu import (
    _apply_retry_results,
    _end_of_run_retry_delay_s,
    _end_of_run_retry_enabled,
    _is_transient_fetch_failure,
    _retry_improved,
    _select_transient_retry_tasks,
)


def _task(pid: str) -> CrawlTask:
    return CrawlTask(
        url=f"https://{pid}.example.com/",
        property_id=pid,
        priority=0,
        budget_ms=30000,
        reason=TaskReason.SCHEDULED,
        render_mode=RenderMode.GET,
    )


def _rec(verdict: str, reason: str = "", units: int = 0) -> dict[str, Any]:
    return {
        "_meta": {"verdict": verdict, "verdict_reason": reason},
        "units": [{"unit_id": str(i)} for i in range(units)],
    }


# ── _is_transient_fetch_failure ──────────────────────────────────────────────

def test_transient_outcomes_are_retriable():
    for outcome in ("TRANSIENT", "RATE_LIMITED", "PROXY_ERROR", "EMPTY_BODY"):
        rec = _rec("FAILED_UNREACHABLE", f"fetch outcome: {outcome}")
        assert _is_transient_fetch_failure(rec) is True, outcome


def test_bot_block_and_hard_fail_not_retriable():
    for outcome in ("BOT_BLOCKED", "HARD_FAIL"):
        rec = _rec("FAILED_UNREACHABLE", f"fetch outcome: {outcome}")
        assert _is_transient_fetch_failure(rec) is False, outcome


def test_dead_url_not_retriable():
    assert _is_transient_fetch_failure(_rec("DEAD_URL", "url is dead")) is False


def test_failed_no_data_not_retriable():
    # Reached the page, empty extraction — a re-fetch won't help.
    assert _is_transient_fetch_failure(_rec("FAILED_NO_DATA", "no records extracted")) is False


def test_success_not_retriable():
    assert _is_transient_fetch_failure(_rec("SUCCESS", "all checks passed", units=5)) is False


# ── _retry_improved ──────────────────────────────────────────────────────────

def test_retry_improved_failed_to_success():
    old = _rec("FAILED_UNREACHABLE", "fetch outcome: TRANSIENT")
    new = _rec("SUCCESS", "all checks passed", units=4)
    assert _retry_improved(old, new) is True


def test_retry_improved_gained_units():
    old = _rec("FAILED_UNREACHABLE", "fetch outcome: TRANSIENT", units=0)
    new = _rec("FAILED_NO_DATA", "no records extracted", units=2)
    assert _retry_improved(old, new) is True


def test_retry_not_improved_still_failed():
    old = _rec("FAILED_UNREACHABLE", "fetch outcome: TRANSIENT")
    new = _rec("FAILED_UNREACHABLE", "fetch outcome: TRANSIENT")
    assert _retry_improved(old, new) is False


def test_retry_not_improved_fewer_units():
    old = _rec("SUCCESS", "all checks passed", units=5)
    new = _rec("SUCCESS", "all checks passed", units=3)
    assert _retry_improved(old, new) is False


# ── _select_transient_retry_tasks ────────────────────────────────────────────

def test_selects_only_transient_failures():
    tasks = [_task("P1"), _task("P2"), _task("P3"), _task("P4")]
    results = [
        _rec("FAILED_UNREACHABLE", "fetch outcome: TRANSIENT"),   # retry
        _rec("SUCCESS", "all checks passed", units=3),            # no
        _rec("FAILED_UNREACHABLE", "fetch outcome: BOT_BLOCKED"), # no
        _rec("DEAD_URL", "url is dead"),                          # no
    ]
    retry = _select_transient_retry_tasks(tasks, results)
    assert [t.property_id for t in retry] == ["P1"]
    assert retry[0].reason == TaskReason.RETRY


def test_selects_skips_non_dict_results():
    tasks = [_task("P1"), _task("P2")]
    results = [ValueError("boom"), _rec("FAILED_UNREACHABLE", "fetch outcome: RATE_LIMITED")]
    retry = _select_transient_retry_tasks(tasks, results)
    assert [t.property_id for t in retry] == ["P2"]


# ── _apply_retry_results ─────────────────────────────────────────────────────

def test_apply_overlays_recovered_record():
    tasks = [_task("P1"), _task("P2")]
    results = [
        _rec("FAILED_UNREACHABLE", "fetch outcome: TRANSIENT"),
        _rec("SUCCESS", "all checks passed", units=2),
    ]
    retry_tasks = [_dc_retry("P1")]
    retry_results = [_rec("SUCCESS", "all checks passed", units=6)]
    merged = _apply_retry_results(tasks, results, retry_tasks, retry_results)
    assert merged[0]["_meta"]["verdict"] == "SUCCESS"
    assert len(merged[0]["units"]) == 6          # replaced with the recovered record
    assert merged[1]["_meta"]["verdict"] == "SUCCESS"  # untouched, order preserved


def test_apply_keeps_original_when_retry_still_failed():
    tasks = [_task("P1")]
    results = [_rec("FAILED_UNREACHABLE", "fetch outcome: TRANSIENT")]
    retry_tasks = [_dc_retry("P1")]
    retry_results = [_rec("FAILED_UNREACHABLE", "fetch outcome: TRANSIENT")]
    merged = _apply_retry_results(tasks, results, retry_tasks, retry_results)
    assert merged[0]["_meta"]["verdict"] == "FAILED_UNREACHABLE"  # not overwritten


def test_apply_preserves_order_and_length():
    tasks = [_task("P1"), _task("P2"), _task("P3")]
    results = [
        _rec("SUCCESS", units=1),
        _rec("FAILED_UNREACHABLE", "fetch outcome: TRANSIENT"),
        _rec("SUCCESS", units=2),
    ]
    retry_tasks = [_dc_retry("P2")]
    retry_results = [_rec("SUCCESS", units=9)]
    merged = _apply_retry_results(tasks, results, retry_tasks, retry_results)
    assert len(merged) == 3
    assert len(merged[0]["units"]) == 1
    assert len(merged[1]["units"]) == 9
    assert len(merged[2]["units"]) == 2


def _dc_retry(pid: str) -> CrawlTask:
    from dataclasses import replace
    return replace(_task(pid), reason=TaskReason.RETRY)


# ── env flags ────────────────────────────────────────────────────────────────

def test_flag_default_off(monkeypatch):
    monkeypatch.delenv("ENABLE_END_OF_RUN_RETRY", raising=False)
    assert _end_of_run_retry_enabled() is False


def test_flag_on(monkeypatch):
    monkeypatch.setenv("ENABLE_END_OF_RUN_RETRY", "true")
    assert _end_of_run_retry_enabled() is True


def test_delay_default_zero(monkeypatch):
    monkeypatch.delenv("END_OF_RUN_RETRY_DELAY_S", raising=False)
    assert _end_of_run_retry_delay_s() == 0


def test_delay_parsed(monkeypatch):
    monkeypatch.setenv("END_OF_RUN_RETRY_DELAY_S", "300")
    assert _end_of_run_retry_delay_s() == 300


def test_delay_invalid_falls_back_to_zero(monkeypatch):
    monkeypatch.setenv("END_OF_RUN_RETRY_DELAY_S", "abc")
    assert _end_of_run_retry_delay_s() == 0
