# CLAUDE_TRIGGERS.md

**Goal:** Build the operator interface for manually invoking Cloud Run jobs — the scripts humans and Claude Code run when they want to scrape, retry, or smoke-test outside the nightly cron. This handoff owns the boundary between "a person wants a thing to happen" and "the GCP API gets called."

**Read before starting:**
- `scripts/jugnu_runner.py` — the CLI surface of the main pipeline (`--csv`, `--limit`, `--run-date`, `--schema-version`, `--proxy`)
- `scripts/jugnu_retry_runner.py` — the retry CLI surface (`--retry-errors`, `--resume`, `--run-date`, `--limit`)
- `CLAUDE_TERRAFORM.md` — the Cloud Run jobs this script triggers, and the naming convention (`jugnu-scrape-{env}`, `jugnu-retry-{env}`)
- `Jugnu_Deployment_Architecture_GCP.docx` §3 — the runtime estimate table (backs the `--target-hours` math)

**Prerequisite:** `CLAUDE_TERRAFORM.md` must be applied to staging. Without the Cloud Run jobs existing, these scripts have nothing to call.

---

## 1. Scope

What this handoff produces:

- `scripts/trigger_run.py` — trigger a scrape execution with tunable parallelism
- `scripts/trigger_retry.py` — trigger a retry execution
- `scripts/trigger_smoke.py` — trigger a canary execution (used by deploy workflow, but also runnable manually)
- `scripts/jugnu_shard_entry.py` — the Cloud Run task entry point that slices the CSV (the arch doc sketches this; we make it production-grade)
- `scripts/jugnu_retry_entry.py` — the retry job entry point
- `scripts/_trigger_common.py` — shared helpers: auth check, job status polling, exit code interpretation, structured output
- Unit tests: `tests/triggers/test_trigger_run.py`, `test_trigger_retry.py`, `test_shard_entry.py`

What this handoff does **not** produce:
- The GitHub Actions workflows that call these scripts (that's `CLAUDE_DEPLOY.md`)
- The retry logic itself inside `jugnu_retry_runner.py` (already exists)
- Any change to `jugnu_runner.py` (treat as immutable — we wrap it, we don't modify it)

---

## 2. Design principles

These are the rules every script in this handoff follows. If a design decision later conflicts with one of these, the rule wins.

**A. The script is the only supported interface.** No `gcloud run jobs execute` in runbooks, no ad-hoc Python in notebooks. If someone needs a new trigger pattern, they add a flag to the script. The script is the choke point for validation, logging, and safety clamps.

**B. Validate before you call GCP.** Every script checks: auth is valid, target job exists, arguments are sane, env flag matches the current `gcloud config`. Fail fast locally, not 30 seconds in when GCP returns a permission error.

**C. One effect per invocation.** A trigger script submits one job execution and waits for it (or polls, if async). It does not loop, batch, or chain. Composition is the operator's job, not the script's.

**D. Structured output.** stdout for human reading, a final JSON line on stderr for Claude Code / CI to parse. Exit codes carry meaning (see §7).

**E. No implicit environment.** `--env staging|prod` is always required. No defaults. The day someone runs a staging test against prod because the default was wrong is a day that doesn't have to happen.

**F. The script never creates resources.** It only executes existing jobs. If the job doesn't exist, that's a Terraform problem, not a script problem — error out with a clear "run terraform apply first."

---

## 3. The parallelism math lives in one place

Wall-clock estimation is the most error-prone bit of this handoff. Centralize it in `_trigger_common.py` so `trigger_run.py` and the deploy workflow and any future tooling all use the same calculation.

```python
# scripts/_trigger_common.py
"""Shared helpers for trigger scripts."""
from __future__ import annotations

import math
import subprocess
import sys
from dataclasses import dataclass
from typing import Literal

# Calibration from the arch doc:
#   ~14.6s weighted per property, 10 browsers per task
# => one task clears ~(3600/14.6)*10 ≈ 2465 properties/hour, sustained
# Conservative floor (allow headroom for warm-up, occasional slow pages):
BASELINE_PROPS_PER_TASK_PER_HOUR = 2000

# Safety ceilings tied to Cloud SQL tier max_connections. Each shard opens
# ~2 SQLAlchemy connections during the end-of-run sync_run_to_pg wave, so
# ceilings leave ~2× burst headroom under the tier's max_connections.
# Keep in lock-step with the tier table in
# infra/terraform/variables.tf :: variable "db_tier".
MAX_TASKS_F1_MICRO        = 10    # db-f1-micro:       ~25 max_conn  (shared / 0.6 GiB)
MAX_TASKS_G1_SMALL        = 20    # db-g1-small:       ~50 max_conn  (shared / 1.7 GiB)
MAX_TASKS_CUSTOM_1_3840   = 50    # db-custom-1-3840:  ~100 max_conn (1 vCPU / 3.75 GiB)
MAX_TASKS_CUSTOM_2_7680   = 100   # db-custom-2-7680:  ~200 max_conn (2 vCPU / 7.5 GiB) — prod default
MAX_TASKS_CUSTOM_4_15360  = 200   # db-custom-4-15360: ~400 max_conn (4 vCPU / 15 GiB)
ABSOLUTE_MAX_TASKS        = 200   # Cloud Run parallelism ceiling we're willing to pay for


@dataclass(frozen=True)
class TaskPlan:
    tasks: int
    estimated_hours: float
    warning: str | None  # human-readable warning to print; None if clean


def plan_tasks(
    *,
    total_properties: int,
    target_hours: float | None = None,
    explicit_tasks: int | None = None,
    db_tier: Literal["f1-micro", "g1-small", "larger"] = "f1-micro",
) -> TaskPlan:
    """
    Pick task count. Exactly one of target_hours or explicit_tasks must be set.

    Raises:
        ValueError: if both or neither are set, or values are negative/zero.
    """
    if (target_hours is None) == (explicit_tasks is None):
        raise ValueError("exactly one of target_hours or explicit_tasks required")
    if target_hours is not None and target_hours <= 0:
        raise ValueError("target_hours must be positive")
    if explicit_tasks is not None and explicit_tasks <= 0:
        raise ValueError("explicit_tasks must be positive")

    ceiling = {
        "f1-micro": MAX_TASKS_F1_MICRO,
        "g1-small": MAX_TASKS_G1_SMALL,
        "larger":   ABSOLUTE_MAX_TASKS,
    }[db_tier]

    if explicit_tasks is not None:
        tasks = explicit_tasks
    else:
        tasks = math.ceil(total_properties / (BASELINE_PROPS_PER_TASK_PER_HOUR * target_hours))
        tasks = max(1, min(tasks, ceiling))

    estimated_hours = total_properties / (BASELINE_PROPS_PER_TASK_PER_HOUR * tasks)

    warning = None
    if tasks > ceiling:
        warning = (
            f"{tasks} tasks requested but db tier '{db_tier}' safely supports "
            f"≤ {ceiling}. Either upgrade the database or lower parallelism."
        )
    elif tasks > MAX_TASKS_F1_MICRO and db_tier == "f1-micro":
        warning = (
            f"{tasks} tasks on db-f1-micro risks connection exhaustion. "
            f"Consider upgrading to db-g1-small for runs this parallel."
        )

    return TaskPlan(tasks=tasks, estimated_hours=estimated_hours, warning=warning)


def verify_gcloud_auth(required_project: str) -> None:
    """Fail fast if gcloud isn't authenticated or pointed at the wrong project."""
    result = subprocess.run(
        ["gcloud", "config", "get-value", "project"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        sys.exit("gcloud not authenticated. Run: gcloud auth login")
    current = result.stdout.strip()
    if current != required_project:
        sys.exit(
            f"gcloud project is '{current}', expected '{required_project}'. "
            f"Run: gcloud config set project {required_project}"
        )


def emit_structured_result(result: dict) -> None:
    """Write a single-line JSON result to stderr for CI/Claude Code parsing."""
    import json
    print(f"RESULT:{json.dumps(result)}", file=sys.stderr)
```

Two things worth pointing out:

- **The baseline is `2000/hour`, not the calculated `2465/hour`.** Headroom matters — picking the optimistic number means every run with a few slow sites ends up outside the 4h ceiling. The arch doc itself uses a similar conservative pattern.
- **`db_tier` is an argument, not a lookup.** The script can't read the SQL tier from anywhere reliably without an extra API call that adds latency and failure modes. Operator passes it; Terraform's tfvars are the source of truth for what to pass. If it drifts, the clamp fails safe (capped lower than necessary), not unsafe.

---

## 4. `scripts/trigger_run.py` — the main scrape trigger

**Interface:**

```
Usage:
  python scripts/trigger_run.py --env {staging|prod} [OPTIONS]

One of --tasks or --target-hours is required:
  --tasks N              Explicit task count (1-40)
  --target-hours H       Pick tasks to fit wall clock target (0.5-8)

Optional:
  --csv GCS_URI          Override CSV location (default: bucket/property-list/properties.csv)
  --limit N              Pass through to jugnu_runner (useful for ad-hoc tests)
  --run-date YYYY-MM-DD  Override run date (default: today in UTC)
  --db-tier TIER         For parallelism safety clamp (default: f1-micro)
  --wait / --no-wait     Wait for completion (default: --wait)
  --dry-run              Print what would be executed, don't submit

Examples:
  # Full prod run, fit into 2h
  python scripts/trigger_run.py --env prod --target-hours 2

  # Explicit 10-way parallelism on staging
  python scripts/trigger_run.py --env staging --tasks 10

  # Test against 50 properties on staging, no wait
  python scripts/trigger_run.py --env staging --tasks 2 --limit 50 --no-wait

  # What would this do?
  python scripts/trigger_run.py --env prod --target-hours 1 --dry-run
```

**Behavior:**

1. Parse args; reject if both or neither of `--tasks`/`--target-hours` given.
2. `verify_gcloud_auth(project_for_env(env))`.
3. Call `plan_tasks(...)` — get `TaskPlan`. If `warning` is non-None, print to stderr. If `tasks > ceiling`, exit non-zero unless `--force` was given (don't add `--force` in v1 — make operators upgrade the DB instead).
4. Count properties in the CSV (either local preview or `gsutil ls -L $CSV | awk '/Content-Length/ ...'` + a partial download to count rows; keep it simple — for v1, require operator to pass `--total-props` if using a non-default CSV).
5. Print a plan summary:
   ```
   Plan:
     Environment:       prod
     Job:               jugnu-scrape-prod
     CSV:               gs://jugnu-raw-prod/property-list/properties.csv
     Properties:        5000
     Tasks:             10
     Est. wall clock:   ~2.5h
     Run date:          2026-04-19
   ```
6. If `--dry-run`, exit 0 here.
7. Otherwise prompt for confirmation (skip if stdout is not a TTY — CI calls this non-interactively with `--yes`):
   ```
   Proceed? [y/N]:
   ```
8. Submit:
   ```bash
   gcloud run jobs execute jugnu-scrape-{env} \
     --project={project_id} \
     --region={region} \
     --tasks={N} \
     --update-env-vars=RUN_DATE={date},CSV_GCS_URI={csv},LIMIT={limit_or_empty} \
     {"--wait" if --wait else ""} \
     --format=json
   ```
9. Parse the execution name from the JSON response; print a console URL operators can click.
10. If `--wait`, the above command blocks; check exit code and emit structured result. If `--no-wait`, emit structured result immediately with the execution name for later polling.

**Structured result shape:**

```json
{
  "status": "SUCCESS" | "FAILED" | "SUBMITTED",
  "execution_name": "jugnu-scrape-prod-abc123",
  "tasks": 10,
  "env": "prod",
  "console_url": "https://console.cloud.google.com/run/jobs/executions/details/..."
}
```

**Safety clamps (must be enforced, not warned):**
- `--target-hours < 0.5` → reject ("below this, you're paying for warm-up, not work")
- `--target-hours > 8` → reject ("use the nightly scheduler instead of a manual trigger")
- `--tasks > ABSOLUTE_MAX_TASKS` (200) → reject (absolute ceiling; anything more is a code change, not a CLI flag)
- `--tasks > <per-tier ceiling>` for the supplied `--db-tier` → reject (generic check against `_TIER_CEILINGS`; covers f1-micro >10, g1-small >20, custom-1-3840 >50, custom-2-7680 >100, custom-4-15360 >200)

---

## 5. `scripts/trigger_retry.py` — the retry trigger

**Interface:**

```
Usage:
  python scripts/trigger_retry.py --env {staging|prod} --mode {errors|resume} [OPTIONS]

Required:
  --env {staging|prod}
  --mode {errors|resume}     errors = retry failed properties from a run
                             resume = resume an interrupted run

Optional:
  --run-date YYYY-MM-DD      Which run to retry (default: most recent)
  --limit N                  Cap retry attempts
  --wait / --no-wait         Wait for completion (default: --wait)
  --dry-run                  Print what would be executed

Examples:
  # Retry failures from yesterday's prod run
  python scripts/trigger_retry.py --env prod --mode errors --run-date 2026-04-18

  # Resume an interrupted staging run
  python scripts/trigger_retry.py --env staging --mode resume
```

**Behavior:**

Straightforward relative to `trigger_run.py` — no parallelism math needed (the retry job is `parallelism = 1`). The script just:

1. Verifies auth.
2. If `--run-date` omitted, queries Cloud SQL (via the auth proxy) for the most recent `run_date` in the `run_ledger` table. If the ledger doesn't exist yet, require the flag.
3. Submits the execution:
   ```bash
   gcloud run jobs execute jugnu-retry-{env} \
     --update-env-vars=RETRY_MODE={mode},RUN_DATE={date},LIMIT={limit_or_empty} \
     {--wait flag}
   ```
4. Emits structured result in the same shape as `trigger_run.py`.

**Why a separate script instead of a `--mode retry` flag on `trigger_run.py`:** different jobs, different arg surfaces, different defaults. Merging them leads to "which flags apply in which mode" documentation sprawl. Keep them split.

---

## 6. `scripts/trigger_smoke.py` — the deploy-time canary

**Interface:**

```
Usage:
  python scripts/trigger_smoke.py --env {staging|prod}

Optional:
  --timeout-seconds N      Fail if run takes longer than N seconds (default: 600)
  --min-rows N             Fail if fewer than N rows written to DB (default: 3)
```

**Behavior:**

Submits the scrape job against `gs://jugnu-raw-{env}/canary/properties.csv` with `--tasks 1`, waits for completion, then runs a set of post-conditions:

1. Job execution exit code is 0
2. At least `--min-rows` rows exist in `properties` table for today's `run_date`
3. The shard's `dlq.jsonl` in GCS is empty (or absent)
4. The run's `events.jsonl` contains zero `severity=ERROR` events

Any failure → exit non-zero with a structured failure result naming the failed post-condition. This is the script that `CLAUDE_DEPLOY.md`'s workflow calls to gate a deploy as healthy.

---

## 7. Exit codes

Every trigger script uses the same exit code convention. Document this in each script's module docstring.

| Code | Meaning | When |
|---|---|---|
| 0 | Success | Job ran and succeeded (or `--no-wait` submission accepted) |
| 1 | Job failed | Job ran but exited non-zero |
| 2 | Usage error | Bad flags, missing required args |
| 3 | Precondition failed | gcloud not authenticated, wrong project, job doesn't exist |
| 4 | Safety clamp rejected | Task count too high for DB tier, target hours out of bounds, etc. |
| 5 | Post-condition failed | (smoke only) Job succeeded but validation checks didn't |
| 6 | Timeout | `--wait` exceeded an internal timeout |
| 130 | SIGINT | Operator ctrl-C'd |

---

## 8. `scripts/jugnu_shard_entry.py` — Cloud Run task entry point

This is the shard-slicing wrapper that replaces the sketch in the arch doc §2.2 with a production version. It runs **inside** each Cloud Run task, not on the operator's machine.

**Key fixes vs the arch doc sketch:**

- `total` is derived from the CSV, not hardcoded to 5000
- Uses ceiling division so no rows silently drop when `total % task_count != 0`
- Reads CSV from GCS (env var `CSV_GCS_URI`), not a mounted `/data/` path — simpler deployment, no volume config needed
- Passes through `RUN_DATE` and `LIMIT` env vars to `jugnu_runner.py` when set
- Uploads per-shard artifacts (`events.jsonl`, `dlq.jsonl`, `cost_ledger.db`) to GCS at end-of-run — even on failure, because that's when artifacts matter most
- Exits non-zero on runner failure, but **only after** the artifact upload completes

**Contract:**

```python
"""
scripts/jugnu_shard_entry.py — Cloud Run task entry point.

Environment variables consumed:
  CLOUD_RUN_TASK_INDEX   (auto-set by Cloud Run) — this task's index
  CLOUD_RUN_TASK_COUNT   (auto-set by Cloud Run) — total tasks in execution
  CSV_GCS_URI            (required) — gs:// URI of the properties CSV
  RUN_DATE               (optional) — YYYY-MM-DD; defaults to UTC today
  LIMIT                  (optional) — cap properties per shard; useful for smoke tests
  SCHEMA_VERSION         (optional) — v1 or v2; defaults to v1
  BUCKET_NAME            (required) — bucket for artifact upload

Flow:
  1. Download CSV from GCS to /tmp/properties.csv
  2. Slice rows for this shard (ceiling division)
  3. Write slice to /tmp/shard_{idx}.csv
  4. Exec: python scripts/jugnu_runner.py --csv /tmp/shard_{idx}.csv ...
  5. Upload /tmp/runs/{run_date}/events.jsonl, dlq.jsonl, cost_ledger.db
     to gs://{bucket}/runs/{run_date}/shard_{idx}/
  6. Exit with the runner's exit code.

Artifact upload happens in a `try/finally` — ensures artifacts exist even when
the runner crashes. This is how Claude Code debugs failed shards.
"""
```

Do **not** modify `jugnu_runner.py`. Subprocess call only. This keeps the runner's "never-fail contract" (from the refactor plan) independent of cloud-specific concerns.

---

## 9. `scripts/jugnu_retry_entry.py` — retry job entry point

Thin wrapper around `jugnu_retry_runner.py`. Reads `RETRY_MODE`, `RUN_DATE`, `LIMIT` from env; translates to `jugnu_retry_runner.py` CLI flags; downloads the run artifacts from GCS to `/tmp/data/runs/{run_date}/` first so the retry runner can find them.

The "download before running" pattern (Option B from the previous conversation turn) keeps `jugnu_retry_runner.py` unchanged.

**Flow:**

```python
"""
1. Read RETRY_MODE, RUN_DATE, LIMIT from env
2. Determine target run_date (env or "latest" lookup via DB)
3. Download gs://{bucket}/runs/{run_date}/ → /tmp/data/runs/{run_date}/
   (gsutil -m cp -r)
4. Download CSV → /tmp/properties.csv
5. Exec: python scripts/jugnu_retry_runner.py
         --retry-errors OR --resume  (based on RETRY_MODE)
         --run-date {run_date}
         --csv /tmp/properties.csv
         [--limit N if set]
6. Upload any new artifacts back to gs://{bucket}/runs/{run_date}/retry-{timestamp}/
7. Exit with runner's code.
"""
```

---

## 10. Tests

Every script gets unit tests. The parallelism math in particular is the kind of logic that's easy to get subtly wrong and hard to catch in production.

**`tests/triggers/test_trigger_run.py`:**
- `test_plan_tasks_rejects_both_flags` — ValueError when both target_hours and explicit_tasks set
- `test_plan_tasks_rejects_neither_flag` — ValueError when neither set
- `test_plan_tasks_ceiling_division` — 5001 properties / 2h → 2 tasks (not 1, not 3)
- `test_plan_tasks_clamps_to_db_ceiling` — 5000 / 0.5h asks for 5 but f1-micro allows 10, so returns 5; 5000 / 0.25h asks for 10, still returns 10
- `test_plan_tasks_warns_above_f1_micro_threshold` — tasks=12 on f1-micro returns a warning
- `test_trigger_run_rejects_without_env` — SystemExit code 2
- `test_trigger_run_dry_run_does_not_call_gcloud` — mock subprocess; assert no call
- `test_trigger_run_emits_structured_result` — assert stderr line parses as JSON and has expected keys

**`tests/triggers/test_shard_entry.py`:**
- `test_shard_slice_divides_evenly` — 1000 rows / 5 tasks → each gets 200
- `test_shard_slice_handles_remainder` — 1003 rows / 5 tasks → tasks 0-3 get 201, task 4 gets 199
- `test_shard_zero_rows_graceful` — empty CSV doesn't crash; produces empty shard CSV; runner exits cleanly
- `test_shard_uploads_artifacts_on_success` — mock GCS; assert 3 expected files uploaded
- `test_shard_uploads_artifacts_on_runner_failure` — make runner exit 1; assert artifacts still uploaded; assert exit code 1

**`tests/triggers/test_retry_entry.py`:**
- `test_retry_mode_errors_translates_to_flag` — RETRY_MODE=errors → subprocess called with `--retry-errors`
- `test_retry_mode_resume_translates_to_flag` — RETRY_MODE=resume → `--resume`
- `test_retry_mode_invalid_rejects` — RETRY_MODE=garbage → exit code 2

Coverage target: **90% line coverage on `scripts/_trigger_common.py`, 80% on the entry scripts.** Safety-critical code deserves higher coverage than the average project.

---

## 11. Gates

| Gate | Check | How to verify |
|---|---|---|
| TR-1 | All unit tests pass | `pytest tests/triggers/` |
| TR-2 | Coverage thresholds met | `pytest tests/triggers/ --cov=scripts._trigger_common --cov=scripts.trigger_run --cov-fail-under=80` |
| TR-3 | `ruff check scripts/trigger_*.py scripts/_trigger_common.py scripts/jugnu_*_entry.py` clean | — |
| TR-4 | `mypy --strict scripts/_trigger_common.py` clean | — |
| TR-5 | `--help` output of each trigger script ends up in repo docs | `docs/OPERATOR_RUNBOOK.md` has a section per script with the `--help` output verbatim |
| TR-6 | End-to-end smoke on staging | `python scripts/trigger_run.py --env staging --tasks 1 --limit 3 --wait` exits 0, writes ≥3 rows to staging DB |
| TR-7 | End-to-end retry smoke on staging | Same run, then `python scripts/trigger_retry.py --env staging --mode resume --wait` exits 0 (should be no-op — nothing to retry) |
| TR-8 | Dry-run mode is side-effect-free | `python scripts/trigger_run.py --env prod --target-hours 2 --dry-run` makes zero GCP API calls (verify via `gcloud` audit log or by running with network disabled) |
| TR-9 | Invalid args exit with code 2 | `python scripts/trigger_run.py --env prod` (no tasks, no target-hours) exits 2 |
| TR-10 | Safety clamps reject unsafe configs | `python scripts/trigger_run.py --env prod --tasks 20 --db-tier f1-micro` exits 4 with clear message |

---

## 12. Non-negotiables

- **No direct `gcloud` calls outside `_trigger_common.py`.** All subprocess calls to gcloud go through a single helper so they can be mocked uniformly in tests.
- **No `os.system` or `shell=True`.** `subprocess.run(..., check=False)` with a list of args, always.
- **No prints to stdout in error paths.** Errors go to stderr. stdout is reserved for the plan summary and the final status line.
- **No catching `BaseException` or bare `except:`.** Catch specific exceptions or let them propagate.
- **No global state.** Every function takes the env as an argument; no module-level `ENV = "prod"` nonsense.
- **No secrets in logs.** If the proxy URL contains credentials, redact before printing.

---

## 13. Open questions to resolve with operator before starting

- **Confirmation prompt:** always on by default, or only in prod? Recommendation: always on, with `--yes` to skip. Staging runs are rare enough that the extra keypress is fine.
- **Where does the CSV live when the property list is being edited?** If a PR just updated `data/property-list/properties.csv` and the deploy workflow pushes it to GCS, is there a window where `trigger_run.py` run manually would pick up a stale CSV? Recommendation: document the invariant "GCS is the source of truth; local is a working copy" in the operator runbook.
- **Does `trigger_run.py` need a `--csv` flag that points at a local file?** Uploading the local file to a temp GCS location before invoking? Recommendation: no for v1. Operators edit locally, push via PR, then trigger. One path is simpler than two.
- **Multi-developer staging:** if two developers trigger staging scrapes in the same day, they'll collide on `run_date`. Accept this (they share a staging DB, first writer wins on idempotent UPSERTs) or partition by developer? Recommendation: accept it; staging is shared by design.

---

## 14. When this handoff is complete

Claude Code has:
1. Implemented every script in §1
2. All gates in §11 pass
3. Written `docs/OPERATOR_RUNBOOK.md` covering: how to trigger a manual scrape, how to retry failures, how to read the structured output, common failure modes and their fixes
4. Walked through the runbook with a human operator — someone who did not write the code successfully triggers a staging run end-to-end following only the runbook

The runbook walkthrough is the real gate. If someone can't use the scripts from the docs, the docs (not the scripts) need another pass.
