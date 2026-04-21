"""
scripts/migrate.py — run Alembic migrations through the Cloud SQL Auth Proxy.

Usage:
  python scripts/migrate.py --env staging up
  python scripts/migrate.py --env staging status
  python scripts/migrate.py --env prod up --to 00a1b2c3
  python scripts/migrate.py --env staging down --steps 1

The script:
  1. Verifies gcloud auth and project
  2. Ensures Cloud SQL instance is running (handles stop-when-idle)
  3. Starts the Cloud SQL Auth Proxy in a subprocess on localhost:5432
  4. Sets DATABASE_URL to use the proxy + IAM auth
  5. Invokes alembic
  6. Always stops the proxy cleanly in a finally block
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

# Mapping from env to (project_id, sql_instance_name).
# Keep in sync with infra/terraform/envs/*.tfvars.
# CI overrides these via the GCP_PROJECT_ID_* secrets.
ENV_CONFIG: dict[str, dict[str, str]] = {
    "staging": {
        "project": os.environ.get("GCP_PROJECT_ID_STAGING", "jugnu-staging-<unique>"),
        "instance": "jugnu-db-staging",
        "region": "us-central1",
    },
    "prod": {
        "project": os.environ.get("GCP_PROJECT_ID_PROD", "jugnu-prod-<unique>"),
        "instance": "jugnu-db-prod",
        "region": "us-central1",
    },
}

# Repo root (2 levels up from ma_poc/scripts/)
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
ALEMBIC_CONFIG = _REPO_ROOT / "infra" / "sql" / "alembic.ini"


def ensure_sql_running(project: str, instance: str) -> None:
    """Handle the stop-when-idle trap; return only when SQL is RUNNABLE."""
    result = subprocess.run(
        ["gcloud", "sql", "instances", "describe", instance, f"--project={project}", "--format=value(state)"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        print(f"Failed to describe SQL instance {instance}: {result.stderr}", file=sys.stderr)
        sys.exit(3)

    state = result.stdout.strip()
    if state == "RUNNABLE":
        return

    print(f"SQL instance state: {state}; starting...", file=sys.stderr)
    subprocess.run(
        [
            "gcloud",
            "sql",
            "instances",
            "patch",
            instance,
            f"--project={project}",
            "--activation-policy=ALWAYS",
        ],
        check=True,
    )
    # Poll until ready (max 3 minutes)
    for _ in range(36):
        result = subprocess.run(
            [
                "gcloud",
                "sql",
                "instances",
                "describe",
                instance,
                f"--project={project}",
                "--format=value(state)",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.stdout.strip() == "RUNNABLE":
            print("SQL instance is RUNNABLE", file=sys.stderr)
            return
        time.sleep(5)
    sys.exit(f"Timeout waiting for {instance} to become RUNNABLE")


@contextmanager
def cloud_sql_proxy(project: str, instance: str, region: str = "us-central1") -> Generator[None, None, None]:
    """Spawn cloud-sql-proxy; yield when ready; terminate on exit."""
    conn_name = f"{project}:{region}:{instance}"
    proc = subprocess.Popen(
        ["cloud-sql-proxy", "--port=5432", conn_name],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        ready = False
        for _ in range(20):
            assert proc.stderr is not None
            line = proc.stderr.readline().decode("utf-8", errors="replace")
            if "ready for new connections" in line.lower():
                ready = True
                break
            time.sleep(0.5)
        if not ready:
            proc.terminate()
            sys.exit("cloud-sql-proxy failed to become ready")
        yield
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def main() -> None:
    p = argparse.ArgumentParser(
        description="Run Alembic migrations through Cloud SQL Auth Proxy.",
        epilog=__doc__,
    )
    p.add_argument("--env", choices=["staging", "prod"], required=True)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("up", help="Upgrade to head")
    sub.add_parser("status", help="Show current revision")
    down_p = sub.add_parser("down", help="Downgrade N steps")
    down_p.add_argument("--steps", type=int, default=1)
    upto_p = sub.add_parser("up-to", help="Upgrade to a specific revision")
    upto_p.add_argument("revision")
    args = p.parse_args()

    cfg = ENV_CONFIG[args.env]
    ensure_sql_running(cfg["project"], cfg["instance"])

    # Get the authenticated IAM identity (SA email or user email)
    result = subprocess.run(
        ["gcloud", "config", "get-value", "account"],
        capture_output=True,
        text=True,
        check=True,
    )
    iam_user = result.stdout.strip()

    with cloud_sql_proxy(cfg["project"], cfg["instance"], cfg["region"]):
        env = os.environ.copy()
        env["DATABASE_URL"] = f"postgresql://{iam_user}@localhost:5432/jugnu?sslmode=disable"
        cmd = ["alembic", "-c", str(ALEMBIC_CONFIG)]
        if args.cmd == "up":
            cmd += ["upgrade", "head"]
        elif args.cmd == "up-to":
            cmd += ["upgrade", args.revision]
        elif args.cmd == "down":
            cmd += ["downgrade", f"-{args.steps}"]
        elif args.cmd == "status":
            cmd += ["current", "-v"]
        sys.exit(subprocess.call(cmd, env=env))


if __name__ == "__main__":
    main()
