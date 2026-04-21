# Operator Runbook — Jugnu Scraping Pipeline

## Prerequisites

- `gcloud` CLI installed and authenticated: `gcloud auth login`
- `gcloud config set project jugnu-{env}-<unique>`
- Python 3.11+ with `pip install -r ma_poc/requirements.txt`

---

## Trigger a manual scrape

```
python ma_poc/scripts/trigger_run.py --env {staging|prod} (--tasks N | --target-hours H) [OPTIONS]
```

**Options:**

| Flag | Description |
|------|-------------|
| `--env staging\|prod` | Required. No default. |
| `--tasks N` | Explicit task count (1–40). |
| `--target-hours H` | Pick tasks to fit wall clock target (0.5–8h). |
| `--csv GCS_URI` | Override CSV (default: `gs://jugnu-raw-{env}/property-list/properties.csv`). |
| `--limit N` | Cap properties per shard (for ad-hoc tests). |
| `--run-date YYYY-MM-DD` | Override run date (default: today UTC). |
| `--db-tier f1-micro\|g1-small\|larger` | Safety clamp tier (default: `f1-micro`). |
| `--total-props N` | Required when `--csv` is non-default. |
| `--wait / --no-wait` | Wait for completion (default: `--wait`). |
| `--dry-run` | Print plan, do not submit. |
| `--yes` | Skip confirmation prompt (for CI). |

**Examples:**

```bash
# Full prod run, target 2-hour window
python ma_poc/scripts/trigger_run.py --env prod --target-hours 2

# Explicit 10-way parallelism on staging
python ma_poc/scripts/trigger_run.py --env staging --tasks 10

# Test with 50 properties on staging, no wait
python ma_poc/scripts/trigger_run.py --env staging --tasks 2 --limit 50 --no-wait

# What would this do?
python ma_poc/scripts/trigger_run.py --env prod --target-hours 1 --dry-run
```

**Safety clamps:**

| Condition | Action |
|-----------|--------|
| `--target-hours < 0.5` | Reject (exit 4) |
| `--target-hours > 8` | Reject (exit 4) |
| `--tasks > 40` | Reject (exit 4) |
| `--tasks > 15` + `f1-micro` | Reject (exit 4) |

---

## Trigger a retry

```
python ma_poc/scripts/trigger_retry.py --env {staging|prod} --mode {errors|resume} [OPTIONS]
```

**Options:**

| Flag | Description |
|------|-------------|
| `--env staging\|prod` | Required. |
| `--mode errors\|resume` | Required. `errors` = retry failed properties; `resume` = resume interrupted run. |
| `--run-date YYYY-MM-DD` | Target run date (default: today). |
| `--limit N` | Cap retry attempts. |
| `--wait / --no-wait` | (default: `--wait`) |
| `--dry-run` | Print plan, do not submit. |
| `--yes` | Skip confirmation prompt. |

**Examples:**

```bash
# Retry failures from yesterday's prod run
python ma_poc/scripts/trigger_retry.py --env prod --mode errors --run-date 2026-04-18

# Resume an interrupted staging run
python ma_poc/scripts/trigger_retry.py --env staging --mode resume
```

---

## Run the deploy-time canary smoke test

```
python ma_poc/scripts/trigger_smoke.py --env {staging|prod} [OPTIONS]
```

**Options:**

| Flag | Default | Description |
|------|---------|-------------|
| `--timeout-seconds N` | 600 | Fail if run takes longer |
| `--min-rows N` | 3 | Fail if fewer rows written to DB |

This submits the scrape job against the canary CSV (`gs://jugnu-raw-{env}/canary/properties.csv`) with `--tasks 1` and validates:
1. Exit code 0
2. DLQ is empty
3. ≥ `--min-rows` rows written

---

## Reading structured output

All trigger scripts emit a JSON result line on stderr:

```
RESULT:{"status": "SUCCESS", "execution_name": "jugnu-scrape-prod-abc123", "tasks": 10, "env": "prod", "console_url": "https://..."}
```

Parse it in shell:

```bash
python ma_poc/scripts/trigger_run.py --env staging --tasks 3 --yes 2>&1 \
  | grep '^RESULT:' | cut -c8- | python -m json.tool
```

---

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | Job failed |
| 2 | Usage error (bad flags) |
| 3 | Precondition failed (auth, wrong project, job not found) |
| 4 | Safety clamp rejected |
| 5 | Post-condition failed (smoke only) |
| 6 | Timeout |
| 130 | SIGINT (Ctrl-C) |

---

## Common failure modes

### "gcloud not authenticated"
```bash
gcloud auth login
gcloud config set project jugnu-staging-<unique>
```

### "Cloud Run job not found"
The Terraform hasn't been applied yet:
```bash
cd infra/terraform
terraform init -backend-config="bucket=jugnu-tfstate-staging" -backend-config="prefix=terraform/state"
terraform apply -var-file=envs/staging.tfvars -var="image_tag=latest" ...
```

### "tasks > 15 on db-f1-micro"
Either lower `--tasks` or upgrade the database tier:
```bash
gcloud sql instances patch jugnu-db-staging --tier=db-g1-small
# Then re-run with --db-tier g1-small
```

### Job execution fails
Check Cloud Logging:
```bash
gcloud logging read 'resource.type="cloud_run_job" resource.labels.job_name="jugnu-scrape-staging"' \
  --limit=50 --format=json | python -m json.tool
```

Check GCS artifacts:
```bash
gsutil ls gs://jugnu-raw-staging/runs/2026-04-21/shard_0/
gsutil cat gs://jugnu-raw-staging/runs/2026-04-21/shard_0/events.jsonl | tail -20
gsutil cat gs://jugnu-raw-staging/runs/2026-04-21/shard_0/dlq.jsonl
```
