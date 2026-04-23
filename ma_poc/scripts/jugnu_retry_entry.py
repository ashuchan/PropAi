"""
scripts/jugnu_retry_entry.py — Cloud Run retry job entry point.

Environment variables consumed:
  RETRY_MODE   (required) — "errors" or "resume"
  RUN_DATE     (optional) — YYYY-MM-DD; defaults to UTC today
  LIMIT        (optional) — cap retry attempts
  CSV_GCS_URI  (required) — gs:// URI of the properties CSV
  BUCKET_NAME  (required) — bucket for artifact download/upload

Flow:
  1. Read RETRY_MODE, RUN_DATE, LIMIT from env
  2. Determine target run_date (env or today)
  3. Download gs://{bucket}/runs/{run_date}/ → /tmp/data/runs/{run_date}/
     (gsutil -m cp -r)
  4. Download CSV → /tmp/properties.csv
  5. Exec: python ma_poc/scripts/jugnu_retry_runner.py
         --retry-errors OR --resume  (based on RETRY_MODE)
         --run-date {run_date}
         --csv /tmp/properties.csv
         [--limit N if set]
  6. Upload any new artifacts back to gs://{bucket}/runs/{run_date}/retry-{timestamp}/
  7. Exit with runner's code.
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


def _upload_artifacts(bucket_name: str, run_date: str, timestamp: str) -> None:
    local_run_dir = Path(f"/tmp/data/runs/{run_date}")
    if not local_run_dir.exists():
        return
    dest = f"gs://{bucket_name}/runs/{run_date}/retry-{timestamp}/"
    gcs.upload_prefix(local_run_dir, dest)
    print(f"[retry_entry] Uploaded retry artifacts → {dest}", file=sys.stderr)


def main() -> None:
    retry_mode = os.environ.get("RETRY_MODE", "errors").lower()
    if retry_mode not in ("errors", "resume"):
        sys.exit(f"Invalid RETRY_MODE={retry_mode!r}; expected 'errors' or 'resume'")

    csv_gcs_uri = _require_env("CSV_GCS_URI")
    bucket_name = _require_env("BUCKET_NAME")
    run_date = os.environ.get("RUN_DATE") or date.today().isoformat()
    limit_str = os.environ.get("LIMIT")
    limit = int(limit_str) if limit_str else None

    print(f"[retry_entry] mode={retry_mode}, run_date={run_date}", file=sys.stderr)

    # 3. Download prior run artifacts
    run_gcs_prefix = f"gs://{bucket_name}/runs/{run_date}"
    local_run_dir = Path(f"/tmp/data/runs/{run_date}")
    _download_gcs_dir(run_gcs_prefix, local_run_dir)

    # 4. Download CSV
    csv_local = Path("/tmp/properties.csv")
    gcs.download_object(csv_gcs_uri, csv_local)

    # 5. Run the retry runner
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
    try:
        result = subprocess.run(cmd, check=False)
        runner_exit = result.returncode
    finally:
        _upload_artifacts(bucket_name, run_date, timestamp)

    sys.exit(runner_exit)


if __name__ == "__main__":
    from ma_poc.pms import scraper  # noqa: F401

    mode = os.environ.get("RETRY_MODE", "errors")
    if not os.environ.get("CSV_GCS_URI"):
        print(f"jugnu_retry_entry stub: mode={mode}")
        sys.exit(0)

    main()
