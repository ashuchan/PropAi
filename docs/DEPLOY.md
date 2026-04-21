# Deploy Runbook

## Overview

- **Staging**: auto-deploys on every push to `main`
- **Production**: requires a signed tag `vX.Y.Z` + human approval in GitHub UI

Both environments run the same 9-step pipeline:
1. GCP auth via Workload Identity Federation
2. Ensure Cloud SQL is running (handles stop-when-idle)
3. Build and push Docker image
4. Terraform plan + apply
5. Run database migrations
6. Sync property-list CSV to GCS
7. Run smoke test (canary 3-property scrape)
8. Tag image as `{env}-known-good`

---

## Deploying to staging

Push to `main`. The `deploy-staging.yml` workflow triggers automatically.

Monitor at: **Actions → Deploy to staging**

---

## Deploying to production

1. Create a signed tag:
   ```bash
   git tag -s vX.Y.Z -m "Release notes here"
   git push origin vX.Y.Z
   ```

2. The `deploy-prod.yml` workflow triggers. It will pause at the **production** GitHub Environment and wait for approval.

3. Go to **Actions → Deploy to production → the running workflow → Review deployments**.

4. Click **Approve and deploy**.

5. Monitor the remaining steps (Terraform, migrations, smoke test).

---

## Interpreting failures

### Step: build-and-push
- SQL start timeout → SQL instance didn't become RUNNABLE in 3 minutes; check Cloud SQL console
- Docker build failed → check Dockerfile/requirements.txt changes in the PR

### Step: terraform-apply
- "Destructive changes detected on prod" → infra change requires a separate PR with review
- Plan error → check `infra/terraform/` changes

### Step: migrate
- "cloud-sql-proxy failed to become ready" → check network connectivity
- Alembic error → check `infra/sql/versions/` for migration issues; roll back manually

### Step: sync-csv
- "Duplicate canonical_id" → fix `data/property-list/properties.csv` and push again

### Step: smoke
- Exit code 5 → canary DLQ non-empty; check `gs://jugnu-raw-{env}/runs/{date}/shard_0/dlq.jsonl`
- Exit code 1 → job failed; check Cloud Logging

---

## Manual rollback

See `docs/RUNBOOK_INCIDENTS.md` for step-by-step rollback procedures.
