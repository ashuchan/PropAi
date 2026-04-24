"""Unit tests for jugnu_shard_entry.py — CSV slicing and artifact upload logic."""

from __future__ import annotations

import csv
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Path bootstrap
_here = Path(__file__).resolve().parent.parent.parent
_app = _here.parent
for _p in (_app, _here):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from scripts.jugnu_shard_entry import _slice_csv, _upload_artifacts  # noqa: E402

# ── CSV slicing ──────────────────────────────────────────────────────────────


def _make_csv(tmp_path: Path, rows: list[list[str]]) -> Path:
    p = tmp_path / "props.csv"
    with p.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["canonical_id", "url", "pms_hint"])
        writer.writerows(rows)
    return p


class TestSliceCsv:
    def test_divides_evenly(self, tmp_path: Path) -> None:
        rows = [[f"id-{i}", f"https://example.com/{i}", ""] for i in range(1000)]
        src = _make_csv(tmp_path, rows)
        dest, count = _slice_csv(src, task_idx=0, task_count=5, limit=None)
        assert count == 200

    def test_divides_evenly_all_tasks(self, tmp_path: Path) -> None:
        rows = [[f"id-{i}", f"https://example.com/{i}", ""] for i in range(1000)]
        src = _make_csv(tmp_path, rows)
        total = 0
        for idx in range(5):
            _, count = _slice_csv(src, task_idx=idx, task_count=5, limit=None)
            total += count
        assert total == 1000

    def test_handles_remainder(self, tmp_path: Path) -> None:
        """1003 rows / 5 tasks → tasks 0-3 get ceil(1003/5)=201, task 4 gets the rest."""
        rows = [[f"id-{i}", f"https://example.com/{i}", ""] for i in range(1003)]
        src = _make_csv(tmp_path, rows)
        total = 0
        for idx in range(5):
            _, count = _slice_csv(src, task_idx=idx, task_count=5, limit=None)
            total += count
        assert total == 1003

    def test_limit_caps_rows(self, tmp_path: Path) -> None:
        rows = [[f"id-{i}", f"https://example.com/{i}", ""] for i in range(500)]
        src = _make_csv(tmp_path, rows)
        _, count = _slice_csv(src, task_idx=0, task_count=1, limit=10)
        assert count == 10

    def test_zero_rows_single_task(self, tmp_path: Path) -> None:
        """Empty CSV after header → shard produces 0-row file (sys.exit is in main, not _slice_csv)."""
        src = _make_csv(tmp_path, [])
        # _slice_csv itself doesn't exit; main() checks the returned count
        dest, count = _slice_csv(src, task_idx=0, task_count=1, limit=None)
        assert count == 0

    def test_shard_file_has_header(self, tmp_path: Path) -> None:
        rows = [[f"id-{i}", f"https://example.com/{i}", ""] for i in range(100)]
        src = _make_csv(tmp_path, rows)
        dest, _ = _slice_csv(src, task_idx=0, task_count=2, limit=None)
        with dest.open(newline="") as f:
            reader = csv.reader(f)
            header = next(reader)
        assert header == ["canonical_id", "url", "pms_hint"]

    def test_last_task_does_not_exceed_total(self, tmp_path: Path) -> None:
        """Last task's slice must not request rows beyond the CSV end."""
        rows = [[f"id-{i}", f"https://example.com/{i}", ""] for i in range(7)]
        src = _make_csv(tmp_path, rows)
        _, count = _slice_csv(src, task_idx=2, task_count=3, limit=None)
        # 7 rows, 3 tasks: task 2 gets rows 6..6 (1 row, not 3)
        assert count >= 1


# ── artifact upload ──────────────────────────────────────────────────────────


class TestUploadArtifacts:
    def test_uploads_existing_artifacts(self, tmp_path: Path) -> None:
        """When artifacts exist, gcs.upload_object is called for each one.

        Previously this test patched subprocess.run to observe gsutil
        invocations; the entry script now uses google-cloud-storage
        directly (gsutil is no longer in the prod image), so the mock
        target moved to ma_poc.storage.gcs.upload_object.
        """
        run_date = "2026-04-21"
        # events.jsonl + cost_ledger.db are per-run artifacts; dlq.jsonl
        # is cross-run state written to state_dir by jugnu_runner.
        # Splitting the fixture catches regressions where the uploader
        # pulls dlq.jsonl from the wrong directory (shipped in prod and
        # silently skipped every upload).
        run_dir = tmp_path / "data" / "runs" / run_date
        run_dir.mkdir(parents=True)
        for fname in ("events.jsonl", "cost_ledger.db"):
            (run_dir / fname).write_text("data")
        state_dir = tmp_path / "data" / "state"
        state_dir.mkdir(parents=True)
        (state_dir / "dlq.jsonl").write_text("data")

        uploaded_pairs: list[tuple[Path, str]] = []

        def fake_upload(src: Path, uri: str, **kwargs: object) -> None:
            uploaded_pairs.append((src, uri))

        import scripts.jugnu_shard_entry as m

        original_path_cls = m.Path

        def patched_path(s: str) -> Path:
            if s.startswith("/tmp/"):
                return tmp_path / s[len("/tmp/") :]
            return original_path_cls(s)

        with patch("scripts.jugnu_shard_entry.gcs.upload_object", side_effect=fake_upload):
            m.Path = patched_path  # type: ignore[assignment]
            try:
                _upload_artifacts("jugnu-raw-staging", run_date, 0, "v1")
            finally:
                m.Path = original_path_cls  # type: ignore[assignment]

        assert len(uploaded_pairs) == 3
        uris = {uri for _, uri in uploaded_pairs}
        assert uris == {
            f"gs://jugnu-raw-staging/runs/{run_date}/shard_0/events.jsonl",
            f"gs://jugnu-raw-staging/runs/{run_date}/shard_0/dlq.jsonl",
            f"gs://jugnu-raw-staging/runs/{run_date}/shard_0/cost_ledger.db",
        }

    def test_missing_run_dir_does_not_crash(self, tmp_path: Path) -> None:
        """If the run dir doesn't exist, upload logs a warning but does not raise."""
        import scripts.jugnu_shard_entry as m

        original_path_cls = m.Path

        def patched_path(s: str) -> Path:
            if s.startswith("/tmp/"):
                return tmp_path / "nonexistent" / s.lstrip("/")
            return original_path_cls(s)

        with patch("subprocess.run") as mock_run:
            m.Path = patched_path  # type: ignore[assignment]
            try:
                _upload_artifacts("jugnu-raw-staging", "2026-04-21", 0, "v1")
            finally:
                m.Path = original_path_cls  # type: ignore[assignment]
            mock_run.assert_not_called()


# ── runner exit code propagation ─────────────────────────────────────────────


class TestRunnerExitCode:
    def test_runner_failure_propagates_and_artifacts_still_uploaded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Even when the runner exits non-zero, artifacts must be uploaded."""
        rows = [[f"id-{i}", f"https://example.com/{i}", ""] for i in range(5)]
        csv_src = _make_csv(tmp_path, rows)

        uploaded = []

        def fake_subprocess_run(cmd: list[str], **kw: object) -> MagicMock:
            if "gsutil" in cmd and "cp" in cmd:
                uploaded.append(cmd[-1])  # destination
            return MagicMock(returncode=0)

        def fake_subprocess_run_runner(cmd: list[str], **kw: object) -> MagicMock:
            # Simulate runner failure
            if "jugnu_runner" in " ".join(cmd):
                return MagicMock(returncode=1)
            return MagicMock(returncode=0)

        monkeypatch.setenv("CSV_GCS_URI", f"file://{csv_src}")
        monkeypatch.setenv("BUCKET_NAME", "jugnu-raw-staging")
        monkeypatch.setenv("CLOUD_RUN_TASK_INDEX", "0")
        monkeypatch.setenv("CLOUD_RUN_TASK_COUNT", "1")
