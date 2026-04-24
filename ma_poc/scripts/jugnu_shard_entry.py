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
  5. On runner success: sync every written artifact (runs, snapshots,
     reports, profiles, scrape_events, LLM reports + diagnostics,
     extraction results, property reports) into Cloud SQL via
     ``sync_run_to_pg.sync_run_to_postgres``. The runner itself writes
     only to the local FS; without this step Postgres stays empty.
  6. Upload /tmp/data/v2/runs/{run_date}/events.jsonl, dlq.jsonl,
     cost_ledger.db to gs://{bucket}/runs/{run_date}/shard_{idx}/.
  7. Exit with the runner's exit code (or 1 if the PG sync failed).

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


def _resolve_run_dir(schema_version: str, run_date: str) -> Path:
    """Mirror ``jugnu_runner._resolve_data_dirs`` — v2 injects a ``v2/`` prefix.

    Keep these two in sync: if the runner changes how it roots per-schema
    output, this function must change with it or downstream GCS upload +
    PG sync will look in the wrong place and silently skip everything.
    """
    root = Path("/tmp/data")
    if schema_version == "v2":
        root = root / "v2"
    return root / "runs" / run_date


def _schema_root(schema_version: str) -> Path:
    root = Path("/tmp/data")
    return root / "v2" if schema_version == "v2" else root


def _upload_artifacts(bucket_name: str, run_date: str, task_idx: int, schema_version: str) -> None:
    """Upload per-shard artifacts to GCS; called in finally so failures don't suppress runner exit code."""
    local_run_dir = _resolve_run_dir(schema_version, run_date)
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


def _sync_to_postgres(run_date: str, schema_version: str) -> int:
    """Copy the shard's FS output into Cloud SQL.

    Returns 0 on success, 1 on any failure. Callers should treat a sync
    failure as a shard failure: the DB is the authoritative destination
    for this job and a silent drop here is exactly the class of bug that
    left prod at 0 rows. Run logs already contain the full traceback.

    No-op if ``DATABASE_URL`` is unset — that's the local-dev path where
    Postgres isn't configured. In prod terraform always sets it.
    """
    if not os.environ.get("DATABASE_URL"):
        print(
            "[shard_entry] DATABASE_URL unset; skipping PG sync (local-dev path)",
            file=sys.stderr,
        )
        return 0

    try:
        # Import here so container startup doesn't pay for SQLAlchemy + the
        # Cloud SQL connector unless we're actually about to use them.
        from ma_poc.scripts.sync_run_to_pg import sync_run_to_postgres
    except Exception as exc:  # noqa: BLE001
        print(f"[shard_entry] Failed to import sync module: {exc}", file=sys.stderr)
        return 1

    data_dir = _schema_root(schema_version)
    # Profiles are written by services/profile_store.py into
    # _MA_POC_ROOT / "config" / "profiles" — see jugnu_runner._SimpleProfileStore.
    # That resolves to /app/ma_poc/config inside the Cloud Run container.
    config_dir = _ma_poc_root / "config"

    try:
        summary = sync_run_to_postgres(
            run_date=run_date,
            data_dir=data_dir,
            config_dir=config_dir,
        )
        print(f"[shard_entry] PG sync complete: {summary}", file=sys.stderr)
        return 0
    except Exception:
        import traceback

        print("[shard_entry] PG sync FAILED:", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return 1


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
    sync_exit = 0
    try:
        result = subprocess.run(cmd, check=False)
        runner_exit = result.returncode
        # Only attempt the PG sync when the scrape itself succeeded.
        # Syncing a partial/failed run would copy a mix of today's
        # rows and half-written JSON; safer to let the retry job
        # rerun from scratch and re-sync.
        if runner_exit == 0:
            sync_exit = _sync_to_postgres(run_date, schema_version)
    finally:
        _upload_artifacts(bucket_name, run_date, task_idx, schema_version)

    # A sync failure is a shard failure — the DB is the destination of
    # record. Surfacing it as exit 1 lets the retry job (jugnu-retry)
    # pick this shard back up instead of marking it silently good.
    sys.exit(runner_exit or sync_exit)


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
