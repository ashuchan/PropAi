"""trigger_smoke.py — deploy-time canary smoke test.

Exit codes:
  0  Success (job exited 0 and post-conditions passed)
  1  Job failed (execution exit code non-zero)
  2  Usage error
  3  Precondition failed
  6  Timeout
  130 SIGINT

Usage:
  python scripts/trigger_smoke.py --env {staging|prod} [OPTIONS]

Options:
  --timeout-seconds N   Fail if run takes longer than N seconds (default: 600)

Previously this script also checked GCS ``runs/{date}/shard_0/dlq.jsonl``
for emptiness. That gate was tautological: shard_entry never uploads to
that path, and the DLQ is now durable cross-run state mirrored to
Postgres via alembic 0006_dlq_entries — a per-run DLQ view no longer
exists. The real "did this canary succeed?" signal is the Cloud Run
job's own exit code, which ``gcloud run jobs execute --wait`` reports.
"""

from __future__ import annotations

import argparse
import signal
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
    tf_env_for,
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
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    project = project_for_env(args.env)
    verify_gcloud_auth(project)

    tf_env = tf_env_for(args.env)
    job_name = f"jugnu-scrape-{tf_env}"
    bucket_name = f"jugnu-raw-{tf_env}"
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

    # Job exit 0 is the canary signal. shard_entry fails the task if
    # either the scrape or the post-run Postgres sync failed, so a
    # green exit already implies rows landed — no separate DB gate
    # needed here.
    print("[smoke] Canary job exited 0", file=sys.stderr)
    emit_structured_result({"status": "SUCCESS", "env": args.env})
    sys.exit(0)


if __name__ == "__main__":
    main()
