# Jugnu GCP Infrastructure — Terraform

## One-time bootstrap (human, per environment)

Before running `terraform apply` for the first time, a human with GCP org-level access must complete these steps for each environment:

1. Create the GCP project; link billing
2. Enable APIs:
   ```bash
   gcloud services enable \
     run.googleapis.com sqladmin.googleapis.com artifactregistry.googleapis.com \
     secretmanager.googleapis.com cloudscheduler.googleapis.com \
     vpcaccess.googleapis.com servicenetworking.googleapis.com \
     compute.googleapis.com iamcredentials.googleapis.com \
     --project=jugnu-{env}-<unique>
   ```
3. Create VPC with private services access for Cloud SQL peering
4. Create tfstate bucket:
   ```bash
   gsutil mb -l us-central1 -b on gs://jugnu-tfstate-{env}
   gsutil versioning set on gs://jugnu-tfstate-{env}
   ```
5. Configure Workload Identity Federation; bind to GitHub repo
6. Create `github-deployer-{env}` SA; grant `roles/owner`; bind WIF principal
7. Add GitHub repo secrets: `GCP_PROJECT_ID_STAGING`, `GCP_PROJECT_ID_PROD`, `WIF_PROVIDER`, `WIF_SA_STAGING`, `WIF_SA_PROD`

## Applying infrastructure

### Staging

```bash
cd infra/terraform
terraform init \
  -backend-config="bucket=jugnu-tfstate-staging" \
  -backend-config="prefix=terraform/state"

terraform apply \
  -var-file=envs/staging.tfvars \
  -var="image_tag=latest" \
  -var="project_id=jugnu-staging-<unique>" \
  -var="vpc_self_link=projects/jugnu-staging-<unique>/global/networks/default" \
  -var="deployer_sa_email=github-deployer-staging@jugnu-staging-<unique>.iam.gserviceaccount.com"
```

### Production

Same as staging but with `envs/prod.tfvars` and the prod project.

## Writing secret values (after first apply)

Terraform creates empty secret *slots*; values are written manually so they
never land in tfstate. Replace `{env}` with `staging` or `production`.

```bash
# Anthropic (default provider — set this at minimum)
echo -n "$ANTHROPIC_KEY" | gcloud secrets versions add anthropic-api-key-{env} \
  --data-file=- --project={project_id}

# OpenRouter (only needed if you switch llm_provider to "openrouter")
echo -n "$OPENROUTER_KEY" | gcloud secrets versions add openrouter-api-key-{env} \
  --data-file=- --project={project_id}

# Proxy credentials (always required)
echo -n "$PROXY_CREDS" | gcloud secrets versions add proxy-credentials-{env} \
  --data-file=- --project={project_id}
```

Verify the latest version landed:
```bash
gcloud secrets versions list anthropic-api-key-{env} --project={project_id}
```

## Switching LLM provider

The default is **Anthropic Claude Haiku 4.5** (cheapest current Anthropic
model, supports vision). Both Anthropic and OpenRouter API keys are mounted
into every Cloud Run job; `LLM_PROVIDER` selects which one
`ma_poc/llm/factory.py` actually instantiates.

Override per environment in `envs/{env}.tfvars`:

```hcl
# Keep default (Anthropic Haiku 4.5) — nothing to set.

# Switch to a more capable Anthropic model for vision only
anthropic_vision_model = "claude-sonnet-4-5-20250929"

# Switch provider entirely
llm_provider            = "openrouter"
openrouter_model        = "qwen/qwen3-235b-a22b-2507"
openrouter_vision_model = "qwen/qwen3-235b-a22b-2507"
```

Then `terraform apply` — Cloud Run jobs pick up the new env vars on the
next execution. If you switch providers, make sure the corresponding
secret has a version written (see block above).

## State recovery

If the tfstate bucket is lost (the one unrecoverable failure — bucket has versioning enabled):
1. Get the versioned state from the bucket before it was lost (if possible)
2. `terraform import` each resource back into state
3. Verify with `terraform plan` → should show zero diff

## Emergency: disable the scheduled scrape

```bash
gcloud scheduler jobs pause jugnu-daily-scrape-{env} \
  --location=us-central1 --project={project_id}
```

## Module structure

| Module | Resources |
|--------|-----------|
| `artifact_registry` | Docker image repository |
| `iam` | 3 service accounts + project IAM bindings |
| `storage` | `jugnu-raw-{env}` bucket with lifecycle rules |
| `cloud_sql` | Postgres 15 instance + IAM users + VPC connector |
| `secrets` | Secret Manager slots (values written manually) |
| `cloud_run_jobs` | scrape job + retry job |
| `scheduler` | 4 Cloud Scheduler jobs (SQL start/stop + daily scrape + daily retry) |
