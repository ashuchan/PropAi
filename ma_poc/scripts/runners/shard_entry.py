"""
scripts/runners/shard_entry.py — Cloud Run task entry point.

Environment variables consumed:
  CLOUD_RUN_TASK_INDEX   (auto-set by Cloud Run) — this task's index
  CLOUD_RUN_TASK_COUNT   (auto-set by Cloud Run) — total tasks in execution
  SHARD_SOURCE           (optional) — "db" (default) or "csv". DB-mode reads
                                       the catalog from the `properties` table
                                       and slices via --shard-index/--shard-count.
                                       CSV-mode is the legacy download-and-slice
                                       path, retained as an escape hatch.
  CSV_GCS_URI            (required iff SHARD_SOURCE=csv) — gs:// URI of the CSV
  RUN_DATE               (optional) — YYYY-MM-DD; defaults to UTC today
  LIMIT                  (optional) — cap properties per shard; useful for smoke tests
  SCHEMA_VERSION         (optional) — v1 or v2; defaults to v1
  BUCKET_NAME            (required) — bucket for artifact upload

Flow (SHARD_SOURCE=db, default):
  1. Exec: python ma_poc/scripts/runners/jugnu.py --shard-index $IDX --shard-count $N
     The runner queries the `properties` table directly and slices rows by
     (canonical_id ORDER BY) into $N contiguous chunks. No CSV download,
     no /tmp slicing, no per-shard CSV file.
  2. Sync, upload, exit — identical to the legacy flow below.

Flow (SHARD_SOURCE=csv, legacy):
  1. Download CSV from GCS to /tmp/properties.csv
  2. Slice rows for this shard (ceiling division)
  3. Write slice to /tmp/shard_{idx}.csv
  4. Exec: python ma_poc/scripts/runners/jugnu.py --csv /tmp/shard_{idx}.csv ...
  5. Sync every written artifact (runs, snapshots, reports, profiles,
     scrape_events, LLM reports + diagnostics, extraction results, property
     reports, current-state properties/units) into Cloud SQL via
     ``sync_run_to_pg.sync_run_to_postgres``. The sync runs whenever the
     runner produced ``runs/{date}/properties.json`` — we do NOT gate it
     on runner_exit==0. The runner exits 1 whenever any property fails
     (common with 500-property shards), and gating sync on that turned
     every partial run into a zero-rows-in-DB deploy.
  6. Upload the ENTIRE /tmp/data/{v2/}runs/{run_date}/ tree plus the
     cross-run dlq.jsonl to gs://{bucket}/runs/{run_date}/shard_{idx}/.
     Uploading the whole run dir (not just events.jsonl) is what makes
     failed shards debuggable after /tmp is torn down.
  7. Exit with max(runner_exit, sync_exit) — surface either failure to
     Cloud Run so the retry job can pick up failed shards.

Artifact upload + PG sync both happen in a try/finally — the runner's
exit code must never suppress them, and the sync must run even when the
runner exited non-zero (that is the whole reason we're here).
"""

from __future__ import annotations

import csv
import math
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

# Ensure ma_poc is importable when invoked as /app/ma_poc/scripts/runners/shard_entry.py
_script_dir = Path(__file__).resolve().parent
_ma_poc_root = _script_dir.parent.parent  # /app/ma_poc
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
    """Upload the whole shard run dir + cross-run dlq.jsonl to GCS.

    Uploads EVERY file under ``runs/{date}/`` preserving relative paths
    (properties.json, report.json/md, property_reports/*.md,
    llm_report.json, llm_report/*.json, llm_diagnostics/*.json,
    events.jsonl, cost_ledger.db, …). Previously we uploaded only
    events.jsonl + cost_ledger.db + dlq.jsonl — which meant a shard that
    exited 1 lost properties.json and all reports to /tmp teardown, and
    we had no way to post-mortem the failure.

    dlq.jsonl lives outside the run dir (it's cross-run state), so it's
    uploaded separately. Called from a finally block — per-file errors
    are logged and swallowed by ``gcs.upload_prefix`` so nothing here
    masks the runner's or sync's exit code.
    """
    local_run_dir = _resolve_run_dir(schema_version, run_date)
    state_dir = _schema_root(schema_version) / "state"
    dest_prefix = f"gs://{bucket_name}/runs/{run_date}/shard_{task_idx}/"

    if not local_run_dir.exists() and not state_dir.exists():
        print(
            f"[shard_entry] Neither {local_run_dir} nor {state_dir} exists; skipping upload",
            file=sys.stderr,
        )
        return

    if local_run_dir.exists():
        try:
            count = gcs.upload_prefix(local_run_dir, dest_prefix)
            print(
                f"[shard_entry] Uploaded {count} files from {local_run_dir} → {dest_prefix}",
                file=sys.stderr,
            )
        except Exception as exc:  # noqa: BLE001 — must not mask runner exit
            print(
                f"[shard_entry] Failed to upload run dir {local_run_dir}: {exc}",
                file=sys.stderr,
            )
    else:
        print(f"[shard_entry] {local_run_dir} not found; skipping run-dir upload", file=sys.stderr)

    # dlq.jsonl is cross-run state (``state/`` sits above ``runs/``), so
    # it rides on this shard's upload path but isn't part of the run dir.
    dlq_local = state_dir / "dlq.jsonl"
    if dlq_local.exists():
        try:
            gcs.upload_object(dlq_local, dest_prefix + "dlq.jsonl")
            print(f"[shard_entry] Uploaded dlq.jsonl → {dest_prefix}", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001
            print(f"[shard_entry] Failed to upload dlq.jsonl: {exc}", file=sys.stderr)
    else:
        print(f"[shard_entry] dlq.jsonl not found at {dlq_local}; skipping", file=sys.stderr)


def _sync_to_postgres(run_date: str, schema_version: str, shard_id: str) -> int:
    """Copy the shard's FS output into Cloud SQL.

    Returns 0 on success, 1 on any failure. Callers treat a sync failure
    as a shard failure: the DB is the authoritative destination and a
    silent drop here is exactly the class of bug that left prod at 0
    rows. Run logs already contain the full traceback.

    Requires ``runs/{run_date}/properties.json`` to exist (the runner
    actually ran to completion writing the report). Fast-returns 0 if
    the file is missing — nothing to sync, not a sync failure.

    No-op if ``DATABASE_URL`` is unset — that's the local-dev path where
    Postgres isn't configured. In prod terraform always sets it.

    ``shard_id`` scopes per-shard aggregation in run_reports / llm_reports
    so concurrent shards don't clobber each other's totals. Pass the
    Cloud Run task index (or any unique string per shard).
    """
    if not os.environ.get("DATABASE_URL"):
        print(
            "[shard_entry] DATABASE_URL unset; skipping PG sync (local-dev path)",
            file=sys.stderr,
        )
        return 0

    data_dir = _schema_root(schema_version)
    run_dir = _resolve_run_dir(schema_version, run_date)
    properties_json = run_dir / "properties.json"
    if not properties_json.exists():
        # Runner crashed before writing output. Nothing to sync.
        # Still return 0 — this isn't a sync failure, it's an upstream
        # failure that's already reflected in runner_exit.
        print(
            f"[shard_entry] {properties_json} missing; runner did not produce output. Skipping PG sync.",
            file=sys.stderr,
        )
        return 0

    try:
        # Import here so container startup doesn't pay for SQLAlchemy + the
        # Cloud SQL connector unless we're actually about to use them.
        from ma_poc.scripts.sync.run_to_pg import sync_run_to_postgres
    except Exception as exc:  # noqa: BLE001
        print(f"[shard_entry] Failed to import sync module: {exc}", file=sys.stderr)
        return 1

    # Profiles are written by services/profile_store.py into
    # _MA_POC_ROOT / "config" / "profiles" — see jugnu_runner._SimpleProfileStore.
    # That resolves to /app/ma_poc/config inside the Cloud Run container.
    config_dir = _ma_poc_root / "config"

    try:
        summary = sync_run_to_postgres(
            run_date=run_date,
            data_dir=data_dir,
            config_dir=config_dir,
            shard_id=shard_id,
        )
        print(f"[shard_entry] PG sync complete: {summary}", file=sys.stderr)
        return 0
    except Exception:
        import traceback

        print("[shard_entry] PG sync FAILED:", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return 1


def main() -> None:
    # Diagnostic dispatch — SMOKE_MODE=proxy_check short-circuits to
    # check_proxy.py. Avoids a separate Cloud Run job just to verify
    # PROXY_POOL_URLS is wired; trigger_proxy_smoke.py sets this env
    # var via --update-env-vars and relies on the exit code.
    smoke_mode = os.environ.get("SMOKE_MODE", "").strip().lower()
    if smoke_mode == "proxy_check":
        from ma_poc.scripts import check_proxy

        sys.exit(check_proxy.main())

    task_idx = int(os.environ.get("CLOUD_RUN_TASK_INDEX", "0"))
    task_count = int(os.environ.get("CLOUD_RUN_TASK_COUNT", "1"))
    bucket_name = _require_env("BUCKET_NAME")
    run_date = os.environ.get("RUN_DATE") or date.today().isoformat()
    limit_str = os.environ.get("LIMIT")
    limit = int(limit_str) if limit_str else None
    schema_version = os.environ.get("SCHEMA_VERSION", "v1")
    shard_source = (os.environ.get("SHARD_SOURCE") or "db").strip().lower()

    print(
        f"[shard_entry] task {task_idx}/{task_count}, run_date={run_date}, source={shard_source}",
        file=sys.stderr,
    )

    runner = _app_root / "ma_poc" / "scripts" / "runners" / "jugnu.py"
    cmd = [
        sys.executable,
        str(runner),
        "--run-date",
        run_date,
        "--schema-version",
        schema_version,
        "--data-dir",
        "/tmp/data",
    ]

    if shard_source == "db":
        # DB-mode: the runner reads the `properties` table and slices
        # rows itself. No download, no /tmp shard CSV. Empty-shard
        # detection is the runner's job (a shard whose canonical_id
        # range is empty exits cleanly with zero properties processed).
        cmd += [
            "--shard-index",
            str(task_idx),
            "--shard-count",
            str(task_count),
        ]
    elif shard_source == "csv":
        # Legacy escape hatch — download the CSV from GCS, slice locally,
        # exec the runner with --csv pointing at the shard file.
        csv_gcs_uri = _require_env("CSV_GCS_URI")
        csv_local = Path("/tmp/properties.csv")
        _download_csv(csv_gcs_uri, csv_local)
        shard_csv, row_count = _slice_csv(csv_local, task_idx, task_count, limit)
        print(f"[shard_entry] shard has {row_count} rows", file=sys.stderr)
        if row_count == 0:
            print("[shard_entry] Empty shard — nothing to process, exiting 0", file=sys.stderr)
            sys.exit(0)
        cmd += ["--csv", str(shard_csv)]
    else:
        sys.exit(f"Unknown SHARD_SOURCE={shard_source!r}; expected 'db' or 'csv'")

    if limit is not None:
        cmd += ["--limit", str(limit)]

    runner_exit = 1
    sync_exit = 0
    # Wall-clock cap for the runner subprocess. Cloud Run's task timeout is
    # 4h (14400s); we cap at 3h45m so the parent gets control back ~15min
    # before Cloud Run sends SIGKILL. That window is what lets the finally
    # block actually run — _upload_artifacts and PG sync need ~5–10min on a
    # full shard, and SIGKILL is unblockable, so without a parent-side
    # timeout a wedged subprocess takes its artifacts to the grave (the
    # exact failure mode that left stuck shards undebuggable).
    runner_subprocess_timeout = float(os.environ.get("SHARD_RUNNER_TIMEOUT_SECONDS", "13500"))
    try:
        try:
            result = subprocess.run(cmd, check=False, timeout=runner_subprocess_timeout)
            runner_exit = result.returncode
        except subprocess.TimeoutExpired:
            # Subprocess didn't return in time. Python has already sent
            # SIGKILL by the time TimeoutExpired surfaces (subprocess.run
            # internals), so the child is dead. Mark as failure but keep
            # going — the finally block must still upload whatever
            # partial artifacts the runner managed to flush, and PG sync
            # of a partial properties.json is still better than nothing.
            print(
                f"[shard_entry] runner exceeded {runner_subprocess_timeout}s wall-clock cap; "
                "killed subprocess. Proceeding to artifact upload + sync.",
                file=sys.stderr,
            )
            runner_exit = 124  # convention: 124 = timeout (matches GNU coreutils `timeout`)
        # Always attempt PG sync when the runner finished — partial runs
        # (e.g. 140/499 succeeded) still have useful data that MUST land
        # in Postgres. The runner returns 1 whenever any property fails,
        # which is every real 500-property shard; gating sync on
        # runner_exit==0 was the bug that left prod at 0 rows despite
        # scrapes actually producing per-property output.
        #
        # ``_sync_to_postgres`` fast-returns 0 when no properties.json
        # exists (runner crashed before writing output), so this is
        # still safe when the runner dies mid-startup.
        sync_exit = _sync_to_postgres(run_date, schema_version, shard_id=str(task_idx))
    finally:
        _upload_artifacts(bucket_name, run_date, task_idx, schema_version)

    # Cloud Run task exit policy (2026-05-04):
    #
    # The runner returns 1 whenever ANY property fails — which is every
    # real 500-property shard, every day. Propagating that to Cloud Run
    # marked the task as failed in the dashboard despite shards having
    # produced and uploaded valid output. The canary observed exactly
    # this: "Run complete: 24/87 succeeded" was followed by exit(1) and
    # an X marker on the execution.
    #
    # New rule: a shard is FAILED only if the work itself didn't land —
    #   • runner crashed before writing properties.json, OR
    #   • PG sync raised an unhandled exception (data not durable).
    # Per-property scrape failures are signalled via run_report.json,
    # SLO violations, and the bot_blocked_properties.json artifact —
    # not via Cloud Run task status. Operators see the same information,
    # but the dashboard now reflects infra health, not data-quality
    # health (which has its own dashboards).
    run_dir = _resolve_run_dir(schema_version, run_date)
    properties_json = run_dir / "properties.json"
    runner_produced_output = properties_json.exists()

    if not runner_produced_output:
        print(
            f"[shard_entry] runner did not produce {properties_json}; exiting 1",
            file=sys.stderr,
        )
        sys.exit(1)
    if sync_exit != 0:
        print(
            "[shard_entry] PG sync failed — data not durable; exiting 1",
            file=sys.stderr,
        )
        sys.exit(sync_exit)
    if runner_exit != 0:
        print(
            f"[shard_entry] runner exited {runner_exit} but {properties_json} was written and synced; "
            "treating as task SUCCESS (per-property failures are reported in run_report.json)",
            file=sys.stderr,
        )
    sys.exit(0)


if __name__ == "__main__":
    # Import sanity check — catches path/package regressions at deploy time
    from ma_poc.pms import scraper  # noqa: F401

    task_idx = os.environ.get("CLOUD_RUN_TASK_INDEX", "0")
    task_count = os.environ.get("CLOUD_RUN_TASK_COUNT", "1")

    # Stub-mode detection: if neither BUCKET_NAME nor any explicit "go"
    # signal is set, just print info and exit. Previously keyed off
    # CSV_GCS_URI, which DB-mode shards no longer set.
    if not os.environ.get("BUCKET_NAME"):
        print(f"jugnu_shard_entry stub: task {task_idx}/{task_count}")
        sys.exit(0)

    main()
