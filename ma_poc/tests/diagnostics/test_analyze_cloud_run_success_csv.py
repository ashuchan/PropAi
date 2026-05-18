"""Regression guards for ``analyze_cloud_run.render_successes_csv``.

A local ``_SUCCESS_VERDICTS = {"SUCCESS", "CARRY_FORWARD"}`` set inside
this function used to shadow the canonical module-level constant on
line 82 (which includes ``SUCCESS_PLAN_LEVEL`` and ``SUCCESS_PARTIAL``).
Properties with the newer verdict classes were silently dropped from
successes.csv — verified on 2026-05-18 with PID 17102
(irvinecompanyapartments.com, verdict=SUCCESS_PLAN_LEVEL) absent from
both successes.csv and failures.csv.

These tests pin the contract so any future allowlist-style drift fails
fast at CI rather than at the next canary regression-basket sampling.
"""

from __future__ import annotations

import csv
from pathlib import Path

from ma_poc.scripts.diagnostics.analyze_cloud_run import (
    PropertyOutcome,
    render_successes_csv,
)


def _make_outcome(verdict: str, *, property_id: str = "P1", units: int = 0) -> PropertyOutcome:
    """Build a minimal PropertyOutcome with the verdict field populated.

    ``render_successes_csv`` reads many fields; default zero/None values
    are fine because the tests only assert which PIDs end up in the
    output. Setting ``url`` is needed because the rendered row carries a
    domain column derived from urlparse.
    """
    return PropertyOutcome(
        property_id=property_id,
        shard="shard_0",
        url="https://example.com/p",
        verdict=verdict,
        units=units,
    )


def _read_pid_column(path: Path) -> list[str]:
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    return [r["property_id"] for r in rows]


def test_success_csv_includes_plain_success(tmp_path: Path) -> None:
    out = tmp_path / "successes.csv"
    outcomes = {"P1": _make_outcome("SUCCESS")}
    render_successes_csv(outcomes, out)
    assert _read_pid_column(out) == ["P1"]


def test_success_csv_includes_success_plan_level(tmp_path: Path) -> None:
    """SUCCESS_PLAN_LEVEL was the verdict silently dropped on 2026-05-18.

    This is the direct regression guard — if a future contributor
    reintroduces a local allowlist that misses this verdict, this test
    fails before the canary basket gets sampled.
    """
    out = tmp_path / "successes.csv"
    outcomes = {"P1": _make_outcome("SUCCESS_PLAN_LEVEL")}
    render_successes_csv(outcomes, out)
    assert _read_pid_column(out) == ["P1"]


def test_success_csv_includes_success_partial(tmp_path: Path) -> None:
    """SUCCESS_PARTIAL is the timeout-rescue success verdict added 2026-05-17.

    Same drop-class as SUCCESS_PLAN_LEVEL — both must reach the CSV.
    """
    out = tmp_path / "successes.csv"
    outcomes = {"P1": _make_outcome("SUCCESS_PARTIAL", units=12)}
    render_successes_csv(outcomes, out)
    assert _read_pid_column(out) == ["P1"]


def test_success_csv_includes_carry_forward(tmp_path: Path) -> None:
    """CARRY_FORWARD is already on the success list per
    ``reporting.verdict._SUCCESS_VERDICTS``. Don't regress it."""
    out = tmp_path / "successes.csv"
    outcomes = {"P1": _make_outcome("CARRY_FORWARD")}
    render_successes_csv(outcomes, out)
    assert _read_pid_column(out) == ["P1"]


def test_success_csv_excludes_failure_verdicts(tmp_path: Path) -> None:
    """Failure verdicts must NOT leak into successes.csv even when
    fields like ``units`` are non-zero (post-extraction partial drop)."""
    out = tmp_path / "successes.csv"
    outcomes = {
        "F1": _make_outcome("FAILED_NO_DATA"),
        "F2": _make_outcome("FAILED_UNREACHABLE", units=3),
        "F3": _make_outcome("DEAD_URL"),
        "F4": _make_outcome("PARTIAL", units=5),
    }
    render_successes_csv(outcomes, out)
    assert _read_pid_column(out) == []


def test_success_csv_mixed_population(tmp_path: Path) -> None:
    """End-to-end: a realistic mix of verdicts emits exactly the success
    bucket, sorted by terminal_tier / pms_detected / property_id per the
    function's own sort key."""
    out = tmp_path / "successes.csv"
    outcomes = {
        "P1": _make_outcome("SUCCESS", property_id="P1"),
        "P2": _make_outcome("SUCCESS_PLAN_LEVEL", property_id="P2"),
        "P3": _make_outcome("SUCCESS_PARTIAL", property_id="P3", units=8),
        "P4": _make_outcome("CARRY_FORWARD", property_id="P4"),
        "F1": _make_outcome("FAILED_NO_DATA", property_id="F1"),
        "F2": _make_outcome("FAILED_UNREACHABLE", property_id="F2"),
        "F3": _make_outcome("PARTIAL", property_id="F3", units=2),
        "F4": _make_outcome("DEAD_URL", property_id="F4"),
    }
    render_successes_csv(outcomes, out)
    assert sorted(_read_pid_column(out)) == ["P1", "P2", "P3", "P4"]


def test_success_csv_writes_empty_file_when_no_successes(tmp_path: Path) -> None:
    """No successes → empty file (header-less). This is the existing
    contract — render_successes_csv writes the empty string when rows is
    empty. The test pins it so a refactor doesn't accidentally start
    writing a stray header line."""
    out = tmp_path / "successes.csv"
    outcomes = {"F1": _make_outcome("FAILED_NO_DATA")}
    render_successes_csv(outcomes, out)
    assert out.read_text(encoding="utf-8") == ""
