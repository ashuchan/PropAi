"""trigger_run.py — trigger a Jugnu scrape execution on Cloud Run.

Exit codes:
  0  Success (job ran and succeeded, or --no-wait submission accepted)
  1  Job failed
  2  Usage error
  3  Precondition failed (auth, wrong project, job not found)
  4  Safety clamp rejected
  6  Timeout
  130 SIGINT

Usage:
  python scripts/trigger_run.py --env {staging|prod} (--tasks N | --target-hours H) [OPTIONS]

Options:
  --tasks N              Explicit task count (1-40)
  --target-hours H       Pick tasks to fit wall clock target (0.5-8)
  --csv GCS_URI          Override CSV location
  --limit N              Cap properties (useful for ad-hoc tests)
  --run-date YYYY-MM-DD  Override run date (default: today UTC)
  --db-tier TIER         f1-micro | g1-small | larger (default: f1-micro)
  --total-props N        Total property count (required when --csv is a non-default URI)
  --wait / --no-wait     Wait for completion (default: --wait)
  --dry-run              Print plan, don't submit
  --yes                  Skip confirmation prompt (for CI)

Examples:
  python scripts/trigger_run.py --env prod --target-hours 2
  python scripts/trigger_run.py --env staging --tasks 10
  python scripts/trigger_run.py --env staging --tasks 2 --limit 50 --no-wait
  python scripts/trigger_run.py --env prod --target-hours 1 --dry-run
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
from datetime import date
from pathlib import Path

# Path bootstrap
_here = Path(__file__).resolve().parent
_ma_poc = _here.parent
_app = _ma_poc.parent
for _p in (_app, _ma_poc):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from scripts._trigger_common import (  # noqa: E402
    ABSOLUTE_MAX_TASKS,
    REGION,
    TaskPlan,
    check_job_exists,
    emit_structured_result,
    plan_tasks,
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
        description="Trigger a Jugnu scrape execution on Cloud Run.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "--env",
        choices=["staging", "prod"],
        required=True,
        help="Target environment (no default — always explicit)",
    )
    group = p.add_mutually_exclusive_group()
    group.add_argument("--tasks", type=int, metavar="N", help="Explicit task count (1-40)")
    group.add_argument(
        "--target-hours", type=float, metavar="H", help="Pick tasks to fit wall clock target (0.5-8)"
    )
    p.add_argument("--csv", metavar="GCS_URI", help="Override default property-list CSV location")
    p.add_argument("--limit", type=int, metavar="N", help="Cap properties per shard (useful for tests)")
    p.add_argument("--run-date", metavar="YYYY-MM-DD", help="Override run date (default: today UTC)")
    p.add_argument(
        "--db-tier",
        choices=["f1-micro", "g1-small", "larger"],
        default="f1-micro",
        help="DB tier for parallelism safety clamp",
    )
    p.add_argument(
        "--total-props",
        type=int,
        metavar="N",
        help="Total property count (required when --csv is non-default)",
    )
    p.add_argument(
        "--wait", dest="wait", action="store_true", default=True, help="Wait for job completion (default)"
    )
    p.add_argument("--no-wait", dest="wait", action="store_false", help="Submit and return immediately")
    p.add_argument("--dry-run", action="store_true", help="Print plan, do not submit")
    p.add_argument("--yes", action="store_true", help="Skip confirmation prompt (for CI)")
    return p.parse_args()


def _enforce_safety_clamps(args: argparse.Namespace) -> None:
    """Enforce hard limits; exit 4 on violation."""
    if args.target_hours is not None:
        if args.target_hours < 0.5:
            print("target-hours < 0.5: below this, you're paying for warm-up, not work", file=sys.stderr)
            sys.exit(4)
        if args.target_hours > 8:
            print("target-hours > 8: use the nightly scheduler instead of a manual trigger", file=sys.stderr)
            sys.exit(4)
    if args.tasks is not None:
        if args.tasks > ABSOLUTE_MAX_TASKS:
            print(
                f"tasks > {ABSOLUTE_MAX_TASKS}: absolute ceiling; change the code, not the flag",
                file=sys.stderr,
            )
            sys.exit(4)
        if args.tasks > 15 and args.db_tier == "f1-micro":
            print(
                "tasks > 15 on db-f1-micro risks connection exhaustion; upgrade db tier or lower tasks",
                file=sys.stderr,
            )
            sys.exit(4)


def main() -> None:
    args = _parse_args()

    if args.tasks is None and args.target_hours is None:
        print("One of --tasks or --target-hours is required", file=sys.stderr)
        sys.exit(2)

    _enforce_safety_clamps(args)

    project = project_for_env(args.env)

    if not args.dry_run:
        verify_gcloud_auth(project)

    job_name = f"jugnu-scrape-{args.env}"
    bucket_name = f"jugnu-raw-{args.env}"
    csv_uri = args.csv or f"gs://{bucket_name}/property-list/properties.csv"
    run_date = args.run_date or date.today().isoformat()

    # Resolve total property count
    total_props = args.total_props
    if total_props is None:
        if args.csv:
            print("--total-props is required when overriding --csv", file=sys.stderr)
            sys.exit(2)
        # Default CSV: use a placeholder for dry-run; for real runs we could count via gsutil
        # For simplicity, default to 5000 (typical prod list size) — operator adjusts via --total-props
        total_props = 5000

    # Plan
    try:
        plan: TaskPlan = plan_tasks(
            total_properties=total_props,
            target_hours=args.target_hours,
            explicit_tasks=args.tasks,
            db_tier=args.db_tier,
        )
    except ValueError as e:
        print(str(e), file=sys.stderr)
        sys.exit(2)

    if plan.warning:
        print(f"WARNING: {plan.warning}", file=sys.stderr)

    # Print plan summary
    print(f"""
Plan:
  Environment:       {args.env}
  Job:               {job_name}
  CSV:               {csv_uri}
  Properties:        {total_props}
  Tasks:             {plan.tasks}
  Est. wall clock:   ~{plan.estimated_hours:.1f}h
  Run date:          {run_date}
""")

    if args.dry_run:
        emit_structured_result({"status": "DRY_RUN", "tasks": plan.tasks, "env": args.env})
        sys.exit(0)

    # Verify job exists
    check_job_exists(job_name, REGION, project)

    # Confirm
    if not args.yes and sys.stdin.isatty():
        try:
            answer = input("Proceed? [y/N]: ").strip().lower()
        except EOFError:
            answer = ""
        if answer not in ("y", "yes"):
            print("Aborted.", file=sys.stderr)
            sys.exit(0)

    # Build update-env-vars string
    env_vars: list[str] = [
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
        f"--tasks={plan.tasks}",
        f"--update-env-vars={','.join(env_vars)}",
        "--format=json",
    ]
    if args.wait:
        gcloud_cmd.append("--wait")

    result = run_gcloud(*gcloud_cmd)

    # Parse execution name from JSON response
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
                "tasks": plan.tasks,
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
                "tasks": plan.tasks,
                "env": args.env,
                "console_url": console_url,
            }
        )
        sys.exit(0)


if __name__ == "__main__":
    main()
