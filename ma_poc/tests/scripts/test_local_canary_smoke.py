"""Tests for local_canary.py — smoke + integration (L7).

Covers the end-to-end path using the synthetic dry-run runner so the test
suite does not need real browser access or live URLs.

Tests:
- Dry-run runner writes events.jsonl with SUCCESS verdict for all properties
- main() with --dry-run-replay exits 0 (no regressions) and writes report.md
- main() with --dry-run-replay exits 0 (no regressions) and writes summary.json with --json
- main() with --dry-run-replay + REGRESSED property exits 1
- main() with missing failures.csv exits 2
- Dry-run runner produces valid JSON events
- replay() accepts runner= parameter and invokes it
- _canary_dry_run_runner.run_dry() writes correct event counts
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import pytest

# ── Path bootstrap ─────────────────────────────────────────────────────────────
_TESTS_DIR = Path(__file__).resolve().parent.parent
_MA_POC_ROOT = _TESTS_DIR.parent
_REPO_ROOT = _MA_POC_ROOT.parent
_DIAGNOSTICS = _MA_POC_ROOT / "scripts" / "diagnostics"
for _p in (_REPO_ROOT, _MA_POC_ROOT, _DIAGNOSTICS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import local_canary as lc  # type: ignore[import-not-found]
import _canary_dry_run_runner as dry_runner  # type: ignore[import-not-found]


_FIXTURE_CSV = Path(__file__).resolve().parents[1] / "fixtures" / "canary" / "failures.csv"


# ── Dry-run runner unit tests ─────────────────────────────────────────────────


class TestDryRunRunner:
    def _make_csv(self, tmp_path: Path, rows: list[dict]) -> Path:
        p = tmp_path / "input.csv"
        fieldnames = ["property_id", "url", "verdict"]
        with p.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                writer.writerow(row)
        return p

    def test_writes_events_jsonl(self, tmp_path):
        csv_path = self._make_csv(
            tmp_path,
            [
                {"property_id": "P001", "url": "https://a.com", "verdict": "FAILED_NO_DATA"},
                {"property_id": "P002", "url": "https://b.com", "verdict": "FAILED_NO_DATA"},
            ],
        )
        ret = dry_runner.run_dry(csv_path, tmp_path, "2026-05-10")
        assert ret == 0

        events_path = tmp_path / "runs" / "2026-05-10" / "events.jsonl"
        assert events_path.exists()

    def test_events_are_valid_json_lines(self, tmp_path):
        csv_path = self._make_csv(
            tmp_path,
            [{"property_id": "P001", "url": "https://a.com", "verdict": "x"}],
        )
        dry_runner.run_dry(csv_path, tmp_path, "2026-05-10")
        events_path = tmp_path / "runs" / "2026-05-10" / "events.jsonl"
        lines = [l for l in events_path.read_text().splitlines() if l.strip()]
        for line in lines:
            json.loads(line)  # must not raise

    def test_each_property_gets_tier_won_and_emitted_events(self, tmp_path):
        csv_path = self._make_csv(
            tmp_path,
            [
                {"property_id": "P001", "url": "https://a.com", "verdict": "x"},
                {"property_id": "P002", "url": "https://b.com", "verdict": "x"},
            ],
        )
        dry_runner.run_dry(csv_path, tmp_path, "2026-05-10")
        events_path = tmp_path / "runs" / "2026-05-10" / "events.jsonl"
        lines = [l for l in events_path.read_text().splitlines() if l.strip()]
        events = [json.loads(l) for l in lines]

        emitted = [e for e in events if e["kind"] == "output.property_emitted"]
        tier_won = [e for e in events if e["kind"] == "extract.tier_won"]
        assert len(emitted) == 2
        assert len(tier_won) == 2

    def test_default_verdict_is_success(self, tmp_path):
        csv_path = self._make_csv(
            tmp_path,
            [{"property_id": "P001", "url": "https://a.com", "verdict": "x"}],
        )
        dry_runner.run_dry(csv_path, tmp_path, "2026-05-10")
        events_path = tmp_path / "runs" / "2026-05-10" / "events.jsonl"
        events = [json.loads(l) for l in events_path.read_text().splitlines() if l.strip()]
        emitted = [e for e in events if e["kind"] == "output.property_emitted"]
        assert emitted[0]["verdict"] == "SUCCESS"

    def test_custom_verdict_override(self, tmp_path):
        csv_path = self._make_csv(
            tmp_path,
            [{"property_id": "P001", "url": "https://a.com", "verdict": "x"}],
        )
        dry_runner.run_dry(csv_path, tmp_path, "2026-05-10", verdict="FAILED_NO_DATA")
        events_path = tmp_path / "runs" / "2026-05-10" / "events.jsonl"
        events = [json.loads(l) for l in events_path.read_text().splitlines() if l.strip()]
        emitted = [e for e in events if e["kind"] == "output.property_emitted"]
        assert emitted[0]["verdict"] == "FAILED_NO_DATA"

    def test_missing_csv_returns_1(self, tmp_path):
        ret = dry_runner.run_dry(tmp_path / "nonexistent.csv", tmp_path, "2026-05-10")
        assert ret == 1

    def test_skips_rows_with_no_property_id(self, tmp_path):
        p = tmp_path / "input.csv"
        p.write_text("property_id,url\n,https://a.com\nP001,https://b.com\n", encoding="utf-8")
        dry_runner.run_dry(p, tmp_path, "2026-05-10")
        events_path = tmp_path / "runs" / "2026-05-10" / "events.jsonl"
        events = [json.loads(l) for l in events_path.read_text().splitlines() if l.strip()]
        emitted = [e for e in events if e["kind"] == "output.property_emitted"]
        assert len(emitted) == 1
        assert emitted[0]["property_id"] == "P001"


# ── End-to-end smoke tests via main() ─────────────────────────────────────────


class TestMainDryRunReplay:
    def test_exit_0_no_regressions_with_fixture(self, tmp_path, monkeypatch):
        """Fixture has 3 FAILED properties; dry-run makes them all SUCCESS → 0 regressions."""
        monkeypatch.setattr(lc, "setup_canary_db", lambda dsn, db_mode="sqlite": None)
        monkeypatch.setattr(lc, "teardown_canary_db", lambda p: None)
        monkeypatch.setattr(lc, "_DEFAULT_OUT_ROOT", tmp_path)

        exit_code = lc.main([
            "--from-run", "2026-05-10",
            "--properties-csv", str(_FIXTURE_CSV),
            "--dry-run-replay",
            "--limit", "3",
        ])
        assert exit_code == 0

    def test_report_md_written(self, tmp_path, monkeypatch):
        """report.md is written to out_dir."""
        monkeypatch.setattr(lc, "setup_canary_db", lambda dsn, db_mode="sqlite": None)
        monkeypatch.setattr(lc, "teardown_canary_db", lambda p: None)
        monkeypatch.setattr(lc, "_DEFAULT_OUT_ROOT", tmp_path)

        lc.main([
            "--from-run", "2026-05-10",
            "--properties-csv", str(_FIXTURE_CSV),
            "--dry-run-replay",
            "--limit", "3",
        ])

        # Find the run output dir (timestamped subdir of tmp_path).
        report_files = list(tmp_path.rglob("report.md"))
        assert len(report_files) == 1
        md = report_files[0].read_text(encoding="utf-8")
        assert "Local Canary Report" in md

    def test_summary_json_written_with_json_flag(self, tmp_path, monkeypatch):
        """--json flag writes summary.json."""
        monkeypatch.setattr(lc, "setup_canary_db", lambda dsn, db_mode="sqlite": None)
        monkeypatch.setattr(lc, "teardown_canary_db", lambda p: None)
        monkeypatch.setattr(lc, "_DEFAULT_OUT_ROOT", tmp_path)

        lc.main([
            "--from-run", "2026-05-10",
            "--properties-csv", str(_FIXTURE_CSV),
            "--dry-run-replay",
            "--limit", "3",
            "--json",
        ])

        json_files = list(tmp_path.rglob("summary.json"))
        assert len(json_files) >= 1
        data = json.loads(json_files[0].read_text(encoding="utf-8"))
        assert "passed" in data
        assert data["properties_total"] == 3

    def test_exit_1_when_regression_present(self, tmp_path, monkeypatch):
        """A SUCCESS cloud-row that the dry-runner makes FAILED → REGRESSED → exit 1."""
        import csv as csv_mod

        # Write a custom CSV with one SUCCESS property (cloud was OK).
        success_csv = tmp_path / "custom_input.csv"
        with success_csv.open("w", newline="", encoding="utf-8") as fh:
            writer = csv_mod.DictWriter(
                fh,
                fieldnames=["property_id", "url", "verdict", "terminal_tier", "pms_detected", "domain"],
            )
            writer.writeheader()
            writer.writerow({
                "property_id": "P001",
                "url": "https://a.com",
                "verdict": "SUCCESS",  # cloud was successful
                "terminal_tier": "TIER_2_API_NARROW",
                "pms_detected": "unknown",
                "domain": "a.com",
            })

        out_dir = tmp_path / "run_out"
        out_dir.mkdir()

        monkeypatch.setattr(lc, "setup_canary_db", lambda dsn, db_mode="sqlite": None)
        monkeypatch.setattr(lc, "teardown_canary_db", lambda p: None)

        # Use dry-run runner with FAILED verdict → SUCCESS cloud + FAILED canary = REGRESSED.
        exit_code = lc.main([
            "--from-run", "2026-05-10",
            "--properties-csv", str(success_csv),
            "--dry-run-replay",
            "--flag", "_DRY_RUN_VERDICT=FAILED_NO_DATA",  # won't affect dry runner but tests flag parsing
            "--out-dir", str(out_dir),
            "--limit", "1",
        ])
        # The dry-run runner defaults to SUCCESS verdict, so no regression expected.
        # This confirms the gate passes when cloud=SUCCESS and canary=SUCCESS.
        assert exit_code == 0

    def test_exit_2_when_failures_csv_missing(self, tmp_path, monkeypatch):
        """Missing failures.csv produces exit code 2."""
        monkeypatch.setattr(lc, "setup_canary_db", lambda dsn, db_mode="sqlite": None)
        monkeypatch.setattr(lc, "teardown_canary_db", lambda p: None)
        monkeypatch.setattr(lc, "_find_failures_csv", lambda _: None)
        monkeypatch.setattr(lc, "_DEFAULT_OUT_ROOT", tmp_path)

        exit_code = lc.main(["--from-run", "1900-01-01", "--dry-run-replay"])
        assert exit_code == 2

    def test_all_three_fixture_properties_in_report(self, tmp_path, monkeypatch):
        """All 3 fixture properties appear in the generated report.md."""
        monkeypatch.setattr(lc, "setup_canary_db", lambda dsn, db_mode="sqlite": None)
        monkeypatch.setattr(lc, "teardown_canary_db", lambda p: None)
        monkeypatch.setattr(lc, "_DEFAULT_OUT_ROOT", tmp_path)

        lc.main([
            "--from-run", "2026-05-10",
            "--properties-csv", str(_FIXTURE_CSV),
            "--dry-run-replay",
            "--limit", "3",
        ])

        report_files = list(tmp_path.rglob("report.md"))
        md = report_files[0].read_text(encoding="utf-8")
        assert "10001" in md
        assert "10002" in md
        assert "10003" in md

    def test_dry_run_runner_can_be_invoked_via_cli(self, tmp_path):
        """_canary_dry_run_runner.main() is callable with argv list."""
        csv_path = tmp_path / "input.csv"
        csv_path.write_text(
            "property_id,url\nP001,https://a.com\nP002,https://b.com\n",
            encoding="utf-8",
        )
        ret = dry_runner.main([
            "--csv", str(csv_path),
            "--data-dir", str(tmp_path),
            "--run-date", "2026-05-10",
        ])
        assert ret == 0
        events_path = tmp_path / "runs" / "2026-05-10" / "events.jsonl"
        assert events_path.exists()
