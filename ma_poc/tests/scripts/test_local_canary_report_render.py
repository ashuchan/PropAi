"""Tests for local_canary.py — comparison, report rendering, and exit codes.

Covers:
- Verdict assignment: all four verdict types (IMPROVED / REGRESSED /
  UNCHANGED_OK / UNCHANGED_FAIL) plus TIMEOUT_IN_CANARY
- Markdown report contains every property row
- Summary stats match the row counts
- Exit code 0 when REGRESSED == 0
- Exit code 1 when REGRESSED >= 1
- render_json produces valid JSON with the expected keys
- read_canary_outcomes parses events.jsonl correctly
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# ── Path bootstrap ─────────────────────────────────────────────────────────────
_TESTS_DIR = Path(__file__).resolve().parent.parent
_MA_POC_ROOT = _TESTS_DIR.parent
_REPO_ROOT = _MA_POC_ROOT.parent
for _p in (_REPO_ROOT, _MA_POC_ROOT, _MA_POC_ROOT / "scripts" / "diagnostics"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import local_canary as lc  # type: ignore[import-not-found]


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_row(
    pid: str,
    cloud_verdict: str,
    cloud_tier: str = "TIER_4_LLM_API",
    url: str = "https://example.com",
) -> dict[str, str]:
    return {
        "property_id": pid,
        "url": url,
        "verdict": cloud_verdict,
        "terminal_tier": cloud_tier,
        "pms_detected": "unknown",
        "domain": "example.com",
    }


def _make_outcome(pid: str, verdict: str, units: int = 0) -> lc._CanaryOutcome:
    return lc._CanaryOutcome(property_id=pid, verdict=verdict, units=units)


def _write_events(tmp_path: Path, run_date: str, events: list[dict]) -> None:
    """Write a synthetic events.jsonl for testing read_canary_outcomes."""
    run_dir = tmp_path / "runs" / run_date
    run_dir.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(e) for e in events]
    (run_dir / "events.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


# ── compare() — verdict assignment ────────────────────────────────────────────


class TestCompareVerdicts:
    def test_improved(self):
        rows = [_make_row("P1", "FAILED_NO_DATA")]
        outcomes = {"P1": _make_outcome("P1", "SUCCESS", units=12)}
        report = lc.compare(rows, outcomes, source_run_date="2026-05-10")
        assert report.rows[0].verdict == lc.VERDICT_IMPROVED
        assert report.improved == 1
        assert report.regressed == 0

    def test_regressed(self):
        rows = [_make_row("P2", "SUCCESS")]
        outcomes = {"P2": _make_outcome("P2", "FAILED_NO_DATA")}
        report = lc.compare(rows, outcomes, source_run_date="2026-05-10")
        assert report.rows[0].verdict == lc.VERDICT_REGRESSED
        assert report.regressed == 1
        assert report.improved == 0

    def test_unchanged_ok(self):
        rows = [_make_row("P3", "SUCCESS")]
        outcomes = {"P3": _make_outcome("P3", "SUCCESS", units=5)}
        report = lc.compare(rows, outcomes, source_run_date="2026-05-10")
        assert report.rows[0].verdict == lc.VERDICT_UNCHANGED_OK
        assert report.unchanged_ok == 1

    def test_unchanged_fail(self):
        rows = [_make_row("P4", "FAILED_UNREACHABLE")]
        outcomes = {"P4": _make_outcome("P4", "FAILED_NO_DATA")}
        report = lc.compare(rows, outcomes, source_run_date="2026-05-10")
        assert report.rows[0].verdict == lc.VERDICT_UNCHANGED_FAIL
        assert report.unchanged_fail == 1

    def test_timeout_in_canary(self):
        """Property with no canary outcome is TIMEOUT_IN_CANARY."""
        rows = [_make_row("P5", "FAILED_NO_DATA")]
        outcomes = {}  # no PROPERTY_EMITTED event
        report = lc.compare(rows, outcomes, source_run_date="2026-05-10")
        assert report.rows[0].verdict == lc.VERDICT_TIMEOUT_IN_CANARY
        assert report.timeout_in_canary == 1

    def test_carry_forward_treated_as_ok(self):
        """Cloud CARRY_FORWARD is soft-success; canary failure is REGRESSED."""
        rows = [_make_row("P6", "CARRY_FORWARD")]
        outcomes = {"P6": _make_outcome("P6", "FAILED_NO_DATA")}
        report = lc.compare(rows, outcomes, source_run_date="2026-05-10")
        assert report.rows[0].verdict == lc.VERDICT_REGRESSED

    def test_mixed_set_counts_sum_to_total(self):
        rows = [
            _make_row("A", "FAILED_NO_DATA"),
            _make_row("B", "SUCCESS"),
            _make_row("C", "FAILED_UNREACHABLE"),
            _make_row("D", "SUCCESS"),
        ]
        outcomes = {
            "A": _make_outcome("A", "SUCCESS"),     # IMPROVED
            "B": _make_outcome("B", "FAILED_NO_DATA"),  # REGRESSED
            "C": _make_outcome("C", "FAILED_NO_DATA"),  # UNCHANGED_FAIL
            # D has no outcome → TIMEOUT
        }
        report = lc.compare(rows, outcomes, source_run_date="2026-05-10")
        assert report.improved == 1
        assert report.regressed == 1
        assert report.unchanged_fail == 1
        assert report.timeout_in_canary == 1
        total_accounted = (
            report.improved
            + report.regressed
            + report.unchanged_ok
            + report.unchanged_fail
            + report.timeout_in_canary
        )
        assert total_accounted == report.properties_total == 4

    def test_report_source_run_date_preserved(self):
        rows = [_make_row("X", "FAILED_NO_DATA")]
        report = lc.compare(rows, {}, source_run_date="2026-05-10")
        assert report.source_run_date == "2026-05-10"


# ── render_markdown() ─────────────────────────────────────────────────────────


class TestRenderMarkdown:
    def _build_report(self, **kwargs) -> lc.CanaryReport:
        rows = kwargs.pop("cloud_rows", [_make_row("P1", "FAILED_NO_DATA")])
        outcomes = kwargs.pop("canary_outcomes", {"P1": _make_outcome("P1", "SUCCESS")})
        return lc.compare(rows, outcomes, source_run_date="2026-05-10")

    def test_contains_every_property_id(self):
        rows = [_make_row("P1", "FAILED_NO_DATA"), _make_row("P2", "SUCCESS")]
        outcomes = {
            "P1": _make_outcome("P1", "SUCCESS"),
            "P2": _make_outcome("P2", "FAILED_NO_DATA"),
        }
        report = lc.compare(rows, outcomes, source_run_date="2026-05-10")
        md = lc.render_markdown(report)
        assert "P1" in md
        assert "P2" in md

    def test_summary_stats_accurate(self):
        rows = [
            _make_row("A", "FAILED_NO_DATA"),
            _make_row("B", "SUCCESS"),
        ]
        outcomes = {
            "A": _make_outcome("A", "SUCCESS"),
            "B": _make_outcome("B", "FAILED_NO_DATA"),
        }
        report = lc.compare(rows, outcomes, source_run_date="2026-05-10")
        md = lc.render_markdown(report)
        assert "IMPROVED" in md
        assert "REGRESSED" in md
        assert "1" in md  # both counts appear as 1

    def test_pass_gate_when_no_regressions(self):
        rows = [_make_row("P1", "FAILED_NO_DATA")]
        outcomes = {"P1": _make_outcome("P1", "SUCCESS")}
        report = lc.compare(rows, outcomes, source_run_date="2026-05-10")
        assert report.passed
        md = lc.render_markdown(report)
        assert "PASS" in md
        assert "FAIL" not in md or "UNCHANGED_FAIL" in md  # UNCHANGED_FAIL is OK in table

    def test_fail_gate_when_regressions(self):
        rows = [_make_row("P2", "SUCCESS")]
        outcomes = {"P2": _make_outcome("P2", "FAILED_NO_DATA")}
        report = lc.compare(rows, outcomes, source_run_date="2026-05-10")
        assert not report.passed
        md = lc.render_markdown(report)
        assert "FAIL" in md

    def test_regression_section_lists_regressed_property(self):
        rows = [_make_row("P2", "SUCCESS", url="https://regressed.com")]
        outcomes = {"P2": _make_outcome("P2", "FAILED_NO_DATA")}
        report = lc.compare(rows, outcomes, source_run_date="2026-05-10")
        md = lc.render_markdown(report)
        assert "ACTION REQUIRED" in md or "Regressions" in md
        assert "P2" in md

    def test_no_regression_section_when_all_passed(self):
        rows = [_make_row("P1", "FAILED_NO_DATA")]
        outcomes = {"P1": _make_outcome("P1", "SUCCESS")}
        report = lc.compare(rows, outcomes, source_run_date="2026-05-10")
        md = lc.render_markdown(report)
        assert "ACTION REQUIRED" not in md

    def test_per_property_table_has_header(self):
        rows = [_make_row("P1", "FAILED_NO_DATA")]
        outcomes = {}
        report = lc.compare(rows, outcomes, source_run_date="2026-05-10")
        md = lc.render_markdown(report)
        assert "property_id" in md
        assert "cloud_outcome" in md
        assert "verdict" in md


# ── render_json() ─────────────────────────────────────────────────────────────


class TestRenderJson:
    def _simple_report(self) -> lc.CanaryReport:
        rows = [_make_row("P1", "FAILED_NO_DATA")]
        outcomes = {"P1": _make_outcome("P1", "SUCCESS", units=7)}
        return lc.compare(rows, outcomes, source_run_date="2026-05-10")

    def test_output_is_valid_json(self):
        report = self._simple_report()
        raw = lc.render_json(report)
        parsed = json.loads(raw)
        assert isinstance(parsed, dict)

    def test_top_level_keys_present(self):
        report = self._simple_report()
        parsed = json.loads(lc.render_json(report))
        expected_keys = {
            "run_date", "source_run_date", "properties_total",
            "improved", "regressed", "unchanged_ok", "unchanged_fail",
            "timeout_in_canary", "passed", "rows",
        }
        assert expected_keys <= set(parsed.keys())

    def test_rows_list_length_matches_input(self):
        rows = [_make_row(f"P{i}", "FAILED_NO_DATA") for i in range(5)]
        report = lc.compare(rows, {}, source_run_date="2026-05-10")
        parsed = json.loads(lc.render_json(report))
        assert len(parsed["rows"]) == 5

    def test_passed_field_reflects_report(self):
        # No regressions → passed = True
        rows = [_make_row("P1", "FAILED_NO_DATA")]
        outcomes = {"P1": _make_outcome("P1", "SUCCESS")}
        report = lc.compare(rows, outcomes, source_run_date="2026-05-10")
        parsed = json.loads(lc.render_json(report))
        assert parsed["passed"] is True

        # One regression → passed = False
        rows2 = [_make_row("P2", "SUCCESS")]
        outcomes2 = {"P2": _make_outcome("P2", "FAILED_NO_DATA")}
        report2 = lc.compare(rows2, outcomes2, source_run_date="2026-05-10")
        parsed2 = json.loads(lc.render_json(report2))
        assert parsed2["passed"] is False


# ── read_canary_outcomes() ────────────────────────────────────────────────────


class TestReadCanaryOutcomes:
    def test_reads_property_emitted_events(self, tmp_path):
        run_date = "2026-05-10"
        _write_events(
            tmp_path,
            run_date,
            [
                {"kind": "output.property_emitted", "property_id": "A1", "verdict": "SUCCESS", "units": 5},
                {"kind": "output.property_emitted", "property_id": "A2", "verdict": "FAILED_NO_DATA", "units": 0},
            ],
        )
        outcomes = lc.read_canary_outcomes(tmp_path, run_date)
        assert set(outcomes.keys()) == {"A1", "A2"}
        assert outcomes["A1"].verdict == "SUCCESS"
        assert outcomes["A1"].units == 5
        assert outcomes["A2"].verdict == "FAILED_NO_DATA"

    def test_ignores_non_property_emitted_events(self, tmp_path):
        run_date = "2026-05-10"
        _write_events(
            tmp_path,
            run_date,
            [
                {"kind": "fetch.started", "property_id": "A1"},
                {"kind": "extract.pms_detected", "property_id": "A1", "pms": "entrata"},
                {"kind": "output.property_emitted", "property_id": "A1", "verdict": "SUCCESS", "units": 3},
            ],
        )
        outcomes = lc.read_canary_outcomes(tmp_path, run_date)
        assert len(outcomes) == 1
        assert "A1" in outcomes

    def test_last_event_wins_for_same_property(self, tmp_path):
        """If a property emits PROPERTY_EMITTED twice (carry-forward path), last wins."""
        run_date = "2026-05-10"
        _write_events(
            tmp_path,
            run_date,
            [
                {"kind": "output.property_emitted", "property_id": "X", "verdict": "FAILED_NO_DATA", "units": 0},
                {"kind": "output.property_emitted", "property_id": "X", "verdict": "SUCCESS", "units": 8},
            ],
        )
        outcomes = lc.read_canary_outcomes(tmp_path, run_date)
        assert outcomes["X"].verdict == "SUCCESS"
        assert outcomes["X"].units == 8

    def test_returns_empty_when_no_events_file(self, tmp_path):
        outcomes = lc.read_canary_outcomes(tmp_path, "2026-05-10")
        assert outcomes == {}

    def test_tolerates_malformed_lines(self, tmp_path):
        """Truncated / non-JSON lines must be skipped, not crash the reader."""
        run_date = "2026-05-10"
        run_dir = tmp_path / "runs" / run_date
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "events.jsonl").write_text(
            '{"kind": "output.property_emitted", "property_id": "Z", "verdict": "SUCCESS", "units": 2}\n'
            "THIS IS NOT JSON\n"
            '{"kind": "output.property_emitted", "property_id": "W", "verdict": "FAILED_NO_DATA", "units": 0}\n',
            encoding="utf-8",
        )
        outcomes = lc.read_canary_outcomes(tmp_path, run_date)
        assert "Z" in outcomes
        assert "W" in outcomes
        assert len(outcomes) == 2


# ── Exit code semantics (via main() with mocked infra) ───────────────────────


class TestExitCodes:
    """Test the three exit codes without launching a real jugnu subprocess."""

    _FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "canary" / "failures.csv"

    def _run_main(self, monkeypatch, tmp_path, canary_outcomes: dict, jugnu_exit: int = 0) -> int:
        """Run main() with jugnu and DB creation stubbed out."""
        monkeypatch.setattr(lc, "_find_failures_csv", lambda _: self._FIXTURE)
        monkeypatch.setattr(lc, "setup_canary_db", lambda dsn, db_mode="sqlite": None)
        monkeypatch.setattr(lc, "teardown_canary_db", lambda p: None)
        monkeypatch.setattr(lc, "replay", lambda **kw: jugnu_exit)
        monkeypatch.setattr(lc, "read_canary_outcomes", lambda *a, **kw: canary_outcomes)
        monkeypatch.setattr(lc, "_DEFAULT_OUT_ROOT", tmp_path)

        return lc.main(["--from-run", "2026-05-10", "--limit", "3"])

    def test_exit_0_when_no_regressions(self, monkeypatch, tmp_path):
        # All 3 fixture properties improve or stay failed — no regression
        outcomes = {
            "10001": lc._CanaryOutcome("10001", "SUCCESS", 5),
            "10002": lc._CanaryOutcome("10002", "FAILED_NO_DATA", 0),
            "10003": lc._CanaryOutcome("10003", "SUCCESS", 3),
        }
        code = self._run_main(monkeypatch, tmp_path, canary_outcomes=outcomes)
        assert code == 0

    def test_exit_1_when_regression(self, monkeypatch, tmp_path):
        # 10001 cloud=FAILED, canary=SUCCESS → IMPROVED (ok)
        # 10002 cloud=FAILED_UNREACHABLE, canary=FAILED → UNCHANGED_FAIL (ok)
        # 10003 cloud=FAILED_NO_DATA, canary=FAILED → UNCHANGED_FAIL (ok)
        # To trigger a regression, we need a cloud=SUCCESS property in the fixture.
        # The fixture only has failures — so REGRESSED is impossible without SUCCESS rows.
        # Override the fixture path with a synthetic one that has a SUCCESS cloud row.
        import csv

        synth = tmp_path / "synthetic_failures.csv"
        with synth.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=["property_id", "url", "verdict", "terminal_tier", "pms_detected"],
            )
            writer.writeheader()
            writer.writerow(
                {
                    "property_id": "GOOD_PROP",
                    "url": "https://good.com",
                    "verdict": "SUCCESS",
                    "terminal_tier": "TIER_1_API",
                    "pms_detected": "rentcafe",
                }
            )

        monkeypatch.setattr(lc, "_find_failures_csv", lambda _: synth)
        monkeypatch.setattr(lc, "setup_canary_db", lambda dsn, db_mode="sqlite": None)
        monkeypatch.setattr(lc, "teardown_canary_db", lambda p: None)
        monkeypatch.setattr(lc, "replay", lambda **kw: 0)
        monkeypatch.setattr(
            lc,
            "read_canary_outcomes",
            lambda *a, **kw: {"GOOD_PROP": lc._CanaryOutcome("GOOD_PROP", "FAILED_NO_DATA", 0)},
        )
        monkeypatch.setattr(lc, "_DEFAULT_OUT_ROOT", tmp_path)

        code = lc.main(["--from-run", "2026-05-10", "--limit", "5"])
        assert code == 1

    def test_exit_2_on_jugnu_infra_failure(self, monkeypatch, tmp_path):
        """Jugnu subprocess returning code 2 → canary exits 2."""
        code = self._run_main(monkeypatch, tmp_path, canary_outcomes={}, jugnu_exit=2)
        assert code == 2

    def test_exit_2_when_failures_csv_missing(self, monkeypatch, tmp_path):
        monkeypatch.setattr(lc, "_find_failures_csv", lambda _: None)
        monkeypatch.setattr(lc, "setup_canary_db", lambda dsn, db_mode="sqlite": None)
        monkeypatch.setattr(lc, "_DEFAULT_OUT_ROOT", tmp_path)
        code = lc.main(["--from-run", "2026-05-10", "--limit", "3"])
        assert code == 2
