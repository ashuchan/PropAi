"""trigger_proxy_smoke.py — minimal proxy validation against Cloud Run.

Single-task invocation of ``jugnu-scrape-{env}`` with
``SMOKE_MODE=proxy_check``. The shard entry short-circuits to
``check_proxy.py``, which fetches ``https://lumtest.com/myip.json``
through the first URL in ``PROXY_POOL_URLS`` and exits non-zero if the
ASN comes back as Google's 15169 (meaning the proxy was bypassed and
traffic went via Cloud Run egress).

Unlike ``trigger_smoke.py``, this test:

- Does NOT upload a CSV fixture (the scrape pipeline is bypassed).
- Does NOT write to Postgres (no sync runs).
- Runs exactly ONE task — parallelism here adds cost (each task makes a
  real BrightData request) without adding signal.

Exit codes:
  0   Proxy is working (check_proxy exited 0)
  1   Execution completed but check_proxy failed — see logs for the ASN
  2   Usage error
  3   Precondition failed (auth, wrong project, job not found)
  6   Timed out waiting for execution to complete
  130 SIGINT

Usage:
  python ma_poc/scripts/trigger_proxy_smoke.py --env {staging|prod}
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from pathlib import Path

_here = Path(__file__).resolve().parent
_ma_poc = _here.parent
_app = _ma_poc.parent
for _p in (_app, _ma_poc):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from scripts._common.trigger import (  # noqa: E402
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
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--env", choices=["staging", "prod"], required=True)
    p.add_argument(
        "--timeout-seconds",
        type=int,
        default=300,
        help="Max wall-clock for the full flow. Default: 5 min.",
    )
    return p.parse_args()


def _execute_job(
    *,
    job_name: str,
    project: str,
    timeout_seconds: int,
) -> tuple[int, str, str]:
    """Kick off a single-task execution with SMOKE_MODE=proxy_check.

    Returns (exit_code, execution_name, completion_status).
      exit_code: 0 on terminal state reached, 6 on timeout, other on gcloud kickoff failure
      completion_status: "True" | "False" | "" (empty if timeout)

    The check_proxy exit code is surfaced through the Cloud Run task's
    container exit code, which maps to the Completed condition's status:
    ``True`` = check passed, ``False`` = check failed (any of exits 1-4).
    """
    print(
        f"[proxy-smoke] Executing {job_name} tasks=1 SMOKE_MODE=proxy_check",
        file=sys.stderr,
    )

    result = run_gcloud(
        "gcloud",
        "run",
        "jobs",
        "execute",
        job_name,
        f"--project={project}",
        f"--region={REGION}",
        "--tasks=1",
        "--update-env-vars=SMOKE_MODE=proxy_check",
        "--async",
        "--format=value(metadata.name)",
    )
    if result.returncode != 0:
        print(f"[proxy-smoke] gcloud execute failed: {result.stderr}", file=sys.stderr)
        return result.returncode, "", ""

    execution_name = result.stdout.strip()
    print(f"[proxy-smoke] Execution started: {execution_name}", file=sys.stderr)

    deadline = time.monotonic() + timeout_seconds
    poll_interval = 15
    while time.monotonic() < deadline:
        describe = run_gcloud(
            "gcloud",
            "run",
            "jobs",
            "executions",
            "describe",
            execution_name,
            f"--project={project}",
            f"--region={REGION}",
            "--format=json",
        )
        if describe.returncode != 0:
            print(f"[proxy-smoke] describe transient error: {describe.stderr}", file=sys.stderr)
            time.sleep(poll_interval)
            continue
        try:
            body = json.loads(describe.stdout)
        except json.JSONDecodeError:
            time.sleep(poll_interval)
            continue

        status = body.get("status") or {}
        conds = status.get("conditions") or []
        completed = next((c for c in conds if c.get("type") == "Completed"), None)
        print(
            f"[proxy-smoke] …succeeded={status.get('succeededCount', 0)} "
            f"failed={status.get('failedCount', 0)} "
            f"running={status.get('runningCount', 0)}",
            file=sys.stderr,
        )

        # Cloud Run v2 attaches Completed with status="Unknown" before
        # terminal state; only True/False indicate the execution is done.
        status_str = (completed or {}).get("status")
        if status_str in ("True", "False"):
            return 0, execution_name, status_str
        time.sleep(poll_interval)

    print(f"[proxy-smoke] Timeout after {timeout_seconds}s", file=sys.stderr)
    return 6, execution_name, ""


def _fetch_check_proxy_logs(*, execution_name: str, project: str) -> list[str]:
    """Pull the ``[proxy-check] …`` lines so operators can see the ASN/IP
    without hunting through Cloud Logging."""
    log_filter = (
        f'resource.type=cloud_run_job AND '
        f'labels."run.googleapis.com/execution_name"="{execution_name}" AND '
        f'textPayload:"[proxy-check]"'
    )
    result = run_gcloud(
        "gcloud",
        "logging",
        "read",
        log_filter,
        f"--project={project}",
        "--limit=20",
        "--format=value(textPayload)",
        "--order=asc",
    )
    if result.returncode != 0:
        print(f"[proxy-smoke] log fetch failed: {result.stderr}", file=sys.stderr)
        return []
    return [line for line in result.stdout.splitlines() if line.strip()]


def main() -> int:
    args = _parse_args()
    project = project_for_env(args.env)
    verify_gcloud_auth(project)

    tf_env = tf_env_for(args.env)
    job_name = f"jugnu-scrape-{tf_env}"
    check_job_exists(job_name, REGION, project)

    exec_rc, execution_name, completion_status = _execute_job(
        job_name=job_name,
        project=project,
        timeout_seconds=args.timeout_seconds,
    )
    if exec_rc == 6:
        emit_structured_result({"status": "TIMEOUT", "env": args.env})
        return 6
    if exec_rc != 0 or not execution_name:
        emit_structured_result({"status": "EXEC_FAILED", "env": args.env})
        return 1

    # Surface the actual check output — the ASN + IP are the whole point.
    log_lines = _fetch_check_proxy_logs(execution_name=execution_name, project=project)
    for line in log_lines:
        print(f"[proxy-smoke]   {line}", file=sys.stderr)

    # Completed.status == "True" means container exited 0, i.e. proxy check OK.
    if completion_status == "True":
        emit_structured_result(
            {
                "status": "SUCCESS",
                "env": args.env,
                "execution": execution_name,
            }
        )
        print("\n[proxy-smoke] PASSED: proxy is working", file=sys.stderr)
        return 0

    emit_structured_result(
        {
            "status": "PROXY_CHECK_FAILED",
            "env": args.env,
            "execution": execution_name,
        }
    )
    print(
        "\n[proxy-smoke] FAILED: check_proxy exited non-zero. "
        "See the [proxy-check] lines above for the reason (ASN=15169 → bypass, "
        "fetch error → bad creds/firewall, empty PROXY_POOL_URLS → secret not wired).",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
