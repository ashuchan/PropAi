# Cloud Run jugnu-adhoc — Operator Manual

On-demand runner for any script in [`ma_poc/scripts/`](../ma_poc/scripts/). Reuses the `jugnu` image, worker SA, VPC connector, Cloud SQL Connector wiring, GCS bucket, and provider/proxy secrets — so any script gets Cloud SQL + GCS access for free.

Job name: **`jugnu-adhoc-{env}`** (e.g. `jugnu-adhoc-staging`, `jugnu-adhoc-prod`).

Dispatcher source: [`ma_poc/scripts/run_script.py`](../ma_poc/scripts/run_script.py).

---

## How to run from the Cloud Run console

1. Console → **Cloud Run** → **Jobs** tab → click `jugnu-adhoc-{env}`.
2. Click **EXECUTE WITH OVERRIDES** (button at the top, not the plain "EXECUTE").
3. Open **Container, variables & secrets** → **Variables & Secrets** → edit:
   - `SCRIPT_NAME` — module stem (e.g. `validate_outputs`). **No `.py`, no path.**
   - `SCRIPT_ARGS` — shell-quoted CLI args (e.g. `--csv config/properties.csv --limit 5`). Optional. Parsed via `shlex` so `'embedded spaces'` and `--key="quoted=value"` work.
4. Click **EXECUTE**.
5. After it starts: click the execution row → **LOGS** tab to watch live output.

The dispatcher prints a banner showing python version, hostname, every `CLOUD_RUN_*` injection, all wiring envs (DB, GCS, schema, provider), and a masked list of every `*_KEY`/`*_TOKEN`/`*_PASSWORD`/`*_SECRET` env var (last 4 chars only). At exit it prints elapsed wall time and the child exit code.

### gcloud CLI equivalent

```bash
gcloud run jobs execute jugnu-adhoc-{env} \
  --region=us-central1 \
  --update-env-vars="SCRIPT_NAME=validate_outputs,SCRIPT_ARGS=--csv config/properties.csv --limit 5" \
  --wait
```

Use `,` as the env-var separator. To embed a literal `,` in `SCRIPT_ARGS`, switch to `--env-vars-file=overrides.yaml`.

---

## Reading the logs

The banner is the first ~30 lines of every execution. Look for:

| Line | What it tells you |
|---|---|
| `script='<name>'` | What was dispatched. `<unset>` means SCRIPT_NAME was missing — fatal. |
| `CLOUD_RUN_EXECUTION` | Pin this to a specific console execution row. |
| `CLOUD_RUN_TASK_INDEX/COUNT/ATTEMPT` | Useful when comparing reruns. Adhoc runs at parallelism=1, so usually `0/1/1`. |
| `DATA_PROVIDER`, `DATABASE_URL`, `CLOUD_SQL_INSTANCE`, `BUCKET_NAME`, `SCHEMA_VERSION` | Wiring the script will see. If a backfill writes to the wrong DB, this is the first place to look. |
| `Secrets (masked)` | Every secret-shaped env var with `***<last4>` so you can confirm the right secret version was injected without leaking it. |
| `dispatching :` | Exact `python -m ...` command being run. Copy-paste-runnable locally. |
| `exit_code  :` | Final return code from the child script. The job inherits this — non-zero shows red in the console. |

If the child exits non-zero, the dispatcher logs `exit_code = <n>` and the Cloud Run execution is marked failed. Scripts wrapped in `try/except` that swallow errors will still exit 0 — judge by the script's own log output, not the job's exit code alone.

---

## Script catalog

All entries below specify the exact `SCRIPT_NAME` and `SCRIPT_ARGS` values to paste into the console. Args were verified against each script's argparse spec.

### Backfills

#### `backfill_artifacts_pg` — copy run artifact files into Postgres
Tables: `property_reports`, `llm_reports`, `llm_property_details`, `llm_diagnostics`. Idempotent (upsert).

| Field | Value |
|---|---|
| `SCRIPT_NAME` | `backfill_artifacts_pg` |
| `SCRIPT_ARGS` | `--run-date 2026-05-07` |

Optional flags: `--data-root data/v2`, `--url <DATABASE_URL override>`, `--dry-run`.

```
SCRIPT_ARGS=--run-date 2026-05-07 --dry-run
```

#### `backfill_floor_plan_id` — populate `units.floor_plan_id` for legacy rows
Idempotent: only fills NULLs. Uses the same `compute_floor_plan_id` helper as the writer.

| Field | Value |
|---|---|
| `SCRIPT_NAME` | `backfill_floor_plan_id` |
| `SCRIPT_ARGS` | *(empty)* |

Optional: `--batch-size 1000`, `--database-url <override>`, `--dry-run`, `-v`.

#### `backfill_pg` — copy FS data into the SQL provider
Reads `DATA_DIR` + `CONFIG_DIR` via `FileSystemDataProvider` and writes through PostgresDataProvider. **Note: requires FS data on the container — only useful if you've also mounted/uploaded the source.** For per-run sync use `sync_run_to_pg` instead (see below).

| Field | Value |
|---|---|
| `SCRIPT_NAME` | `backfill_pg` |
| `SCRIPT_ARGS` | `--target postgres` |

Sections (`--only`): `state,profiles,events,runs,extractions`. Date filter: `--run-dates 2026-04-14,2026-04-19`. Add `--dry-run` to preview.

#### `backfill_units_bed_bath` — fill `units.beds` / `units.baths` from `floor_plan_name`
Idempotent: never overwrites non-NULL values.

| Field | Value |
|---|---|
| `SCRIPT_NAME` | `backfill_units_bed_bath` |
| `SCRIPT_ARGS` | *(empty)* |

Optional: `--batch-size 500`, `--database-url <override>`, `--dry-run`, `-v`.

---

### Floor-plan comparison & disagreement export

#### `compare_floor_plans_csv` — match a floor-plan CSV against the DB
Writes `floor_plan_comparison_runs` + `floor_plan_comparison_rows`. Re-running with the same `--run-id` replaces results for that run.

| Field | Value |
|---|---|
| `SCRIPT_NAME` | `compare_floor_plans_csv` |
| `SCRIPT_ARGS` | `--csv ma_poc/config/Floorplan-comparisons.csv --run-id 2026-05-07-prod` |

Tuning: `--threshold 85` (default name match score), `--buffer 35` (sqft buffer in sqft), `--database-url`, `-v`.

> The CSV must already be present on the container image. To compare against a fresh CSV, upload it to GCS and either bake it into the image or load via a small adapter script — `gs://` paths are not directly supported.

#### `export_floor_plan_disagreements` — write review-queue rows for genuine CSV-vs-DB disagreements
Reads from a previous `compare_floor_plans_csv` run. Idempotent: re-running clears `status='pending'` rows for the run-id; reviewer-touched rows are preserved.

| Field | Value |
|---|---|
| `SCRIPT_NAME` | `export_floor_plan_disagreements` |
| `SCRIPT_ARGS` | `--run-id 2026-05-07-prod` |

Optional: `--min-score 95` (default 90, tighter than the matcher's 85), `--to-csv /tmp/disagreements.csv`, `--database-url`, `-v`.

---

### Email reports

> Email reports send via Gmail MCP using OAuth credentials baked into the image at `~/.gmail-mcp/credentials.json`. **If you haven't configured Gmail MCP in the image, all of these will fail.** Use `--dry-run` first to render HTML to stdout without sending.

#### `email_daily_report` — daily PropAi scrape report (headline dashboard)

| Field | Value |
|---|---|
| `SCRIPT_NAME` | `email_daily_report` |
| `SCRIPT_ARGS` | *(empty for today UTC, latest run)* |

Common variations:
```
SCRIPT_ARGS=--run-date 2026-05-07
SCRIPT_ARGS=--recipients alice@example.com,bob@example.com
SCRIPT_ARGS=--dry-run
SCRIPT_ARGS=--database-url postgresql+pg8000://user:pass@host:5432/proppy
```

#### `email_daily_failures_report` — operational drill-down with two XLSX attachments
Ships `scraped_units_{run_date}.xlsx` and `failed_properties_{run_date}.xlsx`. Falls back to GCS `gs://{BUCKET_NAME}/runs/{date}/shard_*/` when SQL has no rows for the date (older than 3-day retention).

| Field | Value |
|---|---|
| `SCRIPT_NAME` | `email_daily_failures_report` |
| `SCRIPT_ARGS` | *(empty)* |

Variations:
```
SCRIPT_ARGS=--run-date 2026-05-07
SCRIPT_ARGS=--use-gcs --run-date 2026-05-01
SCRIPT_ARGS=--out-dir /tmp/reports --dry-run
SCRIPT_ARGS=--recipients ops@example.com
```

#### `email_merge_analysis` — unit-merge-failure analysis for a run

| Field | Value |
|---|---|
| `SCRIPT_NAME` | `email_merge_analysis` |
| `SCRIPT_ARGS` | *(empty for latest)* |

```
SCRIPT_ARGS=--run-date 2026-05-05
SCRIPT_ARGS=--dry-run
SCRIPT_ARGS=--recipients alice@example.com
```

#### `email_refactor_plan` — one-shot internal email

| Field | Value |
|---|---|
| `SCRIPT_NAME` | `email_refactor_plan` |
| `SCRIPT_ARGS` | `--dry-run` |

---

### Sync / data movement

#### `sync_run_to_pg` — copy one run's FS output into Postgres
This is what every Jugnu shard calls at the end of its execution. Useful as a one-off when a previous shard's sync failed.

| Field | Value |
|---|---|
| `SCRIPT_NAME` | `sync_run_to_pg` |
| `SCRIPT_ARGS` | `--run-date 2026-05-07 --data-dir /tmp/data` |

Optional: `--config-dir <path>`, `--url <DATABASE_URL override>`, `--shard-id <id>`.

> The data-dir must be locally accessible on the container. If the run lives only in GCS, download it first inside a wrapper script.

#### `sync_cloud_to_local` — pull Cloud SQL into a local DB
Mostly intended for laptop use. Runs fine on Cloud Run if you point `--local-url` at a real target; otherwise leave it for local dev only.

| Field | Value |
|---|---|
| `SCRIPT_NAME` | `sync_cloud_to_local` |
| `SCRIPT_ARGS` | `--mode upsert --auth-type IAM --dry-run` |

Other useful flags: `--tables properties,units` (whitelist), `--mode mirror` (delete-and-replace), `--batch-size 1000`.

---

### Smoke tests / sanity checks

#### `smoke_test` — Jugnu import-sanity (offline, no network)
Verifies all five layer contracts import cleanly. Exits 0 if all 5 pass. **Takes no arguments.**

| Field | Value |
|---|---|
| `SCRIPT_NAME` | `smoke_test` |
| `SCRIPT_ARGS` | *(empty)* |

#### `smoke_rentcafe_direct` — production smoke for the RentCafe direct path
50-property network smoke. **Calls real network endpoints.** Designed to run from production-equivalent egress (i.e. Cloud Run, not laptop) for the IP-reputation comparison to be meaningful.

| Field | Value |
|---|---|
| `SCRIPT_NAME` | `smoke_rentcafe_direct` |
| `SCRIPT_ARGS` | `--blocked-input ma_poc/data/runs/2026-05-04/bot_blocked_properties_latest.json` |

Optional: `--csv path/to/properties.csv`, `--output /tmp/smoke.json`, `--sample-size 50`.

#### `validate_deployment` — image-time sanity
Same script that runs at `docker build` time. Verifies the floor-plan mapping, FloorplanCatalog, prompt templates, and module imports. Use it to sanity-check the deployed image after a rollout. **Takes no arguments.**

| Field | Value |
|---|---|
| `SCRIPT_NAME` | `validate_deployment` |
| `SCRIPT_ARGS` | *(empty)* |

#### `validate_outputs` — post-run metric validation
Reads `data/scrape_events.jsonl` and computes the BRD weekly-gate metrics (success rate, tier distribution, P95 page load, etc.).

| Field | Value |
|---|---|
| `SCRIPT_NAME` | `validate_outputs` |
| `SCRIPT_ARGS` | *(empty)* |

> Like `sync_run_to_pg`, this expects local FS data. If the run is GCS-only, download it inside a wrapper.

---

### Reports & analysis

#### `health_report` — health / extraction-quality report

| Field | Value |
|---|---|
| `SCRIPT_NAME` | `health_report` |
| `SCRIPT_ARGS` | `--report health --source db` |

Variations:
```
SCRIPT_ARGS=--report all --source gcs --bucket <bucket>
SCRIPT_ARGS=--report extraction-quality --run-date 2026-05-07
SCRIPT_ARGS=--report health --days 7 --json --out -
```

`--no-scan-events` skips the events.jsonl scan when sourcing from GCS (faster, drops wall-clock runtime metrics).

#### `generate_daily_report` — markdown daily report

| Field | Value |
|---|---|
| `SCRIPT_NAME` | `generate_daily_report` |
| `SCRIPT_ARGS` | `--run-date 2026-05-07` |

Optional: `--out /tmp/report.md`, `--database-url`, `--trend-window 14`.

#### `escalation_report` — fetch-tier escalation report

| Field | Value |
|---|---|
| `SCRIPT_NAME` | `escalation_report` |
| `SCRIPT_ARGS` | *(empty for latest run)* |

Optional: `--run-dir /tmp/data/runs/2026-05-07`.

#### `escalation_baseline` — generate ESCALATION_BASELINE.md from latest run

| Field | Value |
|---|---|
| `SCRIPT_NAME` | `escalation_baseline` |
| `SCRIPT_ARGS` | *(empty)* |

Optional: `--data-dir /tmp/data`, `--out /tmp/ESCALATION_BASELINE.md`.

#### `replay` — replay extraction for a single property
Useful to inspect a previous-run's raw HTML and re-run the extractor against it.

| Field | Value |
|---|---|
| `SCRIPT_NAME` | `replay` |
| `SCRIPT_ARGS` | `--cid 12345 --date 2026-05-07` |

Optional: `--rerun` (re-run extractor on the saved HTML), `--out /tmp/replay.md`, `--runs-root <path>`, `--html-root <path>`.

#### `cluster_retry` — analyze / retry PMC-portal failures

| Field | Value |
|---|---|
| `SCRIPT_NAME` | `cluster_retry` |
| `SCRIPT_ARGS` | `--analyze ma_poc/data/runs/2026-05-07/properties.json` |

Or:
```
SCRIPT_ARGS=--retry example.com --run-date 2026-05-07
```

#### `failure_debug_summary` — per-property failure debug bundles
For every property whose `run_ledger` status is not `SUCCESS` on the given day, builds a self-contained triage bundle combining Cloud SQL state and Cloud Storage artifacts. Each bundle includes the latest `scrape_events`, all `run_issues`, the full `scrape_profile` (maturity, blocked_endpoints, llm_field_mappings), the `property_snapshot.payload` with `_meta` / `_extract_result` / `_explored_links` / `_raw_api_responses` / `_filtered_apis` / `_winning_page_url` / `_llm_interactions`, the `dlq_entries` row if parked, and `llm_property_details` / `llm_diagnostics` blobs. With `--include-gcs` (or `--use-gcs` for runs aged out past the 3-day SQL retention) it also copies in `property_reports/{cid}.md`, `raw_api/{cid}.json`, and `llm_report/{cid}.json` from `gs://{BUCKET_NAME}/runs/{date}/shard_*/`.

| Field | Value |
|---|---|
| `SCRIPT_NAME` | `failure_debug_summary` |
| `SCRIPT_ARGS` | `--run-date 2026-05-07 --include-gcs` |

Variations:
```
# Today (UTC), latest run, SQL only
SCRIPT_ARGS=

# Specific day, full bundle (SQL + GCS artifacts)
SCRIPT_ARGS=--run-date 2026-05-07 --include-gcs

# Backdated past the 3-day SQL retention — forces GCS read
SCRIPT_ARGS=--run-date 2026-05-01 --use-gcs

# Drill into specific properties (cids that succeeded are dropped automatically)
SCRIPT_ARGS=--run-date 2026-05-07 --canonical-ids 12345,67890 --include-gcs

# Pin the output dir
SCRIPT_ARGS=--run-date 2026-05-07 --out-dir /tmp/failures --include-gcs

# Tweak the per-cid scrape_events window (default 10)
SCRIPT_ARGS=--run-date 2026-05-07 --events-limit 25
```

Output (default `ma_poc/data/runs/{run_date}/failure_debug/`):
```
failure_debug/
    index.md / index.json                       # Top-level: status / tier / issue-code histograms + per-cid links
    {canonical_id}/
        summary.md / summary.json               # Triage summary (URLs scraped/filtered/explored, issue codes, latest scrape event, profile state)
        scrape_profile.json                     # Full profile payload
        scrape_events.jsonl                     # Latest events
        run_issues.jsonl                        # All issues for this cid
        property_snapshot.json                  # Full payload incl. internal debug keys
        llm_property_detail.json                # If present
        llm_diagnostics.jsonl                   # If present
        dlq_entry.json                          # If currently parked
        property_report.md / raw_api.json /     # Copied from GCS shard if --include-gcs
            llm_report.json
```

> The bundle is self-contained — no follow-up `gsutil` needed. Inspect `index.md` first for the failure cohort overview, then drill into any `{cid}/summary.md` for the per-property narrative.

---

### Migrations / schema

#### `migrate` — alembic migrations through Cloud SQL Auth Proxy
**Pre-deploy / post-deploy use only.** Runs alembic via the proxy; expects `--env staging|prod` and a subcommand.

| Field | Value |
|---|---|
| `SCRIPT_NAME` | `migrate` |
| `SCRIPT_ARGS` | `--env staging up` |

Subcommands: `up`, `down --steps N`, `up-to <revision>`, `stamp <revision>`. `migrate` shells out to a binary; double-check it can find `cloud-sql-proxy` on the image before relying on this path.

#### `gate_jugnu` — Jugnu gate validator

| Field | Value |
|---|---|
| `SCRIPT_NAME` | `gate_jugnu` |
| `SCRIPT_ARGS` | `all` |

Or: `phase 3` for a specific phase, `tests 5` to run pytest for a phase.

---

## Scripts that should NOT run via jugnu-adhoc

These exist in `ma_poc/scripts/` but require local-laptop context (browser sessions, local data dirs, interactive prompts) or are themselves wrappers around `gcloud run jobs execute`:

| Script | Why |
|---|---|
| `trigger_run`, `trigger_smoke`, `trigger_retry`, `trigger_proxy_smoke` | Wrap `gcloud run jobs execute`. Running them inside Cloud Run nests jobs — use them from a workstation. |
| `daily_runner`, `jugnu_runner`, `jugnu_retry_runner` | Production scrape orchestrators. Use the dedicated `jugnu-scrape-{env}` / `jugnu-retry-{env}` jobs, not adhoc. |
| `jugnu_shard_entry`, `jugnu_retry_entry` | Hard-wired to `CLOUD_RUN_TASK_INDEX/COUNT` and the scrape job's task pool. |
| `email_html` | Library, not a CLI. |
| `_trigger_common` | Module-private, blocked by the dispatcher. |
| `verify_csv_mapping` | Already runs as part of the docker build. Run again only if the CSV changes mid-deploy. |

---

## Recipes

### "I need to email a backdated daily report"

1. Confirm the run date is within the 3-day Postgres retention window. Anything older needs `--use-gcs`.
2. `SCRIPT_NAME=email_daily_report`, `SCRIPT_ARGS=--run-date 2026-05-05 --dry-run` first.
3. Inspect the rendered HTML in the logs.
4. Re-run without `--dry-run`.

### "Floor-plan comparison says zero matches — am I hitting the right DB?"

1. `SCRIPT_NAME=compare_floor_plans_csv`, `SCRIPT_ARGS=--csv <path> --run-id <id> -v`.
2. In the banner, confirm `DATABASE_URL` and `CLOUD_SQL_INSTANCE` point at the env you expect. The dispatcher logs both verbatim.
3. If they're wrong, redeploy with the right tfvars — env vars are baked into the job at apply time, not at execute time (overrides only work on the same job's vars).

### "A historical run wasn't synced — push it to Postgres now"

1. The data-dir must exist on the container. If it's only in GCS, write a small wrapper that downloads first; otherwise:
2. `SCRIPT_NAME=sync_run_to_pg`, `SCRIPT_ARGS=--run-date 2026-05-04 --data-dir /tmp/data`.
3. Then trigger any downstream backfill (`backfill_artifacts_pg --run-date <same>`).

### "Why did these properties fail today? Get me everything"

Builds a per-property bundle for every cid whose `run_ledger` status is not `SUCCESS`, combining Cloud SQL (events, issues, profile, snapshot, dlq, llm tables) and Cloud Storage (`property_reports/{cid}.md`, `raw_api/{cid}.json`, `llm_report/{cid}.json`).

1. Today's run, full bundle:
   ```
   SCRIPT_NAME=failure_debug_summary
   SCRIPT_ARGS=--include-gcs
   ```
2. After it finishes, browse `data/runs/{date}/failure_debug/index.md` in the run dir (uploaded back to `gs://{BUCKET_NAME}/runs/{date}/shard_<idx>/failure_debug/` by the next sync).
3. For ad-hoc inspection of specific cids: `SCRIPT_ARGS=--canonical-ids 12345,67890 --include-gcs`.
4. For runs older than 3 days (past SQL retention): `SCRIPT_ARGS=--run-date 2026-05-01 --use-gcs`.

### "Verify the live image is healthy after a deploy"

```
SCRIPT_NAME=validate_deployment
SCRIPT_ARGS=
```

then

```
SCRIPT_NAME=smoke_test
SCRIPT_ARGS=
```

Both are network-free. If either fails, the image is broken and you should roll back before scheduling scrapes.

---

## Failure modes & what to do

| Symptom | Cause | Fix |
|---|---|---|
| Job exits 1 with `error: SCRIPT_NAME is required` | Forgot to set the env var, or set it to `""`. | Set `SCRIPT_NAME` in the override. |
| `error: no such script ma_poc/scripts/<name>.py` with `Did you mean: ...` | Typo in `SCRIPT_NAME`. | Use one of the suggested names. |
| Banner shows `<unset>` for `CLOUD_SQL_INSTANCE` | Adhoc job was deployed before that env was added. | `cd infra/terraform && terraform apply -var-file=envs/{env}.tfvars`. |
| Child script prints `relation "X" does not exist` | DB hasn't been migrated to the schema this image expects. | Run `migrate --env <env> up` first. |
| Email scripts fail with Gmail MCP errors | `~/.gmail-mcp/credentials.json` not present in the image, or token expired. | Re-auth Gmail MCP locally and rebake the image. |
| Script needs a file under `data/` or `config/` and fails with `FileNotFoundError` | The container only carries what's baked into the image at build time. | Either put the file on GCS and download in a wrapper, or rebuild the image with the file copied in. |
| `OSError: [Errno 28] No space left on device` | Long-running script filled `/tmp`. | Bump `task_memory` in tfvars or aggressively clean inside the script. |
| Job times out at 7200s | Default adhoc timeout. | Re-execute with `--task-timeout=...` override on the gcloud command, or extend the var in tfvars. |
