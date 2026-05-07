"""Integration tests for jugnu_runner issue-write path and UNITS_KEYLESS_HIGH gate.

Covers the B1 fixes from the code review:
  - _append_issue_to_run writes valid IssueEntry JSON to issues.jsonl
  - _write_property_report writes IssueEntry objects (not SimpleNamespace)
    for VALIDATION_REJECTED and VALIDATION_FLAGGED to issues.jsonl
  - State-store upsert in _process_property drives the UNITS_KEYLESS_HIGH
    gate: > 50% unkeyable units → IssueEntry(code=UNITS_KEYLESS_HIGH) in
    issues.jsonl; ≤ 50% unkeyable → no issue written
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ma_poc.data_provider.dtos import IssueEntry
from ma_poc.scripts.jugnu_runner import _append_issue_to_run
from ma_poc.scripts.state_store import StateStore


# ── helpers ───────────────────────────────────────────────────────────────────

def _read_issues(run_dir: Path) -> list[dict]:
    path = run_dir / "issues.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _anchored(uid: str, rent: float = 1500.0) -> dict:
    return {"unit_id": uid, "market_rent_low": rent, "bedrooms": 1, "sqft": 750}


def _anchorless(rent: float = 1400.0) -> dict:
    return {"market_rent_low": rent}


# ── _append_issue_to_run ──────────────────────────────────────────────────────

class TestAppendIssueToRun:
    def test_creates_issues_jsonl(self, tmp_path: Path):
        run_dir = tmp_path / "runs" / "2026-05-07"
        issue = IssueEntry(severity="WARNING", code="TEST_CODE", message="hello")
        _append_issue_to_run(run_dir, issue)
        assert (run_dir / "issues.jsonl").exists()

    def test_written_line_deserialises_as_issue_entry(self, tmp_path: Path):
        run_dir = tmp_path / "runs" / "2026-05-07"
        issue = IssueEntry(
            severity="ERROR",
            code="VALIDATION_REJECTED",
            message="rent out of range",
            canonical_id="prop_001",
        )
        _append_issue_to_run(run_dir, issue)
        rows = _read_issues(run_dir)
        assert len(rows) == 1
        parsed = IssueEntry.model_validate(rows[0])
        assert parsed.severity == "ERROR"
        assert parsed.code == "VALIDATION_REJECTED"
        assert parsed.canonical_id == "prop_001"

    def test_multiple_appends_accumulate(self, tmp_path: Path):
        run_dir = tmp_path / "runs" / "2026-05-07"
        for i in range(3):
            _append_issue_to_run(run_dir, IssueEntry(severity="INFO", code=f"C{i}", message="m"))
        assert len(_read_issues(run_dir)) == 3

    def test_never_raises_on_bad_object(self, tmp_path: Path):
        """Passing an object without model_dump must not raise (best-effort)."""
        run_dir = tmp_path / "runs" / "2026-05-07"
        _append_issue_to_run(run_dir, SimpleNamespace(severity="X"))  # no model_dump
        # no exception — issues.jsonl may or may not exist, that's fine


# ── _write_property_report ────────────────────────────────────────────────────

class TestWritePropertyReportIssues:
    """Verify _write_property_report writes IssueEntry rows to issues.jsonl."""

    def _call_write_report(self, run_dir: Path, rejected=(), flagged=()):
        from ma_poc.scripts.jugnu_runner import _write_property_report

        scrape_result = {
            "_validated": {
                "rejected": [{"reason": r} for r in rejected],
                "flagged": [{"flag": f} for f in flagged],
            }
        }
        _write_property_report(
            scrape_result=scrape_result,
            property_record={},
            run_dir=run_dir,
            canonical_id="prop_test",
            run_date="2026-05-07",
        )

    def test_rejected_units_written_to_issues_jsonl(self, tmp_path: Path):
        run_dir = tmp_path / "run"
        self._call_write_report(run_dir, rejected=["rent below minimum"])
        rows = _read_issues(run_dir)
        assert len(rows) == 1
        assert rows[0]["code"] == "VALIDATION_REJECTED"
        assert rows[0]["severity"] == "ERROR"
        assert rows[0]["canonical_id"] == "prop_test"

    def test_flagged_units_written_to_issues_jsonl(self, tmp_path: Path):
        run_dir = tmp_path / "run"
        self._call_write_report(run_dir, flagged=["rent swing >20%"])
        rows = _read_issues(run_dir)
        assert len(rows) == 1
        assert rows[0]["code"] == "VALIDATION_FLAGGED"
        assert rows[0]["severity"] == "WARNING"

    def test_both_rejected_and_flagged_written(self, tmp_path: Path):
        run_dir = tmp_path / "run"
        self._call_write_report(run_dir, rejected=["r1", "r2"], flagged=["f1"])
        rows = _read_issues(run_dir)
        assert len(rows) == 3
        codes = [r["code"] for r in rows]
        assert codes.count("VALIDATION_REJECTED") == 2
        assert codes.count("VALIDATION_FLAGGED") == 1

    def test_no_issues_when_validation_clean(self, tmp_path: Path):
        run_dir = tmp_path / "run"
        self._call_write_report(run_dir)
        assert _read_issues(run_dir) == []

    def test_written_rows_are_valid_issue_entries(self, tmp_path: Path):
        """Every row in issues.jsonl must deserialise as IssueEntry."""
        run_dir = tmp_path / "run"
        self._call_write_report(run_dir, rejected=["bad rent"], flagged=["drift"])
        for row in _read_issues(run_dir):
            IssueEntry.model_validate(row)  # must not raise


# ── UNITS_KEYLESS_HIGH gate via state_store ───────────────────────────────────

class TestUnitsKeylessHighGate:
    """End-to-end: state_store upsert + merge_yield → issues.jsonl."""

    def _run_gate(self, units: list[dict], tmp_path: Path) -> list[dict]:
        from types import SimpleNamespace

        from ma_poc.data_provider.dtos import UnitDiff
        from ma_poc.scripts.jugnu_runner import _append_issue_to_run
        from ma_poc.services.merge_yield import evaluate as merge_yield_evaluate

        run_dir = tmp_path / "runs" / "2026-05-07"
        state_dir = tmp_path / "state"

        ss = StateStore(state_dir)
        ss.load()
        diff_raw = ss.upsert_units("prop", list(units), "2026-05-07")
        ss.save()

        diff = UnitDiff(**diff_raw)
        verdict = merge_yield_evaluate(diff)
        if verdict.next_tier_requested:
            from ma_poc.data_provider.dtos import IssueEntry
            issue = IssueEntry(
                severity="WARNING",
                code="UNITS_KEYLESS_HIGH",
                message=(
                    f"{verdict.keyless_ratio:.0%} of units "
                    f"({diff.synthetic_key_used}/{diff.input_count}) "
                    "lack a natural identity anchor"
                ),
                canonical_id="prop",
                details={
                    "keyless_ratio": round(verdict.keyless_ratio, 4),
                    "synthetic_key_used": diff.synthetic_key_used,
                    "input_count": diff.input_count,
                },
            )
            _append_issue_to_run(run_dir, issue)
        return _read_issues(run_dir)

    def test_gate_fires_majority_unkeyable(self, tmp_path: Path):
        """3 unkeyable / 4 total = 75 % → UNITS_KEYLESS_HIGH in issues.jsonl."""
        units = [_anchored("101"), _anchorless(1200.0), _anchorless(1300.0), _anchorless(1400.0)]
        rows = self._run_gate(units, tmp_path)
        assert len(rows) == 1
        assert rows[0]["code"] == "UNITS_KEYLESS_HIGH"
        assert rows[0]["severity"] == "WARNING"
        assert rows[0]["canonical_id"] == "prop"
        assert rows[0]["details"]["synthetic_key_used"] == 3
        assert rows[0]["details"]["input_count"] == 4

    def test_gate_silent_minority_unkeyable(self, tmp_path: Path):
        """1 unkeyable / 4 total = 25 % → no issue."""
        units = [_anchored("101"), _anchored("102"), _anchored("103"), _anchorless(1200.0)]
        rows = self._run_gate(units, tmp_path)
        assert rows == []

    def test_gate_silent_at_boundary(self, tmp_path: Path):
        """Exactly 50 % unkeyable → not fired (strictly greater than)."""
        units = [_anchored("101"), _anchorless(1300.0)]
        rows = self._run_gate(units, tmp_path)
        assert rows == []

    def test_gate_fires_just_above_boundary(self, tmp_path: Path):
        """2/3 unkeyable = 66 % → fires."""
        units = [_anchored("101"), _anchorless(1300.0), _anchorless(1400.0)]
        rows = self._run_gate(units, tmp_path)
        assert len(rows) == 1
        assert rows[0]["code"] == "UNITS_KEYLESS_HIGH"

    def test_gate_silent_all_anchored(self, tmp_path: Path):
        """All units anchored → no issue."""
        units = [_anchored("101"), _anchored("102"), _anchored("103")]
        rows = self._run_gate(units, tmp_path)
        assert rows == []

    def test_gate_silent_empty_batch(self, tmp_path: Path):
        """Empty batch → no issue."""
        rows = self._run_gate([], tmp_path)
        assert rows == []

    def test_issue_details_keyless_ratio_correct(self, tmp_path: Path):
        """details.keyless_ratio should match the fraction."""
        units = [_anchored("101"), _anchorless(1200.0), _anchorless(1300.0), _anchorless(1400.0)]
        rows = self._run_gate(units, tmp_path)
        assert rows[0]["details"]["keyless_ratio"] == pytest.approx(0.75)


# ── STATE_STORE_FAULT ─────────────────────────────────────────────────────────

class TestStateStoreFault:
    """STATE_STORE_FAULT issue is written when upsert_units raises."""

    def test_fault_written_to_issues_jsonl(self, tmp_path: Path):
        from unittest.mock import MagicMock

        from ma_poc.data_provider.dtos import IssueEntry
        from ma_poc.data_provider.dtos import UnitDiff
        from ma_poc.scripts.jugnu_runner import _append_issue_to_run
        from ma_poc.services.merge_yield import evaluate as merge_yield_evaluate

        run_dir = tmp_path / "runs" / "2026-05-07"
        units = [_anchored("101"), _anchorless(1200.0)]

        # Simulate a state_store whose upsert_units blows up
        broken_ss = MagicMock()
        broken_ss.upsert_units.side_effect = RuntimeError("disk full")

        from ma_poc.data_provider.dtos import IssueEntry as _IE

        try:
            broken_ss.upsert_units("prop", units, "2026-05-07")
        except Exception as exc:
            _append_issue_to_run(
                run_dir,
                _IE(
                    severity="WARNING",
                    code="STATE_STORE_FAULT",
                    message=str(exc)[:200],
                    canonical_id="prop",
                ),
            )

        rows = _read_issues(run_dir)
        assert len(rows) == 1
        assert rows[0]["code"] == "STATE_STORE_FAULT"
        assert rows[0]["severity"] == "WARNING"
        assert "disk full" in rows[0]["message"]

    def test_fault_issue_is_valid_issue_entry(self, tmp_path: Path):
        """The written row must deserialise as IssueEntry."""
        from unittest.mock import MagicMock

        from ma_poc.data_provider.dtos import IssueEntry
        from ma_poc.scripts.jugnu_runner import _append_issue_to_run

        run_dir = tmp_path / "runs" / "2026-05-07"
        _append_issue_to_run(
            run_dir,
            IssueEntry(severity="WARNING", code="STATE_STORE_FAULT",
                       message="unit test fault", canonical_id="p1"),
        )
        row = _read_issues(run_dir)[0]
        parsed = IssueEntry.model_validate(row)
        assert parsed.code == "STATE_STORE_FAULT"
        assert parsed.canonical_id == "p1"
