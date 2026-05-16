"""Tests for the wedge-rescue retry pass in jugnu_runner.

Added 2026-05-16 as the shard_84 fix. When a property's RENDER-mode
fetch times out or wedges (CANCELLED / partial_recovery), the runner
enqueues an HTTP_ONLY retry that bypasses Playwright IPC entirely.
This is what turns the 32 wedged shard_84 PIDs from "next-run wedge
again" into "next-run extract successfully via static HTML / JSON-LD".

These tests grep-assert the retry-pass scaffolding is present and
correctly wired. A full end-to-end test would require the AsyncPool
+ scheduler + FS state — beyond the value of these guard tests.
"""
from __future__ import annotations

import inspect


def test_wedge_rescue_retry_pass_present_in_run_jugnu() -> None:
    """The retry pass must exist inside run_jugnu, between the main
    pool.map call and the state-store save."""
    from ma_poc.scripts.runners import jugnu

    src = inspect.getsource(jugnu)
    assert "Wedge-rescue retry pass" in src, (
        "Wedge-rescue retry pass missing from run_jugnu. Without it, "
        "wedged properties have no path to recovery and will re-wedge "
        "next run."
    )


def test_retry_uses_http_only_render_mode() -> None:
    """Retries MUST use RenderMode.GET. The whole point is to bypass
    the Playwright IPC channel that wedged on the original attempt;
    a RENDER-mode retry would just re-wedge."""
    from ma_poc.scripts.runners import jugnu

    src = inspect.getsource(jugnu)
    # The retry-task construction block must reference RenderMode.GET.
    # If a future change accidentally uses RENDER, this test fails loudly.
    assert "_RenderMode.GET" in src or "RenderMode.GET" in src, (
        "Retry pass not using RenderMode.GET — it would re-wedge on "
        "the same Playwright IPC pathology."
    )


def test_retry_only_when_no_partial_units() -> None:
    """If partial recovery saved units > 0, we already have data — the
    retry should be skipped so HTTP_ONLY doesn't overwrite real
    partial data with an empty static-HTML fetch."""
    from ma_poc.scripts.runners import jugnu

    src = inspect.getsource(jugnu)
    # The candidate-selection block should explicitly check `_has_units`
    # and skip retrying when truthy.
    assert "_has_units" in src
    assert "not _has_units" in src or "_has_units is False" in src, (
        "Retry pass not gated on absence of partial units. Risk: an "
        "HTTP_ONLY retry with empty body overwrites valid partial-"
        "recovery data."
    )


def test_retry_uses_reason_RETRY() -> None:
    """Retry tasks should be tagged TaskReason.RETRY so frontier /
    DLQ / event logs can distinguish them from scheduled fetches."""
    from ma_poc.scripts.runners import jugnu

    src = inspect.getsource(jugnu)
    assert "_TaskReason.RETRY" in src or "TaskReason.RETRY" in src


def test_retry_uses_shorter_budget() -> None:
    """The retry budget should be shorter than the per-property 600s
    wallclock — a wedge-rescue retry has no business burning the full
    budget. Currently 90s."""
    from ma_poc.scripts.runners import jugnu

    src = inspect.getsource(jugnu)
    # Look for the budget_ms=N within the retry block.
    import re
    block = re.search(
        r"Wedge-rescue[\s\S]{0,4000}?budget_ms\s*=\s*(\d[\d_]*)",
        src,
    )
    assert block is not None
    budget = int(block.group(1).replace("_", ""))
    assert 30_000 <= budget <= 180_000, (
        f"Retry budget {budget}ms outside the safe range [30s, 180s]. "
        "Document the tuning rationale before relaxing."
    )


def test_retry_pool_size_capped() -> None:
    """The retry AsyncPool should be sized to min(len(retry_tasks),
    pool_size) — never larger than the main pool, never larger than
    the actual work. Prevents over-allocation on small retry sets."""
    from ma_poc.scripts.runners import jugnu

    src = inspect.getsource(jugnu)
    assert "_retry_pool_size = min(len(_retry_tasks), pool_size)" in src


def test_retry_only_upgrades_on_success() -> None:
    """When the retry also fails, do NOT replace the original record.
    The original has partial-recovery state-store data we want to
    preserve for carry-forward."""
    from ma_poc.scripts.runners import jugnu

    src = inspect.getsource(jugnu)
    # The upgrade gate should require either units > 0 OR a SUCCESS
    # verdict from the retry.
    assert "_rr_has_units or _rr_v in" in src, (
        "Retry-result upgrade gate doesn't condition on success. "
        "Failing retries should not clobber the original partial "
        "record."
    )


def test_retry_stamps_wedge_rescue_applied() -> None:
    """When an upgrade happens, the new record should carry
    `_meta.wedge_rescue_applied = True` so downstream analysis (and
    SLO dashboards) can attribute successes to the rescue path."""
    from ma_poc.scripts.runners import jugnu

    src = inspect.getsource(jugnu)
    assert "wedge_rescue_applied" in src, (
        "Wedge-rescue stamp missing — no way to attribute next-run "
        "successes to the rescue path vs. organic success."
    )
