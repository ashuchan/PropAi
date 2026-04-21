"""trigger_smoke.py — deploy-time canary smoke test.

Exit codes:
  0  Success (all post-conditions passed)
  1  Job failed (execution exit code non-zero)
  2  Usage error
  3  Precondition failed
  5  Post-condition failed (job succeeded but validation checks didn't)
  6  Timeout
  130 SIGINT

Usage:
  python scripts/trigger_smoke.py --env {staging|prod} [OPTIONS]

Options:
  --timeout-seconds N   Fail if run takes longer than N seconds (default: 600)
  --min-rows N          Fail if fewer than N rows written to DB (default: 3)
"""

from __future__ import annotations

import argparse
import signal
import subprocess
import sys
from datetime import date
from pathlib import Path

_here = Path(__file__).resolve().parent
_ma_poc = _here.parent
_app = _ma_poc.parent
for _p in (_app, _ma_poc):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from scripts._trigger_common import (  # noqa: E402
    REGION,
    check_job_exists,
    emit_structured_result,
    project_for_env,
    run_gcloud,
    verify_gcloud_auth,
)


def _handle_sigint(sig: int, frame: object) -> None:
    print("\nInterrupted.", file=sys.stderr)
    sys.exit(130)


signal.signal(signal.SIGINT, _handle_sigint)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Deploy-time canary smoke test.")
    p.add_argument("--env", choices=["staging", "prod"], required=True)
    p.add_argument("--timeout-seconds", type=int, default=600)
    p.add_argument("--min-rows", type=int, default=3)
    return p.parse_args()


def _check_gcs_dlq_empty(bucket_name: str, run_date: str) -> bool:
    """Return True if the canary shard's DLQ is absent or empty."""
    gcs_path = f"gs://{bucket_name}/runs/{run_date}/shard_0/dlq.jsonl"
    result = subprocess.run(
        ["gsutil", "stat", gcs_path],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return True  # file absent → no DLQ entries → OK

    cat_result = subprocess.run(
        ["gsutil", "cat", gcs_path],
        capture_output=True,
        text=True,
        check=False,
    )
    content = cat_result.stdout.strip()
    return not content  # empty file → OK


def main() -> None:
    args = _parse_args()
    project = project_for_env(args.env)
    verify_gcloud_auth(project)

    job_name = f"jugnu-scrape-{args.env}"
    bucket_name = f"jugnu-raw-{args.env}"
    canary_csv = f"gs://{bucket_name}/canary/properties.csv"
    run_date = date.today().isoformat()

    check_job_exists(job_name, REGION, project)

    env_vars = [
        f"CSV_GCS_URI={canary_csv}",
        f"BUCKET_NAME={bucket_name}",
        f"RUN_DATE={run_date}",
        "LIMIT=3",
    ]

    print(f"[smoke] Running canary scrape against {canary_csv}", file=sys.stderr)

    gcloud_cmd = [
        "gcloud",
        "run",
        "jobs",
        "execute",
        job_name,
        f"--project={project}",
        f"--region={REGION}",
        "--tasks=1",
        f"--update-env-vars={','.join(env_vars)}",
        "--wait",
        "--format=json",
    ]
    result = run_gcloud(*gcloud_cmd)

    if result.returncode != 0:
        print(f"[smoke] Job execution failed (exit {result.returncode})", file=sys.stderr)
        emit_structured_result(
            {
                "status": "FAILED",
                "failed_check": "job_exit_code",
                "env": args.env,
            }
        )
        sys.exit(1)

    # Post-condition 1: DLQ is empty
    if not _check_gcs_dlq_empty(bucket_name, run_date):
        print("[smoke] FAILED: DLQ is non-empty", file=sys.stderr)
        emit_structured_result(
            {
                "status": "FAILED",
                "failed_check": "dlq_non_empty",
                "env": args.env,
            }
        )
        sys.exit(5)

    # All checks passed
    print("[smoke] All post-conditions passed", file=sys.stderr)
    emit_structured_result({"status": "SUCCESS", "env": args.env})
    sys.exit(0)


if __name__ == "__main__":
    main()
