"""trigger_retry.py — trigger a Jugnu retry execution on Cloud Run.

Exit codes:
  0  Success
  1  Job failed
  2  Usage error
  3  Precondition failed
  4  Safety clamp rejected
  6  Timeout
  130 SIGINT

Usage:
  python scripts/trigger_retry.py --env {staging|prod} --mode {errors|resume} [OPTIONS]

Options:
  --env {staging|prod}       Target environment (required)
  --mode {errors|resume}     errors = retry failed properties; resume = resume interrupted run
  --run-date YYYY-MM-DD      Which run to retry (default: most recent)
  --limit N                  Cap retry attempts
  --wait / --no-wait         Wait for completion (default: --wait)
  --dry-run                  Print plan, do not submit
  --yes                      Skip confirmation prompt

Examples:
  python scripts/trigger_retry.py --env prod --mode errors --run-date 2026-04-18
  python scripts/trigger_retry.py --env staging --mode resume
"""

from __future__ import annotations

import argparse
import json
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
    verify_gcloud_auth,
)


def _handle_sigint(sig: int, frame: object) -> None:
    print("\nInterrupted.", file=sys.stderr)
    sys.exit(130)


signal.signal(signal.SIGINT, _handle_sigint)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Trigger a Jugnu retry execution on Cloud Run.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--env", choices=["staging", "prod"], required=True)
    p.add_argument(
        "--mode",
        choices=["errors", "resume"],
        required=True,
        help="errors = retry failed properties; resume = resume interrupted run",
    )
    p.add_argument("--run-date", metavar="YYYY-MM-DD", help="Target run date (default: most recent)")
    p.add_argument("--limit", type=int, metavar="N")
    p.add_argument("--wait", dest="wait", action="store_true", default=True)
    p.add_argument("--no-wait", dest="wait", action="store_false")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--yes", action="store_true")
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    project = project_for_env(args.env)

    if not args.dry_run:
        verify_gcloud_auth(project)

    job_name = f"jugnu-retry-{args.env}"
    bucket_name = f"jugnu-raw-{args.env}"
    run_date = args.run_date or date.today().isoformat()
    csv_uri = f"gs://{bucket_name}/property-list/properties.csv"

    print(f"""
Plan:
  Environment:  {args.env}
  Job:          {job_name}
  Mode:         {args.mode}
  Run date:     {run_date}
""")

    if args.dry_run:
        emit_structured_result({"status": "DRY_RUN", "mode": args.mode, "env": args.env})
        sys.exit(0)

    check_job_exists(job_name, REGION, project)

    if not args.yes and sys.stdin.isatty():
        try:
            answer = input("Proceed? [y/N]: ").strip().lower()
        except EOFError:
            answer = ""
        if answer not in ("y", "yes"):
            print("Aborted.", file=sys.stderr)
            sys.exit(0)

    env_vars = [
        f"RETRY_MODE={args.mode}",
        f"RUN_DATE={run_date}",
        f"CSV_GCS_URI={csv_uri}",
        f"BUCKET_NAME={bucket_name}",
    ]
    if args.limit:
        env_vars.append(f"LIMIT={args.limit}")

    gcloud_cmd = [
        "gcloud",
        "run",
        "jobs",
        "execute",
        job_name,
        f"--project={project}",
        f"--region={REGION}",
        f"--update-env-vars={','.join(env_vars)}",
        "--format=json",
    ]
    if args.wait:
        gcloud_cmd.append("--wait")

    result = run_gcloud(*gcloud_cmd)

    execution_name = "unknown"
    console_url = (
        f"https://console.cloud.google.com/run/jobs/details/{REGION}/{job_name}/executions?project={project}"
    )
    try:
        data = json.loads(result.stdout)
        if isinstance(data, dict):
            execution_name = data.get("metadata", {}).get("name", execution_name)
    except (json.JSONDecodeError, AttributeError):
        pass

    print(f"Console: {console_url}", file=sys.stderr)

    if args.wait:
        status = "SUCCESS" if result.returncode == 0 else "FAILED"
        emit_structured_result(
            {
                "status": status,
                "execution_name": execution_name,
                "mode": args.mode,
                "env": args.env,
                "console_url": console_url,
            }
        )
        sys.exit(0 if result.returncode == 0 else 1)
    else:
        emit_structured_result(
            {
                "status": "SUBMITTED",
                "execution_name": execution_name,
                "mode": args.mode,
                "env": args.env,
                "console_url": console_url,
            }
        )
        sys.exit(0)


if __name__ == "__main__":
    main()
