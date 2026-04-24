"""trigger_smoke.py — deploy-time smoke test for the Cloud Run scrape job.

Runs a real, sharded Cloud Run job execution and validates that every
shard's data landed in Cloud SQL. This is the last gate before a deploy
is tagged ``known-good``.

What it does
------------
1. Uploads a 6-row smoke fixture CSV to ``gs://{bucket}/smoke-test/{ts}/``
   containing fast-failing ``.invalid`` URLs (RFC 2606) — no DNS resolution,
   no real sites hit, no LLM credits burned.
2. Triggers ``jugnu-scrape-{env}`` with ``--tasks=3 --parallelism=3`` and
   ``LIMIT=2`` so each task scrapes exactly 2 properties (6 total).
3. Waits for execution to reach a terminal state.
4. Fetches structured logs for the execution, groups ``PG sync complete``
   entries by ``CLOUD_RUN_TASK_INDEX``, and asserts:
     - Each task_index in {0, 1, 2} emitted at least one ``PG sync complete``
       line (proves the exit-code gate fix: sync ran despite runner exit 1).
     - Each task's summary shows ``state_from_snapshots=2``,
       ``ledger_from_snapshots=2``, ``scrape_events_from_snapshots=2``,
       ``extractions_from_snapshots=2`` — proves the 6 properties actually
       landed in all four tables, not just the snapshots table.
5. Cleans up the smoke CSV + smoke rows (scoped by the unique run_date
   and canonical_id prefix so prod data is never touched).

Why ``.invalid`` URLs
---------------------
- Deterministic: DNS NXDOMAIN in milliseconds, no network flake.
- Free: no LLM calls, no proxy usage.
- Still exercises the real multi-shard write path end-to-end: every
  property gets a FAILED_UNREACHABLE verdict that the sync persists
  to properties + units + run_ledger + scrape_events + extraction_results.

Cloud Run will retry failed tasks once (``max_retries=1`` in terraform),
so the log query has to group by ``task_index`` — otherwise 6 "PG sync
complete" lines (2 per task) would be mistaken for "6 shards synced".

Exit codes
----------
  0   All 3 shards synced + per-shard counts match expectations
  1   Job execution itself failed in an unexpected way
  2   Usage error
  3   Precondition failed (auth, wrong project, job not found)
  5   Job ran but post-conditions failed (writes didn't land)
  6   Timeout
  130 SIGINT

Usage
-----
  python ma_poc/scripts/trigger_smoke.py --env {staging|prod}

Options
-------
  --timeout-seconds N   Fail if flow exceeds N seconds (default: 900 = 15 min).
  --skip-cleanup        Leave smoke CSV + rows for debugging.
  --cleanup-only --run-date VALUE
                        Delete smoke rows for a specific run_date (e.g.
                        after a previous smoke left leaked resources).
"""

from __future__ import annotations

import argparse
import ast
import csv
import io
import json
import signal
import sys
import time
from datetime import UTC, datetime
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


SHARD_COUNT = 3
PROPS_PER_SHARD = 2
TOTAL_PROPS = SHARD_COUNT * PROPS_PER_SHARD  # 6


def _handle_sigint(sig: int, frame: object) -> None:
    print("\nInterrupted.", file=sys.stderr)
    sys.exit(130)


signal.signal(signal.SIGINT, _handle_sigint)


# ── CLI ─────────────────────────────────────────────────────────────────────


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--env", choices=["staging", "prod"], required=True)
    p.add_argument(
        "--timeout-seconds",
        type=int,
        default=900,
        help="Max wall-clock for the full flow. Default: 15 min.",
    )
    p.add_argument(
        "--skip-cleanup",
        action="store_true",
        help="Leave smoke CSV + rows for debugging.",
    )
    p.add_argument(
        "--cleanup-only",
        action="store_true",
        help="Skip execution; only clean up a prior run's leaked rows.",
    )
    p.add_argument(
        "--run-date",
        type=str,
        default=None,
        help="RUN_DATE passed to the job (required with --cleanup-only).",
    )
    return p.parse_args()


# ── Fixture CSV ─────────────────────────────────────────────────────────────


def _build_smoke_csv(run_date: str) -> tuple[bytes, list[str]]:
    """Produce a 6-row CSV body with fast-failing URLs.

    ``.invalid`` is an IANA-reserved TLD (RFC 2606) guaranteed not to
    resolve. L1 fetcher returns HARD_FAIL within the DNS timeout; the
    verdict lands as ``FAILED_UNREACHABLE``. No LLM / proxy cost.

    Returns (csv_bytes, canonical_ids). CIDs carry the run_date suffix
    so two smoke runs in parallel don't collide, and cleanup can target
    exactly these rows.
    """
    suffix = run_date.replace("smoke-", "").replace("-", "")
    cids: list[str] = []
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["apartmentid", "name", "city", "state", "zip", "website"])
    for shard_idx in range(SHARD_COUNT):
        for i in range(PROPS_PER_SHARD):
            cid = f"SMOKE-{suffix}-S{shard_idx}-P{i}"
            cids.append(cid)
            writer.writerow(
                [
                    cid,
                    f"Smoke Shard {shard_idx} Prop {i}",
                    "Smokeville",
                    "AZ",
                    "85001",
                    f"http://{cid.lower()}.invalid/",
                ]
            )
    return buf.getvalue().encode("utf-8"), cids


# ── GCS helpers ─────────────────────────────────────────────────────────────


def _upload_smoke_csv(bucket: str, gcs_key: str, body: bytes) -> str:
    """Upload the smoke CSV. Returns the gs:// URI."""
    from google.cloud import storage

    client = storage.Client()
    blob = client.bucket(bucket).blob(gcs_key)
    blob.upload_from_string(body, content_type="text/csv")
    uri = f"gs://{bucket}/{gcs_key}"
    print(f"[smoke] Uploaded smoke CSV → {uri} ({len(body)} bytes)", file=sys.stderr)
    return uri


def _delete_smoke_csv(bucket: str, gcs_key: str) -> None:
    from google.cloud import storage

    try:
        client = storage.Client()
        client.bucket(bucket).blob(gcs_key).delete()
        print(f"[smoke] Deleted smoke CSV gs://{bucket}/{gcs_key}", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 — cleanup must not mask other failures
        print(f"[smoke] WARN delete smoke CSV: {exc}", file=sys.stderr)


# ── Cloud Run execution ─────────────────────────────────────────────────────


def _execute_job(
    *,
    job_name: str,
    project: str,
    csv_uri: str,
    bucket: str,
    run_date: str,
    timeout_seconds: int,
) -> tuple[int, str]:
    """Kick the job off in ``--async`` mode and poll until terminal.

    Using ``--async`` + polling (instead of ``--wait``) gives us the
    execution name up front and lets us surface interim status. It
    also means a transient gcloud hiccup during the wait doesn't
    collapse the whole test — we just retry the describe.

    Returns (exit_code, execution_name). exit_code semantics:
      0  execution reached a terminal state (success OR expected
         failure — with invalid URLs we expect task-level failures,
         see module docstring)
      6  timed out waiting for terminal state
      other: gcloud kickoff itself failed
    """
    env_vars = ",".join(
        [
            f"CSV_GCS_URI={csv_uri}",
            f"BUCKET_NAME={bucket}",
            f"RUN_DATE={run_date}",
            f"LIMIT={PROPS_PER_SHARD}",
        ]
    )
    print(
        f"[smoke] Executing {job_name} tasks={SHARD_COUNT} limit={PROPS_PER_SHARD} "
        f"run_date={run_date}",
        file=sys.stderr,
    )

    # ``gcloud run jobs execute`` accepts ``--tasks`` for per-execution
    # task-count override but has no ``--parallelism`` flag — parallelism
    # is a property of the job itself (set in terraform via
    # ``parallelism = var.default_task_count``). Prod's job already has
    # parallelism=10, so SHARD_COUNT=3 tasks run fully in parallel
    # without overriding here.
    result = run_gcloud(
        "gcloud",
        "run",
        "jobs",
        "execute",
        job_name,
        f"--project={project}",
        f"--region={REGION}",
        f"--tasks={SHARD_COUNT}",
        f"--update-env-vars={env_vars}",
        "--async",
        "--format=value(metadata.name)",
    )
    if result.returncode != 0:
        print(f"[smoke] gcloud execute failed: {result.stderr}", file=sys.stderr)
        return result.returncode, ""
    execution_name = result.stdout.strip()
    print(f"[smoke] Execution started: {execution_name}", file=sys.stderr)

    deadline = time.monotonic() + timeout_seconds
    poll_interval = 20
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
            print(f"[smoke] describe transient error: {describe.stderr}", file=sys.stderr)
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
            f"[smoke] …succeeded={status.get('succeededCount', 0)} "
            f"failed={status.get('failedCount', 0)} "
            f"running={status.get('runningCount', 0)}",
            file=sys.stderr,
        )
        # Cloud Run v2 attaches the "Completed" condition at execution
        # creation with status="Unknown", then flips it to "True" or
        # "False" only at genuine terminal state. Treating the mere
        # presence of the condition as terminal makes the smoke test
        # exit on its first 20-second poll with 0/0/0 task counts —
        # what the 2026-04-24 prod run showed. Only True/False are
        # terminal; Unknown means still running.
        status_str = (completed or {}).get("status")
        if status_str in ("True", "False"):
            msg = (completed or {}).get("message", "")
            if status_str == "True":
                print("[smoke] Execution completed OK", file=sys.stderr)
            else:
                print(
                    f"[smoke] Execution completed with task failures "
                    f"(expected — smoke fixture: {msg})",
                    file=sys.stderr,
                )
            return 0, execution_name
        time.sleep(poll_interval)

    print(f"[smoke] Timeout after {timeout_seconds}s", file=sys.stderr)
    return 6, execution_name


# ── Log verification ────────────────────────────────────────────────────────


def _verify_via_logs(
    *, execution_name: str, project: str
) -> tuple[bool, dict[str, dict[str, int]]]:
    """Fetch structured logs and group ``PG sync complete`` by task_index.

    Cloud Run retries failed tasks once (``max_retries=1`` in terraform).
    With invalid-URL fixtures every task's runner exits 1, so each
    task_index appears up to twice in the logs. Counting raw lines
    would give 6 and falsely pass on a 2-shard run. Grouping by
    ``labels."run.googleapis.com/task_index"`` gives the correct
    "distinct shards that synced" view.

    Returns (all_3_shards_synced, {task_index: summary_dict}).
    """
    log_filter = (
        f'resource.type=cloud_run_job AND '
        f'labels."run.googleapis.com/execution_name"="{execution_name}" AND '
        f'textPayload:"PG sync complete"'
    )
    result = run_gcloud(
        "gcloud",
        "logging",
        "read",
        log_filter,
        f"--project={project}",
        "--limit=100",
        "--format=json",
    )
    if result.returncode != 0:
        print(f"[smoke] log fetch failed: {result.stderr}", file=sys.stderr)
        return False, {}

    try:
        entries = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        print(f"[smoke] log JSON decode failed: {exc}", file=sys.stderr)
        return False, {}

    by_task: dict[str, dict[str, int]] = {}
    for entry in entries:
        labels = entry.get("labels") or {}
        task_idx = labels.get("run.googleapis.com/task_index")
        if task_idx is None:
            continue
        text = entry.get("textPayload") or ""
        _, _, dict_body = text.partition("PG sync complete:")
        summary = _loose_parse_dict(dict_body.strip())
        # gcloud returns most-recent-first; keep the LATEST success
        # per task_index (if a task's retry also synced, pick either —
        # they should be identical against the same FS output).
        if task_idx not in by_task:
            by_task[task_idx] = summary

    expected = {str(i) for i in range(SHARD_COUNT)}
    ok = set(by_task.keys()) == expected
    return ok, by_task


def _loose_parse_dict(text: str) -> dict[str, int]:
    """Safely parse a Python-repr dict; empty dict on any error."""
    try:
        parsed = ast.literal_eval(text)
        if isinstance(parsed, dict):
            return parsed
    except (ValueError, SyntaxError):
        pass
    return {}


def _per_shard_counts_match(summaries: dict[str, dict[str, int]]) -> bool:
    """True iff every shard's summary shows the expected counts.

    Each shard processes ``PROPS_PER_SHARD`` properties; the sync
    persists one row per property across four derive-from-snapshots
    tables. The per-shard expectation is exactly ``PROPS_PER_SHARD``
    for each of those counters.
    """
    expected = PROPS_PER_SHARD
    keys = (
        "state_from_snapshots",
        "ledger_from_snapshots",
        "scrape_events_from_snapshots",
        "extractions_from_snapshots",
    )
    return all(
        all(s.get(k) == expected for k in keys)
        for s in summaries.values()
    )


# ── Cleanup ─────────────────────────────────────────────────────────────────


def _cleanup_gcs(bucket: str, gcs_key: str) -> None:
    _delete_smoke_csv(bucket, gcs_key)


# ── Main flow ───────────────────────────────────────────────────────────────


def main() -> int:
    args = _parse_args()
    project = project_for_env(args.env)
    verify_gcloud_auth(project)

    tf_env = tf_env_for(args.env)
    job_name = f"jugnu-scrape-{tf_env}"
    bucket = f"jugnu-raw-{tf_env}"

    if args.cleanup_only:
        if not args.run_date:
            print("--cleanup-only requires --run-date", file=sys.stderr)
            return 2
        # Only GCS to clean up — DB cleanup requires direct Cloud SQL
        # access that's out of scope for CI. Prod DB rows are scoped by
        # the ``smoke-{ts}`` run_date prefix; drop them manually with a
        # psql one-liner after a leaked run.
        ts_suffix = args.run_date.replace("smoke-", "")
        gcs_prefix = f"smoke-test/{ts_suffix}/"
        print(
            f"[smoke] Cleanup-only: GCS prefix {gcs_prefix}; for DB rows "
            f"run: DELETE FROM run_reports WHERE run_date = '{args.run_date}' "
            f"(plus cascade to run_issues/run_ledger/property_snapshots/etc.)",
            file=sys.stderr,
        )
        try:
            from google.cloud import storage

            client = storage.Client()
            for blob in client.list_blobs(bucket, prefix=gcs_prefix):
                blob.delete()
                print(f"[smoke] deleted gs://{bucket}/{blob.name}", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001
            print(f"[smoke] WARN cleanup failed: {exc}", file=sys.stderr)
        return 0

    check_job_exists(job_name, REGION, project)

    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    run_date = args.run_date or f"smoke-{ts}"
    gcs_key = f"smoke-test/{ts}/properties.csv"

    csv_body, expected_cids = _build_smoke_csv(run_date)
    csv_uri = _upload_smoke_csv(bucket, gcs_key, csv_body)

    try:
        exec_rc, execution_name = _execute_job(
            job_name=job_name,
            project=project,
            csv_uri=csv_uri,
            bucket=bucket,
            run_date=run_date,
            timeout_seconds=args.timeout_seconds,
        )
        if exec_rc == 6:
            emit_structured_result(
                {"status": "TIMEOUT", "env": args.env, "run_date": run_date}
            )
            return 6
        if exec_rc != 0 or not execution_name:
            emit_structured_result(
                {"status": "EXEC_FAILED", "env": args.env, "run_date": run_date}
            )
            return 1

        logs_ok, summaries = _verify_via_logs(
            execution_name=execution_name, project=project
        )
        print(
            f"[smoke] Distinct task_indices that synced: {sorted(summaries.keys())} "
            f"({len(summaries)}/{SHARD_COUNT})",
            file=sys.stderr,
        )
        for task_idx in sorted(summaries.keys()):
            s = summaries[task_idx]
            print(
                f"[smoke]   shard {task_idx}: "
                f"state_from_snapshots={s.get('state_from_snapshots')}, "
                f"ledger={s.get('ledger_from_snapshots')}, "
                f"events={s.get('scrape_events_from_snapshots')}, "
                f"extractions={s.get('extractions_from_snapshots')}",
                file=sys.stderr,
            )

        per_shard_ok = _per_shard_counts_match(summaries)

        if logs_ok and per_shard_ok:
            emit_structured_result(
                {
                    "status": "SUCCESS",
                    "env": args.env,
                    "run_date": run_date,
                    "shards_synced": len(summaries),
                    "props_per_shard": PROPS_PER_SHARD,
                }
            )
            print(
                f"\n[smoke] PASSED: {len(summaries)}/{SHARD_COUNT} shards synced, "
                f"{PROPS_PER_SHARD} props/shard confirmed in 4 tables each",
                file=sys.stderr,
            )
            return 0

        emit_structured_result(
            {
                "status": "POST_CONDITION_FAILED",
                "env": args.env,
                "run_date": run_date,
                "distinct_shards_synced": sorted(summaries.keys()),
                "per_shard_counts_ok": per_shard_ok,
                "per_shard_summaries": summaries,
            }
        )
        return 5
    finally:
        if not args.skip_cleanup:
            _cleanup_gcs(bucket, gcs_key)
            # Prod DB cleanup is intentionally left to operators — the
            # rows are scoped by run_date='smoke-{ts}' and canonical_id
            # prefix 'SMOKE-{ts}-*' so they're easy to drop manually
            # and never collide with real data.
            print(
                f"[smoke] Note: DB rows for run_date={run_date} remain. "
                f"Drop with: DELETE FROM run_reports WHERE run_date='{run_date}'; "
                f"(plus the 9 other tables — see sync_run_to_pg.py docstring).",
                file=sys.stderr,
            )
        else:
            print(
                f"[smoke] --skip-cleanup: leaving csv={csv_uri}, run_date={run_date}",
                file=sys.stderr,
            )


if __name__ == "__main__":
    sys.exit(main())
