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
import queue
import subprocess
import sys
import threading
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
        # Instance name is "jugnu-db-production" to match prod.tfvars where
        # env = "production". The CI workflow input is the short form "prod";
        # the Terraform env string is the long form. Keep them in sync with
        # infra/terraform/envs/prod.tfvars.
        "project": os.environ.get("GCP_PROJECT_ID_PROD", "jugnu-prod-<unique>"),
        "instance": "jugnu-db-production",
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


_PROXY_READY_TIMEOUT_SEC = 30.0


def _drain_stderr_to_queue(
    proc: subprocess.Popen[bytes], q: queue.Queue[str | None]
) -> None:
    """Pump proc.stderr lines onto *q* in a thread, sentinel None on EOF."""
    assert proc.stderr is not None
    try:
        for raw in iter(proc.stderr.readline, b""):
            q.put(raw.decode("utf-8", errors="replace"))
    finally:
        q.put(None)


@contextmanager
def cloud_sql_proxy(project: str, instance: str, region: str = "us-central1") -> Generator[None, None, None]:
    """Spawn cloud-sql-proxy; yield when ready; terminate on exit.

    stderr is drained on a background thread so a silent-retry in the proxy
    (e.g. missing roles/cloudsql.client on the caller's SA) cannot hang the
    ready-wait via a blocking readline. Times out after
    ``_PROXY_READY_TIMEOUT_SEC``; also exits early if the proxy process
    dies before writing "ready for new connections".
    """
    conn_name = f"{project}:{region}:{instance}"
    proc = subprocess.Popen(
        ["cloud-sql-proxy", "--port=5432", conn_name],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    lines: queue.Queue[str | None] = queue.Queue()
    pump = threading.Thread(
        target=_drain_stderr_to_queue, args=(proc, lines), daemon=True
    )
    pump.start()

    try:
        ready = False
        seen: list[str] = []
        deadline = time.monotonic() + _PROXY_READY_TIMEOUT_SEC
        while time.monotonic() < deadline:
            # Proxy crashed before becoming ready → surface its output.
            if proc.poll() is not None:
                while True:
                    try:
                        ln = lines.get_nowait()
                    except queue.Empty:
                        break
                    if ln is None:
                        break
                    seen.append(ln.rstrip())
                sys.exit(
                    f"cloud-sql-proxy exited with code {proc.returncode} "
                    f"before becoming ready. stderr:\n" + "\n".join(seen)
                )
            try:
                line = lines.get(timeout=0.5)
            except queue.Empty:
                continue
            if line is None:
                break
            seen.append(line.rstrip())
            if "ready for new connections" in line.lower():
                ready = True
                break

        if not ready:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            sys.exit(
                "cloud-sql-proxy did not become ready in "
                f"{_PROXY_READY_TIMEOUT_SEC:.0f}s. Common causes:\n"
                "  - the caller's SA lacks roles/cloudsql.client on the project\n"
                "  - the caller's SA has no CLOUD_IAM_SERVICE_ACCOUNT user on "
                f"instance {instance}\n"
                "  - the instance is not RUNNABLE\n"
                "stderr so far:\n" + "\n".join(seen)
            )
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
