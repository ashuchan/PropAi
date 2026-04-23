"""
scripts/jugnu_shard_entry.py — Cloud Run task entry point.

Environment variables consumed:
  CLOUD_RUN_TASK_INDEX   (auto-set by Cloud Run) — this task's index
  CLOUD_RUN_TASK_COUNT   (auto-set by Cloud Run) — total tasks in execution
  CSV_GCS_URI            (required) — gs:// URI of the properties CSV
  RUN_DATE               (optional) — YYYY-MM-DD; defaults to UTC today
  LIMIT                  (optional) — cap properties per shard; useful for smoke tests
  SCHEMA_VERSION         (optional) — v1 or v2; defaults to v1
  BUCKET_NAME            (required) — bucket for artifact upload

Flow:
  1. Download CSV from GCS to /tmp/properties.csv
  2. Slice rows for this shard (ceiling division)
  3. Write slice to /tmp/shard_{idx}.csv
  4. Exec: python ma_poc/scripts/jugnu_runner.py --csv /tmp/shard_{idx}.csv ...
  5. Upload /tmp/runs/{run_date}/events.jsonl, dlq.jsonl, cost_ledger.db
     to gs://{bucket}/runs/{run_date}/shard_{idx}/
  6. Exit with the runner's exit code.

Artifact upload happens in a try/finally — ensures artifacts exist even when
the runner crashes. This is how Claude Code debugs failed shards.
"""

from __future__ import annotations

import csv
import math
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

# Ensure ma_poc is importable when invoked as /app/ma_poc/scripts/jugnu_shard_entry.py
_script_dir = Path(__file__).resolve().parent
_ma_poc_root = _script_dir.parent  # /app/ma_poc
_app_root = _ma_poc_root.parent  # /app
for _p in (_app_root, _ma_poc_root):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from ma_poc.storage import gcs  # noqa: E402


def _require_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        sys.exit(f"Required environment variable {name!r} is not set")
    return val


def _download_csv(gcs_uri: str, dest: Path) -> None:
    # Was ``subprocess.run(["gsutil", ...])`` — the slim prod image no
    # longer ships gcloud/gsutil (see commit 4728b57 "Compacted docker
    # image"), so the shell-out was failing at runtime with
    # FileNotFoundError. Use the google-cloud-storage Python client.
    gcs.download_object(gcs_uri, dest)


def _slice_csv(src: Path, task_idx: int, task_count: int, limit: int | None) -> tuple[Path, int]:
    """Read src CSV, slice rows for task_idx, write shard file; return (path, row_count)."""
    with src.open(newline="") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if header is None:
            sys.exit("CSV is empty — nothing to process")
        rows = list(reader)

    total = len(rows)
    shard_size = math.ceil(total / task_count)
    start = task_idx * shard_size
    end = min(start + shard_size, total)
    shard_rows = rows[start:end]

    if limit is not None:
        shard_rows = shard_rows[:limit]

    dest = Path(f"/tmp/shard_{task_idx}.csv")
    with dest.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(shard_rows)

    return dest, len(shard_rows)


def _upload_artifacts(bucket_name: str, run_date: str, task_idx: int) -> None:
    """Upload per-shard artifacts to GCS; called in finally so failures don't suppress runner exit code."""
    local_run_dir = Path(f"/tmp/data/runs/{run_date}")
    dest_prefix = f"gs://{bucket_name}/runs/{run_date}/shard_{task_idx}/"

    if not local_run_dir.exists():
        print(f"[shard_entry] No local run dir {local_run_dir}; skipping upload", file=sys.stderr)
        return

    for artifact in ("events.jsonl", "dlq.jsonl", "cost_ledger.db"):
        local_path = local_run_dir / artifact
        if local_path.exists():
            try:
                gcs.upload_object(local_path, dest_prefix + artifact)
                print(f"[shard_entry] Uploaded {artifact} → {dest_prefix}", file=sys.stderr)
            except Exception as exc:  # noqa: BLE001 — must not mask runner exit
                print(
                    f"[shard_entry] Failed to upload {artifact}: {exc}",
                    file=sys.stderr,
                )
        else:
            print(f"[shard_entry] {artifact} not found; skipping", file=sys.stderr)


def main() -> None:
    task_idx = int(os.environ.get("CLOUD_RUN_TASK_INDEX", "0"))
    task_count = int(os.environ.get("CLOUD_RUN_TASK_COUNT", "1"))
    csv_gcs_uri = _require_env("CSV_GCS_URI")
    bucket_name = _require_env("BUCKET_NAME")
    run_date = os.environ.get("RUN_DATE") or date.today().isoformat()
    limit_str = os.environ.get("LIMIT")
    limit = int(limit_str) if limit_str else None
    schema_version = os.environ.get("SCHEMA_VERSION", "v1")

    print(f"[shard_entry] task {task_idx}/{task_count}, run_date={run_date}", file=sys.stderr)

    # 1. Download CSV
    csv_local = Path("/tmp/properties.csv")
    _download_csv(csv_gcs_uri, csv_local)

    # 2-3. Slice to shard file
    shard_csv, row_count = _slice_csv(csv_local, task_idx, task_count, limit)
    print(f"[shard_entry] shard has {row_count} rows", file=sys.stderr)

    if row_count == 0:
        print("[shard_entry] Empty shard — nothing to process, exiting 0", file=sys.stderr)
        sys.exit(0)

    # 4-5. Run the pipeline; always upload artifacts
    runner = _app_root / "ma_poc" / "scripts" / "jugnu_runner.py"
    cmd = [
        sys.executable,
        str(runner),
        "--csv",
        str(shard_csv),
        "--run-date",
        run_date,
        "--schema-version",
        schema_version,
        "--data-dir",
        "/tmp/data",
    ]
    if limit is not None:
        cmd += ["--limit", str(limit)]

    runner_exit = 1
    try:
        result = subprocess.run(cmd, check=False)
        runner_exit = result.returncode
    finally:
        _upload_artifacts(bucket_name, run_date, task_idx)

    sys.exit(runner_exit)


if __name__ == "__main__":
    # Import sanity check — catches path/package regressions at deploy time
    from ma_poc.pms import scraper  # noqa: F401

    task_idx = os.environ.get("CLOUD_RUN_TASK_INDEX", "0")
    task_count = os.environ.get("CLOUD_RUN_TASK_COUNT", "1")

    # If running as a stub (no CSV_GCS_URI) print info and exit
    if not os.environ.get("CSV_GCS_URI"):
        print(f"jugnu_shard_entry stub: task {task_idx}/{task_count}")
        sys.exit(0)

    main()
