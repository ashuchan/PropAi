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
from typing import IO

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


def _drain_stream_to_queue(
    stream: IO[bytes] | None,
    label: str,
    q: queue.Queue[tuple[str, str] | None],
) -> None:
    """Pump lines from *stream* onto *q* as (label, text) tuples.

    *label* is "stdout" or "stderr" so the reader can tell them apart in
    diagnostics. Sentinel ``None`` is enqueued exactly once per stream on
    EOF so the reader learns the stream has closed.
    """
    if stream is None:
        q.put(None)
        return
    try:
        for raw in iter(stream.readline, b""):
            q.put((label, raw.decode("utf-8", errors="replace")))
    finally:
        q.put(None)


@contextmanager
def cloud_sql_proxy(project: str, instance: str, region: str = "us-central1") -> Generator[None, None, None]:
    """Spawn cloud-sql-proxy; yield when ready; terminate on exit.

    BOTH stdout and stderr are drained on background threads (cloud-sql-proxy
    v2 writes its ready message to stdout; v1 wrote to stderr). Times out
    after ``_PROXY_READY_TIMEOUT_SEC``; also exits early if the proxy
    process dies before writing "ready for new connections".
    """
    conn_name = f"{project}:{region}:{instance}"
    proc = subprocess.Popen(
        ["cloud-sql-proxy", "--port=5432", conn_name],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    q: queue.Queue[tuple[str, str] | None] = queue.Queue()
    pumps = [
        threading.Thread(
            target=_drain_stream_to_queue,
            args=(proc.stdout, "stdout", q),
            daemon=True,
        ),
        threading.Thread(
            target=_drain_stream_to_queue,
            args=(proc.stderr, "stderr", q),
            daemon=True,
        ),
    ]
    for p in pumps:
        p.start()

    try:
        ready = False
        seen: list[str] = []
        closed = 0  # number of streams that have reached EOF
        deadline = time.monotonic() + _PROXY_READY_TIMEOUT_SEC
        while time.monotonic() < deadline:
            try:
                item = q.get(timeout=0.5)
            except queue.Empty:
                # Nothing in the queue; check whether the proc has died.
                if proc.poll() is not None and closed >= len(pumps):
                    break
                continue
            if item is None:
                closed += 1
                # Both streams closed AND process exited → stop waiting.
                if closed >= len(pumps) and proc.poll() is not None:
                    break
                continue
            label, text = item
            seen.append(f"[{label}] {text.rstrip()}")
            if "ready for new connections" in text.lower():
                ready = True
                break

        if ready:
            yield
            return

        # Not ready. Disambiguate: did the proxy die, or did we just time out?
        if proc.poll() is not None:
            # Drain anything still buffered so the error surfaces it.
            while True:
                try:
                    item = q.get(timeout=0.2)
                except queue.Empty:
                    break
                if item is None:
                    continue
                label, text = item
                seen.append(f"[{label}] {text.rstrip()}")
            sys.exit(
                f"cloud-sql-proxy exited with code {proc.returncode} "
                f"before becoming ready. output:\n" + "\n".join(seen)
            )

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
            "proxy output so far:\n" + "\n".join(seen)
        )
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
        # Scheme must be `postgresql+psycopg://` — the project installs
        # psycopg v3 (`psycopg[binary]` in requirements.txt), not psycopg2.
        # A bare `postgresql://` URL resolves to the psycopg2 dialect in
        # SQLAlchemy and fails with ModuleNotFoundError at engine_from_config.
        # Matches the rest of the data_provider layer.
        env["DATABASE_URL"] = (
            f"postgresql+psycopg://{iam_user}@localhost:5432/jugnu?sslmode=disable"
        )
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
