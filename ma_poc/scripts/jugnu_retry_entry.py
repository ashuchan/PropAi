"""
scripts/jugnu_retry_entry.py — Cloud Run retry job entry point.

Environment variables consumed:
  RETRY_MODE      (required) — "errors" or "resume"
  RUN_DATE        (optional) — YYYY-MM-DD; defaults to UTC today
  LIMIT           (optional) — cap retry attempts
  CSV_GCS_URI     (required) — gs:// URI of the properties CSV
  BUCKET_NAME     (required) — bucket for artifact download/upload
  SCHEMA_VERSION  (optional) — v1 | v2; defaults to v1 (matches runner)
  DATABASE_URL    (optional) — when set, pre-hydrate the DLQ from
                  ``dlq_entries`` so cross-run parking survives
                  Cloud Run's ephemeral /tmp.

Flow:
  1. Read RETRY_MODE, RUN_DATE, LIMIT from env
  2. Determine target run_date (env or today)
  3. Download gs://{bucket}/runs/{run_date}/ → /tmp/data/runs/{run_date}/
     (gsutil -m cp -r)
  4. Download CSV → /tmp/properties.csv
  5. Pre-hydrate ``state/dlq.jsonl`` from the ``dlq_entries`` table —
     Cloud Run started a brand new container, so without this step the
     retry runner would see an empty DLQ and re-park any still-broken
     properties from scratch, losing their original ``parked_at``.
  6. Exec: python ma_poc/scripts/jugnu_retry_runner.py
         --retry-errors OR --resume  (based on RETRY_MODE)
         --run-date {run_date}
         --csv /tmp/properties.csv
         [--limit N if set]
  7. Re-sync DLQ back to ``dlq_entries``: retries that succeeded got
     unparked in-memory → need DELETE from DB; retries that failed
     again got ``reschedule()`` → need UPDATE.
  8. Upload any new artifacts back to gs://{bucket}/runs/{run_date}/retry-{timestamp}/
  9. Exit with runner's code (or 1 if post-runner DLQ sync fails).
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, date, datetime
from pathlib import Path

# Ensure ma_poc is importable
_script_dir = Path(__file__).resolve().parent
_ma_poc_root = _script_dir.parent
_app_root = _ma_poc_root.parent
for _p in (_app_root, _ma_poc_root):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from ma_poc.storage import gcs  # noqa: E402


def _require_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        sys.exit(f"Required environment variable {name!r} is not set")
    return val


def _download_gcs_dir(gcs_prefix: str, local_dir: Path) -> None:
    """Download a GCS prefix recursively to a local directory.

    Was ``gsutil -m cp -r``; the slim prod image no longer ships gsutil.
    Uses the google-cloud-storage client instead. Missing prefix is
    tolerated (fresh-day run): download_prefix returns 0.
    """
    gcs.download_prefix(gcs_prefix, local_dir)


def _schema_root(schema_version: str) -> Path:
    root = Path("/tmp/data")
    return root / "v2" if schema_version == "v2" else root


def _upload_artifacts(bucket_name: str, run_date: str, timestamp: str, schema_version: str) -> None:
    local_run_dir = _schema_root(schema_version) / "runs" / run_date
    if not local_run_dir.exists():
        return
    dest = f"gs://{bucket_name}/runs/{run_date}/retry-{timestamp}/"
    gcs.upload_prefix(local_run_dir, dest)
    print(f"[retry_entry] Uploaded retry artifacts → {dest}", file=sys.stderr)


def _hydrate_dlq_from_db(schema_version: str) -> int:
    """Write the DB's current DLQ state to local state/dlq.jsonl.

    Returns number of entries hydrated. Returns 0 silently when
    ``DATABASE_URL`` is unset (local-dev path); the retry runner will
    operate on an empty/fresh DLQ in that case, same as before.
    """
    if not os.environ.get("DATABASE_URL"):
        print("[retry_entry] DATABASE_URL unset; skipping DLQ hydrate", file=sys.stderr)
        return 0

    try:
        from ma_poc.data_provider import PostgresDataProvider
        from ma_poc.scripts.sync_run_to_pg import load_dlq_from_db_to_file
    except Exception as exc:  # noqa: BLE001
        print(f"[retry_entry] Failed to import sync module: {exc}", file=sys.stderr)
        raise

    state_dir = _schema_root(schema_version) / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    provider = PostgresDataProvider()
    try:
        n = load_dlq_from_db_to_file(engine=provider.engine, path=state_dir / "dlq.jsonl")
    finally:
        provider.close()
    print(f"[retry_entry] DLQ hydrated from DB: {n} entries", file=sys.stderr)
    return n


def _sync_dlq_to_db(schema_version: str) -> int:
    """Mirror the post-runner local DLQ state back into ``dlq_entries``.

    Returns 0 on success, 1 on failure. Unlike the scrape shard, a
    retry-entry failure at this step shouldn't silently mask the
    underlying retry result — we propagate it up as a non-zero exit so
    the operator sees it.
    """
    if not os.environ.get("DATABASE_URL"):
        return 0

    try:
        from ma_poc.data_provider import PostgresDataProvider
        from ma_poc.scripts.sync_run_to_pg import _sync_dlq
    except Exception as exc:  # noqa: BLE001
        print(f"[retry_entry] Failed to import sync module: {exc}", file=sys.stderr)
        return 1

    state_dir = _schema_root(schema_version) / "state"
    dlq_path = state_dir / "dlq.jsonl"
    if not dlq_path.exists():
        print("[retry_entry] No local DLQ file after run; skipping sync", file=sys.stderr)
        return 0

    provider = PostgresDataProvider()
    try:
        summary = _sync_dlq(provider.engine, dlq_path)
        print(f"[retry_entry] DLQ synced to DB: {summary}", file=sys.stderr)
        return 0
    except Exception:
        import traceback
        print("[retry_entry] DLQ sync FAILED:", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return 1
    finally:
        provider.close()


def main() -> None:
    retry_mode = os.environ.get("RETRY_MODE", "errors").lower()
    if retry_mode not in ("errors", "resume"):
        sys.exit(f"Invalid RETRY_MODE={retry_mode!r}; expected 'errors' or 'resume'")

    csv_gcs_uri = _require_env("CSV_GCS_URI")
    bucket_name = _require_env("BUCKET_NAME")
    run_date = os.environ.get("RUN_DATE") or date.today().isoformat()
    limit_str = os.environ.get("LIMIT")
    limit = int(limit_str) if limit_str else None
    schema_version = os.environ.get("SCHEMA_VERSION", "v1")

    print(
        f"[retry_entry] mode={retry_mode}, run_date={run_date}, schema={schema_version}",
        file=sys.stderr,
    )

    # 3. Download prior run artifacts
    run_gcs_prefix = f"gs://{bucket_name}/runs/{run_date}"
    local_run_dir = _schema_root(schema_version) / "runs" / run_date
    _download_gcs_dir(run_gcs_prefix, local_run_dir)

    # 4. Download CSV
    csv_local = Path("/tmp/properties.csv")
    gcs.download_object(csv_gcs_uri, csv_local)

    # 5. Hydrate DLQ from DB. Without this step the retry runner's
    #    ``Dlq(state_dir / "dlq.jsonl")._load()`` sees an empty file
    #    (Cloud Run tmp is fresh) and the parked_at / retry_at state
    #    the scrape run persisted is lost. Failing to hydrate is fatal
    #    because the whole point of this job is to act on that state.
    try:
        _hydrate_dlq_from_db(schema_version)
    except Exception:
        import traceback
        print("[retry_entry] DLQ hydrate FAILED:", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)

    # 6. Run the retry runner
    runner = _app_root / "ma_poc" / "scripts" / "jugnu_retry_runner.py"
    mode_flag = "--retry-errors" if retry_mode == "errors" else "--resume"
    cmd = [
        sys.executable,
        str(runner),
        mode_flag,
        "--run-date",
        run_date,
        "--csv",
        str(csv_local),
        "--data-dir",
        "/tmp/data",
    ]
    if limit is not None:
        cmd += ["--limit", str(limit)]

    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    runner_exit = 1
    sync_exit = 0
    try:
        result = subprocess.run(cmd, check=False)
        runner_exit = result.returncode
        # 7. Re-sync DLQ — retries that succeeded unpark locally; we
        #    need to DELETE those rows from dlq_entries. Retries that
        #    failed again got reschedule()'d with a new retry_at.
        #    Sync regardless of runner exit: the retry runner may have
        #    partially updated the DLQ before crashing, and the DB
        #    should reflect its last-known good state.
        sync_exit = _sync_dlq_to_db(schema_version)
    finally:
        _upload_artifacts(bucket_name, run_date, timestamp, schema_version)

    sys.exit(runner_exit or sync_exit)


if __name__ == "__main__":
    from ma_poc.pms import scraper  # noqa: F401

    mode = os.environ.get("RETRY_MODE", "errors")
    if not os.environ.get("CSV_GCS_URI"):
        print(f"jugnu_retry_entry stub: mode={mode}")
        sys.exit(0)

    main()
