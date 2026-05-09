# Cloud Run jugnu-adhoc — Operator Manual

On-demand runner for any script in [`ma_poc/scripts/`](../ma_poc/scripts/). Reuses the `jugnu` image, worker SA, VPC connector, Cloud SQL Connector wiring, GCS bucket, and provider/proxy secrets — so any script gets Cloud SQL + GCS access for free.

Job name: **`jugnu-adhoc-{env}`** (e.g. `jugnu-adhoc-staging`, `jugnu-adhoc-prod`).

Dispatcher source: [`ma_poc/scripts/runners/dispatcher.py`](../ma_poc/scripts/runners/dispatcher.py).

> ⚠️ **Pre-requisite: dispatcher must accept submodule paths.** The Phase 2/Phase 3 reorg moved most scripts into subdirectories (`backfills/`, `email/`, `checks/`, etc.). All `SCRIPT_NAME` values in this doc are **dotted module paths** — e.g. `backfills.artifacts`, not the old flat `backfill_artifacts_pg`. The dispatcher needs a small companion patch (see [Dispatcher: required code fix](#dispatcher-required-code-fix) at the bottom) to translate the dotted name into the corresponding `ma_poc/scripts/<dir>/<name>.py` lookup. Until that patch lands, every `SCRIPT_NAME` below will fail validation with `error: no such script …`.

---

## How to run from the Cloud Run console

1. Console → **Cloud Run** → **Jobs** tab → click `jugnu-adhoc-{env}`.
2. Click **EXECUTE WITH OVERRIDES** (button at the top, not the plain "EXECUTE").
3. Open **Container, variables & secrets** → **Variables & Secrets** → edit:
   - `SCRIPT_NAME` — dotted module path (e.g. `checks.outputs`). **No `.py`, no leading slash.**
   - `SCRIPT_ARGS` — shell-quoted CLI args (e.g. `--csv config/properties.csv --limit 5`). Optional. Parsed via `shlex` so `'embedded spaces'` and `--key="quoted=value"` work.
4. Click **EXECUTE**.
5. After it starts: click the execution row → **LOGS** tab to watch live output.

The dispatcher prints a banner showing python version, hostname, every `CLOUD_RUN_*` injection, all wiring envs (DB, GCS, schema, provider), and a masked list of every `*_KEY`/`*_TOKEN`/`*_PASSWORD`/`*_SECRET` env var (last 4 chars only). At exit it prints elapsed wall time and the child exit code.

### gcloud CLI equivalent

```bash
gcloud run jobs execute jugnu-adhoc-{env} \
  --region=us-central1 \
  --update-env-vars="SCRIPT_NAME=checks.outputs,SCRIPT_ARGS=--csv config/properties.csv --limit 5" \
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

#### `backfills.artifacts` — copy run artifact files into Postgres
Tables: `property_reports`, `llm_reports`, `llm_property_details`, `llm_diagnostics`. Idempotent (upsert).

| Field | Value |
|---|---|
| `SCRIPT_NAME` | `backfills.artifacts` |
| `SCRIPT_ARGS` | `--run-date 2026-05-07` |

Optional flags: `--data-root data/v2`, `--url <DATABASE_URL override>`, `--dry-run`.

```
SCRIPT_ARGS=--run-date 2026-05-07 --dry-run
```

#### `backfills.floor_plan_id` — populate `units.floor_plan_id` for legacy rows
Idempotent: only fills NULLs. Uses the same `compute_floor_plan_id` helper as the writer.

| Field | Value |
|---|---|
| `SCRIPT_NAME` | `backfills.floor_plan_id` |
| `SCRIPT_ARGS` | *(empty)* |

Optional: `--batch-size 1000`, `--database-url <override>`, `--dry-run`, `-v`.

#### `backfills.postgres` — copy FS data into the SQL provider
Reads `DATA_DIR` + `CONFIG_DIR` via `FileSystemDataProvider` and writes through PostgresDataProvider. **Note: requires FS data on the container — only useful if you've also mounted/uploaded the source.** For per-run sync use `sync.run_to_pg` instead (see below).

| Field | Value |
|---|---|
| `SCRIPT_NAME` | `backfills.postgres` |
| `SCRIPT_ARGS` | `--target postgres` |

Sections (`--only`): `state,profiles,events,runs,extractions`. Date filter: `--run-dates 2026-04-14,2026-04-19`. Add `--dry-run` to preview.

#### `backfills.units_bed_bath` — fill `units.beds` / `units.baths` from `floor_plan_name`
Idempotent: never overwrites non-NULL values.

| Field | Value |
|---|---|
| `SCRIPT_NAME` | `backfills.units_bed_bath` |
| `SCRIPT_ARGS` | *(empty)* |

Optional: `--batch-size 500`, `--database-url <override>`, `--dry-run`, `-v`.

---

### Floor-plan comparison & disagreement export

#### `floor_plans.compare` — match a floor-plan CSV against the DB
Writes `floor_plan_comparison_runs` + `floor_plan_comparison_rows`. Re-running with the same `--run-id` replaces results for that run.

| Field | Value |
|---|---|
| `SCRIPT_NAME` | `floor_plans.compare` |
| `SCRIPT_ARGS` | `--csv ma_poc/config/Floorplan-comparisons.csv --run-id 2026-05-07-prod` |

Tuning: `--threshold 85` (default name match score), `--buffer 35` (sqft buffer in sqft), `--database-url`, `-v`.

> The CSV must already be present on the container image. To compare against a fresh CSV, upload it to GCS and either bake it into the image or load via a small adapter script — `gs://` paths are not directly supported.

#### `floor_plans.export_disagreements` — write review-queue rows for genuine CSV-vs-DB disagreements
Reads from a previous `floor_plans.compare` run. Idempotent: re-running clears `status='pending'` rows for the run-id; reviewer-touched rows are preserved.

| Field | Value |
|---|---|
| `SCRIPT_NAME` | `floor_plans.export_disagreements` |
| `SCRIPT_ARGS` | `--run-id 2026-05-07-prod` |

Optional: `--min-score 95` (default 90, tighter than the matcher's 85), `--to-csv /tmp/disagreements.csv`, `--database-url`, `-v`.

---

### Email reports

> Email reports route through one of two transports, selected by the **`EMAIL_TRANSPORT`** env var on the adhoc job (read by [`scripts/email/daily.py`](../ma_poc/scripts/email/daily.py)::`_email_transport`):
>
> - **`gmail_api`** *(default on Cloud Run)* — Workspace domain-wide delegation. The worker SA impersonates `GMAIL_EMAILER_SA` via `iamcredentials.signJwt`, then sends as `GMAIL_DELEGATED_USER`. Both env vars are wired by Terraform (`infra/terraform/envs/{env}.tfvars`); the IAM `serviceAccountTokenCreator` binding is created in the same module. No Node, no `~/.gmail-mcp/credentials.json`, no key files. To verify the chain end-to-end before deploying, run [`scripts/diagnostics/dwd_smoke.py`](../ma_poc/scripts/diagnostics/dwd_smoke.py) locally — it uses the same `_build_gmail_api_credentials` path.
> - **`mcp`** — legacy `@gongrzhe/server-gmail-autoauth-mcp` over stdio. Requires Node + `~/.gmail-mcp/credentials.json` baked into the image; **not** present in the prod image. Useful from a workstation that already has MCP creds set up. Toggle by setting `EMAIL_TRANSPORT=mcp` either in tfvars or as a one-shot override on the execute command (`--update-env-vars="...,EMAIL_TRANSPORT=mcp"`).
>
> Use `--dry-run` first to render the HTML to stdout (visible in Cloud Logging) without actually sending.

#### `email.daily` — daily PropAi scrape report (headline dashboard)

| Field | Value |
|---|---|
| `SCRIPT_NAME` | `email.daily` |
| `SCRIPT_ARGS` | *(empty for today UTC, latest run)* |

Common variations:
```
SCRIPT_ARGS=--run-date 2026-05-07
SCRIPT_ARGS=--recipients alice@example.com,bob@example.com
SCRIPT_ARGS=--dry-run
SCRIPT_ARGS=--database-url postgresql+pg8000://user:pass@host:5432/proppy
```

#### `email.daily_failures` — operational drill-down with two XLSX attachments
Ships `scraped_units_{run_date}.xlsx` and `failed_properties_{run_date}.xlsx`. Falls back to GCS `gs://{BUCKET_NAME}/runs/{date}/shard_*/` when SQL has no rows for the date (older than 3-day retention).

| Field | Value |
|---|---|
| `SCRIPT_NAME` | `email.daily_failures` |
| `SCRIPT_ARGS` | *(empty)* |

Variations:
```
SCRIPT_ARGS=--run-date 2026-05-07
SCRIPT_ARGS=--use-gcs --run-date 2026-05-01
SCRIPT_ARGS=--out-dir /tmp/reports --dry-run
SCRIPT_ARGS=--recipients ops@example.com
```

#### `email.merge_analysis` — unit-merge-failure analysis for a run

| Field | Value |
|---|---|
| `SCRIPT_NAME` | `email.merge_analysis` |
| `SCRIPT_ARGS` | *(empty for latest)* |

```
SCRIPT_ARGS=--run-date 2026-05-05
SCRIPT_ARGS=--dry-run
SCRIPT_ARGS=--recipients alice@example.com
```

#### `email.refactor_plan` — one-shot internal email

| Field | Value |
|---|---|
| `SCRIPT_NAME` | `email.refactor_plan` |
| `SCRIPT_ARGS` | `--dry-run` |

---

### Sync / data movement

#### `sync.run_to_pg` — copy one run's FS output into Postgres
This is what every Jugnu shard calls at the end of its execution. Useful as a one-off when a previous shard's sync failed.

| Field | Value |
|---|---|
| `SCRIPT_NAME` | `sync.run_to_pg` |
| `SCRIPT_ARGS` | `--run-date 2026-05-07 --data-dir /tmp/data` |

Optional: `--config-dir <path>`, `--url <DATABASE_URL override>`, `--shard-id <id>`.

> The data-dir must be locally accessible on the container. If the run lives only in GCS, download it first inside a wrapper script.

#### `sync.cloud_to_local` — pull Cloud SQL into a local DB
Mostly intended for laptop use. Runs fine on Cloud Run if you point `--local-url` at a real target; otherwise leave it for local dev only.

| Field | Value |
|---|---|
| `SCRIPT_NAME` | `sync.cloud_to_local` |
| `SCRIPT_ARGS` | `--mode upsert --auth-type IAM --dry-run` |

Other useful flags: `--tables properties,units` (whitelist), `--mode mirror` (delete-and-replace), `--batch-size 1000`.

---

### Smoke tests / sanity checks

#### `smoke.imports` — Jugnu import-sanity (offline, no network)
Verifies all five layer contracts import cleanly. Exits 0 if all 5 pass. **Takes no arguments.**

| Field | Value |
|---|---|
| `SCRIPT_NAME` | `smoke.imports` |
| `SCRIPT_ARGS` | *(empty)* |

#### `smoke.rentcafe_direct` — production smoke for the RentCafe direct path
50-property network smoke. **Calls real network endpoints.** Designed to run from production-equivalent egress (i.e. Cloud Run, not laptop) for the IP-reputation comparison to be meaningful.

| Field | Value |
|---|---|
| `SCRIPT_NAME` | `smoke.rentcafe_direct` |
| `SCRIPT_ARGS` | `--blocked-input ma_poc/data/runs/2026-05-04/bot_blocked_properties_latest.json` |

Optional: `--csv path/to/properties.csv`, `--output /tmp/smoke.json`, `--sample-size 50`.

#### `checks.deployment` — image-time sanity
Same script that runs at `docker build` time. Verifies the floor-plan mapping, FloorplanCatalog, prompt templates, and module imports. Use it to sanity-check the deployed image after a rollout. **Takes no arguments.**

| Field | Value |
|---|---|
| `SCRIPT_NAME` | `checks.deployment` |
| `SCRIPT_ARGS` | *(empty)* |

#### `checks.outputs` — post-run metric validation
Reads `data/scrape_events.jsonl` and computes the BRD weekly-gate metrics (success rate, tier distribution, P95 page load, etc.).

| Field | Value |
|---|---|
| `SCRIPT_NAME` | `checks.outputs` |
| `SCRIPT_ARGS` | *(empty)* |

> Like `sync.run_to_pg`, this expects local FS data. If the run is GCS-only, download it inside a wrapper.

---

### Reports & analysis

> **All five report scripts now email their output at the end of the run.** They share a single utility — [`scripts/email/report.py`](../ma_poc/scripts/email/report.py)::`email_report` — which renders a professional HTML shell (header, summary callout, body, Cloud Run footer) and ships through the same `EMAIL_TRANSPORT` dispatcher used by `scripts/email/*`. Recipients fall back to `REPORT_RECIPIENTS` (set in tfvars). Every report supports three flags for its email behaviour:
>
> - `--no-email` — skip the send. Markdown / HTML / JSON artifacts are still written.
> - `--email-recipients alice@x.com,bob@y.com` — override `REPORT_RECIPIENTS` for this invocation.
> - `--dry-run` — render the email body to stdout (visible in Cloud Logging) but do not actually send. Use this first when changing recipients or sending a backdated report.
>
> Each report has its own subject line — the recipient can filter on them. Subjects:
> - `reports.daily` → `PropAi Daily Report — {run_date}`
> - `reports.analysis` → `PropAi 5-Day Analytics — {today}`
> - `reports.health` → `PropAi Health Report — {run_date}` (and a separate `PropAi Extraction Quality Report — {run_date}` when `--report=all`)
> - `reports.escalation` → `PropAi Fetch-Tier Escalation — {run_dir}`
> - `reports.floor_plan_comparison` → `Floor Plan Comparison — {run_id}`
>
> Markdown reports (`health`, `floor_plan_comparison`) are converted to HTML by an in-house lightweight converter (headings, pipe tables, lists, fenced code, inline bold/italic/code/links — no extra dependency). Stdout-style reports (`analysis`, `escalation`) are wrapped in a monospace `<pre>` so column alignment survives. The full HTML report from `daily` is attached as a file (Gmail clips inline HTML at ~100KB). All reports include the on-disk artifact (markdown / HTML / JSON sidecar) as an attachment when one was written.

#### `reports.health` — health / extraction-quality report

| Field | Value |
|---|---|
| `SCRIPT_NAME` | `reports.health` |
| `SCRIPT_ARGS` | `--report health --source db` |

Variations:
```
SCRIPT_ARGS=--report all --source gcs --bucket <bucket>
SCRIPT_ARGS=--report extraction-quality --run-date 2026-05-07
SCRIPT_ARGS=--report health --days 7 --json --out -
SCRIPT_ARGS=--report health --no-email                            # write artifact, no mail
SCRIPT_ARGS=--report all --dry-run                                # preview both emails
```

`--no-scan-events` skips the events.jsonl scan when sourcing from GCS (faster, drops wall-clock runtime metrics). `--report=all` sends two separate emails — one health, one extraction-quality — so subject filters stay clean.

#### `reports.daily` — daily HTML dashboard

| Field | Value |
|---|---|
| `SCRIPT_NAME` | `reports.daily` |
| `SCRIPT_ARGS` | `--run-date 2026-05-07` |

Optional: `--out /tmp/report.html`, `--database-url`, `--trend-window 14`. The full HTML report is attached as a file because it usually exceeds Gmail's 100KB inline-clip threshold; the email body itself is a compact text summary with the headline KPIs.

#### `reports.analysis` — 5-day analytics summary

| Field | Value |
|---|---|
| `SCRIPT_NAME` | `reports.analysis` |
| `SCRIPT_ARGS` | *(empty)* |

Writes `data/runs/{today}/analysis_report.json` and emails the full text summary (success trends, units quality, daily diffs, system performance) with the JSON attached.

#### `reports.escalation` — fetch-tier escalation report

| Field | Value |
|---|---|
| `SCRIPT_NAME` | `reports.escalation` |
| `SCRIPT_ARGS` | *(empty for latest run)* |

Optional: `--run-dir /tmp/data/runs/2026-05-07`.

#### `reports.floor_plan_comparison` — markdown report from a `compare_floor_plans_csv` run

| Field | Value |
|---|---|
| `SCRIPT_NAME` | `reports.floor_plan_comparison` |
| `SCRIPT_ARGS` | `--run-id 2026-05-07-prod` |

Optional: `--top 50`, `--examples-per-method 5`, `--out /tmp/report.md`. Emails the markdown body (rendered to HTML via the in-house converter) with the `.md` file attached. If no `--run-id` is passed, picks the most recent comparison run from the DB.

#### `baselines.escalation` — generate ESCALATION_BASELINE.md from latest run

| Field | Value |
|---|---|
| `SCRIPT_NAME` | `baselines.escalation` |
| `SCRIPT_ARGS` | *(empty)* |

Optional: `--data-dir /tmp/data`, `--out /tmp/ESCALATION_BASELINE.md`.

#### `diagnostics.cluster_retry` — analyze / retry PMC-portal failures

| Field | Value |
|---|---|
| `SCRIPT_NAME` | `diagnostics.cluster_retry` |
| `SCRIPT_ARGS` | `--analyze ma_poc/data/runs/2026-05-07/properties.json` |

Or:
```
SCRIPT_ARGS=--retry example.com --run-date 2026-05-07
```

#### `failure_debug_summary` — per-property failure debug bundles
Still at the top level (`ma_poc/scripts/failure_debug_summary.py`) — no submodule prefix. For every property whose `run_ledger` status is not `SUCCESS` on the given day, builds a self-contained triage bundle combining Cloud SQL state and Cloud Storage artifacts. Each bundle includes the latest `scrape_events`, all `run_issues`, the full `scrape_profile` (maturity, blocked_endpoints, llm_field_mappings), the `property_snapshot.payload` with `_meta` / `_extract_result` / `_explored_links` / `_raw_api_responses` / `_filtered_apis` / `_winning_page_url` / `_llm_interactions`, the `dlq_entries` row if parked, and `llm_property_details` / `llm_diagnostics` blobs. With `--include-gcs` (or `--use-gcs` for runs aged out past the 3-day SQL retention) it also copies in `property_reports/{cid}.md`, `raw_api/{cid}.json`, and `llm_report/{cid}.json` from `gs://{BUCKET_NAME}/runs/{date}/shard_*/`.

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

#### `migrations.alembic` — alembic migrations through Cloud SQL Auth Proxy
**Pre-deploy / post-deploy use only.** Runs alembic via the proxy; expects `--env staging|prod` and a subcommand.

| Field | Value |
|---|---|
| `SCRIPT_NAME` | `migrations.alembic` |
| `SCRIPT_ARGS` | `--env staging up` |

Subcommands: `up`, `down --steps N`, `up-to <revision>`, `stamp <revision>`. The script shells out to a binary; double-check it can find `cloud-sql-proxy` on the image before relying on this path.

#### `gates.jugnu` — Jugnu gate validator

| Field | Value |
|---|---|
| `SCRIPT_NAME` | `gates.jugnu` |
| `SCRIPT_ARGS` | `all` |

Or: `phase 3` for a specific phase, `tests 5` to run pytest for a phase.

---

## Scripts that should NOT run via jugnu-adhoc

These exist in `ma_poc/scripts/` but require local-laptop context (browser sessions, local data dirs, interactive prompts) or are themselves wrappers around `gcloud run jobs execute`:

| Module path | Why |
|---|---|
| `triggers.run`, `triggers.smoke`, `triggers.retry`, `triggers.proxy_smoke` | Wrap `gcloud run jobs execute`. Running them inside Cloud Run nests jobs — use them from a workstation. |
| `runners.jugnu`, `runners.jugnu_retry` | Production scrape orchestrators. Use the dedicated `jugnu-scrape-{env}` / `jugnu-retry-{env}` jobs, not adhoc. |
| `runners.shard_entry`, `runners.retry_entry` | Hard-wired to `CLOUD_RUN_TASK_INDEX/COUNT` and the scrape job's task pool. |
| `runners.jugnu_retry_merge` | Invoked by the retry job after all shards complete; not standalone. |
| `email._client` | Library, not a CLI. |
| `_common.trigger` | Module-private, blocked by the dispatcher. |
| `checks.csv_mapping` | Already runs as part of the docker build. Run again only if the CSV changes mid-deploy. |

---

## Recipes

### "I need to email a backdated daily report"

1. Confirm the run date is within the 3-day Postgres retention window. Anything older needs `--use-gcs`.
2. `SCRIPT_NAME=email.daily`, `SCRIPT_ARGS=--run-date 2026-05-05 --dry-run` first.
3. Inspect the rendered HTML in the logs.
4. Re-run without `--dry-run`.

### "Floor-plan comparison says zero matches — am I hitting the right DB?"

1. `SCRIPT_NAME=floor_plans.compare`, `SCRIPT_ARGS=--csv <path> --run-id <id> -v`.
2. In the banner, confirm `DATABASE_URL` and `CLOUD_SQL_INSTANCE` point at the env you expect. The dispatcher logs both verbatim.
3. If they're wrong, redeploy with the right tfvars — env vars are baked into the job at apply time, not at execute time (overrides only work on the same job's vars).

### "A historical run wasn't synced — push it to Postgres now"

1. The data-dir must exist on the container. If it's only in GCS, write a small wrapper that downloads first; otherwise:
2. `SCRIPT_NAME=sync.run_to_pg`, `SCRIPT_ARGS=--run-date 2026-05-04 --data-dir /tmp/data`.
3. Then trigger any downstream backfill (`backfills.artifacts --run-date <same>`).

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
SCRIPT_NAME=checks.deployment
SCRIPT_ARGS=
```

then

```
SCRIPT_NAME=smoke.imports
SCRIPT_ARGS=
```

Both are network-free. If either fails, the image is broken and you should roll back before scheduling scrapes.

---

## Failure modes & what to do

| Symptom | Cause | Fix |
|---|---|---|
| Job exits 1 with `error: SCRIPT_NAME is required` | Forgot to set the env var, or set it to `""`. | Set `SCRIPT_NAME` in the override. |
| `error: no such script ma_poc/scripts/<name>.py` with `Did you mean: ...` | Either a typo, or the dispatcher hasn't been patched to handle dotted submodule paths (see [Dispatcher: required code fix](#dispatcher-required-code-fix)). | Confirm patch is deployed; if so, use one of the suggested names. |
| Banner shows `<unset>` for `CLOUD_SQL_INSTANCE` | Adhoc job was deployed before that env was added. | `cd infra/terraform && terraform apply -var-file=envs/{env}.tfvars`. |
| Child script prints `relation "X" does not exist` | DB hasn't been migrated to the schema this image expects. | Run `migrations.alembic --env <env> up` first. |
| `EMAIL_TRANSPORT=gmail_api` send fails with `Permission 'iam.serviceAccounts.signJwt' denied` | The worker SA doesn't have `serviceAccountTokenCreator` on the emailer SA — usually means the adhoc job was deployed before `gmail_emailer_sa_email` was set in tfvars (the IAM binding is only created when that var is non-empty). | Set the var in `infra/terraform/envs/{env}.tfvars` and `terraform apply`. |
| `EMAIL_TRANSPORT=gmail_api` send fails with `unauthorized_client` from `oauth2.googleapis.com/token` | Workspace DWD entry is missing or has the wrong scope. | admin.google.com → Security → API controls → Domain-wide delegation. Client ID = the **numeric** `oauth2ClientId` of the emailer SA (not its email). Scope: `https://www.googleapis.com/auth/gmail.send`. |
| `EMAIL_TRANSPORT=gmail_api` send fails with `invalid_grant: Invalid email or User ID` | `GMAIL_DELEGATED_USER` is not a real Workspace user in the authorised domain. | Use a real mailbox in the Workspace whose admin authorised the SA. |
| `EMAIL_TRANSPORT=mcp` requested but the script crashes at `npx` spawn | Node is not installed in the prod image. | Switch back to `gmail_api` (the default), or run from a workstation that has Node + `~/.gmail-mcp/credentials.json`. |
| Script needs a file under `data/` or `config/` and fails with `FileNotFoundError` | The container only carries what's baked into the image at build time. | Either put the file on GCS and download in a wrapper, or rebuild the image with the file copied in. |
| `OSError: [Errno 28] No space left on device` | Long-running script filled `/tmp`. | Bump `task_memory` in tfvars or aggressively clean inside the script. |
| Job times out at 7200s | Default adhoc timeout. | Re-execute with `--task-timeout=...` override on the gcloud command, or extend the var in tfvars. |

---

## Dispatcher: required code fix

The Phase 2 reorg moved `run_script.py` to [`runners/dispatcher.py`](../ma_poc/scripts/runners/dispatcher.py) but left two of its constants unchanged:

```python
_SCRIPTS_DIR = Path(__file__).resolve().parent       # now resolves to runners/, not scripts/
_PACKAGE     = "ma_poc.scripts"                      # still claims top-level scripts/
```

The disk-existence check (line 182) looks for `_SCRIPTS_DIR / f"{name}.py"` — i.e. only files directly inside `runners/`. The module spawn (line 218) executes `python -m ma_poc.scripts.<name>` — i.e. the top-level `scripts/` package. The two now disagree, so:

- A `SCRIPT_NAME` that exists at the top level (e.g. `failure_debug_summary`) fails validation: "no such script ma_poc/scripts/runners/…"
- A `SCRIPT_NAME` that exists in `runners/` would pass validation but then fail to spawn: `python -m ma_poc.scripts.shard_entry` is not the right path.
- A dotted submodule `SCRIPT_NAME` (e.g. `backfills.artifacts`) fails the existence check because the literal file `backfills.artifacts.py` does not exist.

The minimal fix:

1. `_SCRIPTS_DIR = Path(__file__).resolve().parent.parent` — point back at `ma_poc/scripts/`.
2. In `_validate_script`, translate the dotted name to a path: `target = _SCRIPTS_DIR.joinpath(*name.split("."))` with `.py` appended.
3. Walk subdirectories for the "Did you mean" suggestion (`_SCRIPTS_DIR.rglob("*.py")` instead of `glob("*.py")`), and emit candidates as their dotted form (`p.relative_to(_SCRIPTS_DIR).with_suffix("").as_posix().replace("/", ".")`).
4. Update `_USAGE` to mention the dotted form.

After the fix, every `SCRIPT_NAME` in this doc resolves correctly. No TF redeploy is required because the dispatcher is part of the image; rebuilding & redeploying the `jugnu` container picks up the change.
