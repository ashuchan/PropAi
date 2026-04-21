# Incident Runbook

## Incident 1: Prod scrape fails repeatedly after a deploy

**Symptoms:** Daily scrapes started failing after a deploy that previously passed the smoke test.

**Steps:**

1. Confirm correlation with deploy time:
   ```bash
   gcloud logging read \
     'resource.type="cloud_run_job" resource.labels.job_name="jugnu-scrape-prod" severity>=ERROR' \
     --limit=20 --format=json | python -m json.tool | grep textPayload
   ```

2. Roll back to the last known-good image:
   ```bash
   # Find the known-good image digest
   gcloud artifacts docker tags list \
     us-central1-docker.pkg.dev/<prod-project>/jugnu-images/jugnu \
     --filter="tag:prod-known-good" --format=json

   # Update both jobs to use known-good
   gcloud run jobs update jugnu-scrape-prod \
     --image=us-central1-docker.pkg.dev/<prod-project>/jugnu-images/jugnu:prod-known-good \
     --region=us-central1

   gcloud run jobs update jugnu-retry-prod \
     --image=us-central1-docker.pkg.dev/<prod-project>/jugnu-images/jugnu:prod-known-good \
     --region=us-central1
   ```

3. Verify:
   ```bash
   python ma_poc/scripts/trigger_smoke.py --env prod
   ```

4. Once verified, open a PR with the fix. Do not re-deploy the broken version.

---

## Incident 2: Migration failure during deploy

**Symptoms:** The `migrate` step in the deploy workflow exits non-zero.

**Steps:**

1. Check what migration failed:
   ```bash
   python ma_poc/scripts/migrate.py --env staging status
   ```

2. Roll back one step:
   ```bash
   python ma_poc/scripts/migrate.py --env staging down --steps 1
   ```

3. If rollback also fails (e.g., data already written by the broken migration), restore from backup:
   ```bash
   gcloud sql backups list --instance=jugnu-db-prod --project=<prod-project>
   # Note the backup ID from the most recent successful entry
   gcloud sql backups restore <backup-id> \
     --restore-instance=jugnu-db-prod \
     --project=<prod-project>
   ```
   This loses at most ~24h of data. Acceptable for this POC.

4. Fix the migration file. Open a PR. Do NOT edit the applied migration — add a new one.

---

## Incident 3: Cloud SQL stop-when-idle collides with a deploy

**Symptoms:** Deploy runs during the 3am–6am window when the scheduler has stopped the database. Migration step fails with "connection refused".

**Steps:**

The deploy workflow already handles this automatically (Step 2: "Ensure Cloud SQL is running"). If you're running a manual deploy, start the instance first:

```bash
gcloud sql instances patch jugnu-db-staging \
  --activation-policy=ALWAYS \
  --project=jugnu-staging-<unique>

# Wait for RUNNABLE state
watch -n 5 "gcloud sql instances describe jugnu-db-staging \
  --project=jugnu-staging-<unique> --format='value(state)'"

# Then proceed with manual steps
python ma_poc/scripts/migrate.py --env staging up
```

---

## Disabling the scheduled scrape in an emergency

```bash
gcloud scheduler jobs pause jugnu-daily-scrape-prod \
  --location=us-central1 --project=<prod-project>

# Re-enable when ready
gcloud scheduler jobs resume jugnu-daily-scrape-prod \
  --location=us-central1 --project=<prod-project>
```
