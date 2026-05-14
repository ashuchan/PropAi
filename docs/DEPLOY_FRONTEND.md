# DEPLOY_FRONTEND — propai-frontend on Cloud Run

**What this deploys:** A single Cloud Run service (`propai-frontend-production`) that serves both the React SPA and the Express API from one container. Public `*.run.app` URL, IAP-gated via Google login.

**Trigger:** Push to `main` with changes under `ma_poc/frontend/**`, `ma_poc/services/**`, the frontend Terraform module, or the workflow itself. Also runnable on demand via the Actions tab → "Deploy frontend (production)" → Run workflow.

**Time:** ~6–10 minutes per deploy (build + push ~4 min, terraform + IAP setup ~2 min).

**Workflow file:** [.github/workflows/deploy-frontend.yml](../.github/workflows/deploy-frontend.yml)

---

## Deploy architecture

```
  GitHub Actions runner
    │
    ├─ Step 1: build-image
    │    docker build -f ma_poc/frontend/Dockerfile . → propai-frontend:frontend-prod-<sha>
    │    Multi-stage: builds services + api deps, runs vite build, ships dist + tsx runtime.
    │    Push to us-central1-docker.pkg.dev/jugnu-494013/jugnu-images/propai-frontend
    │
    ├─ Step 2: terraform-apply
    │    Wakes Cloud SQL (plan-time refresh needs RUNNABLE state).
    │    Reads current scrape-job image tag → passes through unchanged
    │      (so a UI deploy never moves the scrape image).
    │    terraform apply → creates/updates module.cloud_run_service.
    │    Post-apply: gcloud beta run services update --iap        (enable IAP)
    │                gcloud beta iap web add-iam-policy-binding   (grant users)
    │
    └─ Step 3: smoke
         curl the service URL, expect HTTP 302 → accounts.google.com.
         (302 means service is up AND IAP is gating it.)
```

---

## Required GitHub secrets

Already configured (the scrape deploy uses them):

| Secret | Used as |
|---|---|
| `WIF_PROVIDER` | Workload Identity Federation provider resource |
| `WIF_SA_PROD` | Deployer SA email — `github-deployer@jugnu-494013.iam.gserviceaccount.com` |
| `GCP_PROJECT_ID_PROD` | `jugnu-494013` |

No new secrets needed.

---

## One-time prerequisites (do these BEFORE the first deploy)

### 1. Grant the deployer SA IAP admin

The post-apply gcloud steps in the workflow flip IAP on and add user grants. That needs `roles/iap.admin` on the deployer SA, which the scrape deploy doesn't have today:

```powershell
gcloud projects add-iam-policy-binding jugnu-494013 `
  --member="serviceAccount:github-deployer@jugnu-494013.iam.gserviceaccount.com" `
  --role="roles/iap.admin"
```

Idempotent — re-running is a no-op.

### 2. OAuth consent screen (IAP brand)

IAP needs a configured OAuth consent screen at the project level. Terraform's `google_iap_brand` is brittle (can only be created once, can't be modified afterwards), so do this in the console — 60 seconds, one-time.

1. Open <https://console.cloud.google.com/apis/credentials/consent?project=jugnu-494013>.
2. Pick **Internal** if `surgexdigital.com` is a Google Workspace org (it is — DWD is set up for it). Internal limits sign-in to your Workspace domain.
3. App name: `SurgeXDigital - RealPage Proppy`. User support email: `ashu@surgexdigital.com`. Developer contact: same.
4. Save → continue through the (empty) scopes step. No scopes needed — IAP doesn't use them.

If `Internal` isn't selectable, the project isn't part of a Workspace org. Pick `External` and add `ashu@surgexdigital.com` as a test user.

> The OAuth consent screen's "App name" is what users see on the Google sign-in page when IAP forwards them. It's display-only — it has no effect on Terraform, the workflow, or any code path. Done in the console: <https://console.cloud.google.com/apis/credentials/consent?project=jugnu-494013> (set to **"SurgeXDigital - RealPage Proppy"** on 2026-05-15).

### 3. Run from your laptop (one time, with ADC)

```powershell
gcloud auth login ashu@surgexdigital.com
gcloud auth application-default login
gcloud config set project jugnu-494013
```

ADC is only needed if you ever deploy manually outside CI — but the GitHub workflow runs via WIF and doesn't depend on your local creds.

---

## First production deploy

After the three prerequisites above:

1. Either merge a change under `ma_poc/frontend/**` or `ma_poc/services/**`, OR open the Actions tab → "Deploy frontend (production)" → Run workflow on `main`.
2. Watch the run. The three jobs (`build-image`, `terraform-apply`, `smoke`) take ~6–10 minutes total.
3. Once `smoke` succeeds, the URL is in the run summary at the bottom. It looks like `https://propai-frontend-production-<hash>-uc.a.run.app`.
4. Open it in a browser. Google login → sign in as `ashu@surgexdigital.com` → SPA loads.

If `smoke` reports HTTP 302, that's success (IAP redirecting unauthenticated requests to Google login). HTTP 200 means IAP isn't gating — check the "Enable IAP on the service" step's logs.

---

## Subsequent deploys

Just merge to `main`. The path filter triggers a redeploy whenever the frontend, services, or workflow change. Manual dispatch is also available.

Each deploy:
- Builds a new image with a fresh `frontend-prod-<git-sha>` tag (the `frontend-prod-latest` tag also moves to it).
- Terraform updates the Cloud Run service to use the new image; Cloud Run does a zero-downtime revision swap.
- IAP stays on (the `gcloud beta run services update --iap` step is idempotent).
- User grants stay in place (the `add-iam-policy-binding` step is idempotent).

---

## Adding or removing IAP users

The allowed users list lives in [.github/workflows/deploy-frontend.yml](../.github/workflows/deploy-frontend.yml) under `env.IAP_MEMBERS`:

```yaml
IAP_MEMBERS: |
  ashu@surgexdigital.com
  newteammate@surgexdigital.com
```

Commit the change. The next deploy adds the binding. Removed members are NOT cleaned up by the workflow (gcloud doesn't have a "set the policy to exactly this list" idempotent op without writing a temp policy file). To revoke explicitly, run once:

```powershell
gcloud beta iap web remove-iam-policy-binding `
  --resource-type=cloud-run `
  --service=propai-frontend-production `
  --region=us-central1 `
  --project=jugnu-494013 `
  --member="user:formerteammate@surgexdigital.com" `
  --role=roles/iap.httpsResourceAccessor
```

---

## Rollback

The last 10 Cloud Run revisions are kept automatically. To roll back to a previous one:

```powershell
gcloud run revisions list --service propai-frontend-production --region us-central1
gcloud run services update-traffic propai-frontend-production `
  --to-revisions <REVISION_NAME>=100 --region us-central1
```

Then update `frontend_image_tag` in `infra/terraform/envs/prod.tfvars` to the rolled-back image tag so the next Terraform apply doesn't move traffic back.

---

## Troubleshooting

**Workflow fails on `Enable IAP on the service` with permission denied.** The deployer SA is missing `roles/iap.admin`. Re-run prereq step 1.

**Workflow fails on `Grant IAP access to allowed users` with "OAuth consent screen not configured".** Prereq step 2 wasn't done. Configure the consent screen in console, then re-run the workflow.

**`terraform apply` fails on `google_project_service.iap`.** Most likely missing `roles/serviceusage.serviceUsageAdmin` on the deployer. The scrape deploy enables APIs so this is likely already present, but if not:
```powershell
gcloud projects add-iam-policy-binding jugnu-494013 `
  --member="serviceAccount:github-deployer@jugnu-494013.iam.gserviceaccount.com" `
  --role="roles/serviceusage.serviceUsageAdmin"
```

**Page loads but `/api/properties` returns 500.** Check Cloud Run logs:
```powershell
gcloud run services logs read propai-frontend-production --region us-central1 --limit 50
```
Most common cause: the worker SA doesn't have grants on the Postgres tables. Those grants are in `ma_poc/data_provider/sql` migrations — re-run via the migrate-stamp workflow.

**`Page not found` on a deep link like `/properties/X` after a refresh.** The SPA fallback in [api/src/server.ts](../ma_poc/frontend/api/src/server.ts) didn't fire. Check `SERVE_STATIC=true` and `STATIC_DIR=/app/ma_poc/frontend/dist` are set:
```powershell
gcloud run services describe propai-frontend-production --region us-central1 `
  --format="value(spec.template.spec.containers[0].env)"
```

**Cold start latency >5s on the first request after idle.** Expected with `min_instances=0`. To keep one instance always warm, bump `min_instances` to 1 in [modules/cloud_run_service/variables.tf](../infra/terraform/modules/cloud_run_service/variables.tf) — costs ~$15/mo.

**Build job fails on Docker `npm ci`.** Check the lockfile is committed. If you added a dep, regenerate the lockfile locally with `npm install`, commit it, push.

---

## Manual deploy from a laptop (escape hatch)

If GitHub Actions is down or you need to deploy a branch CI doesn't have access to:

```powershell
# Auth via ADC (one-time per laptop reboot)
gcloud auth application-default login
gcloud config set project jugnu-494013
gcloud auth configure-docker us-central1-docker.pkg.dev

# 1. Build + push
$SHA = (git rev-parse --short=12 HEAD)
$TAG = "frontend-prod-$SHA"
gcloud builds submit `
  --tag "us-central1-docker.pkg.dev/jugnu-494013/jugnu-images/propai-frontend`:$TAG" `
  --file ma_poc/frontend/Dockerfile `
  .

# 2. Apply Terraform — pass the current scrape tag through so we don't move it
$SCRAPE_IMG = (gcloud run jobs describe jugnu-scrape-production --region=us-central1 `
  --format='value(spec.template.template.containers[0].image)')
$SCRAPE_TAG = ($SCRAPE_IMG -split ':')[-1]
cd infra\terraform
terraform init -backend-config="bucket=jugnu-tfstate-prod" -backend-config="prefix=terraform/state"
terraform apply `
  -var-file=envs/prod.tfvars `
  -var "frontend_image_tag=$TAG" `
  -var "image_tag=$SCRAPE_TAG" `
  -var "project_id=jugnu-494013"

# 3. IAP toggle + user grants
gcloud beta run services update propai-frontend-production --iap `
  --region=us-central1 --project=jugnu-494013 --quiet
gcloud beta iap web add-iam-policy-binding `
  --resource-type=cloud-run --service=propai-frontend-production `
  --region=us-central1 --project=jugnu-494013 `
  --member="user:ashu@surgexdigital.com" `
  --role=roles/iap.httpsResourceAccessor
```

---

## What's NOT yet deployed

- **Staging:** Per the production-first decision (2026-05-15), only the production workflow exists. To enable staging, copy [.github/workflows/deploy-frontend.yml](../.github/workflows/deploy-frontend.yml) → `deploy-frontend-staging.yml`, swap `WIF_SA_PROD` → `WIF_SA_STAGING`, `GCP_PROJECT_ID_PROD` → `GCP_PROJECT_ID_STAGING`, and point at `envs/staging.tfvars`. The TODO marker is in `infra/terraform/main.tf` and `infra/terraform/envs/staging.tfvars`.
- **Provider 6.x migration:** The hybrid Terraform + gcloud IAP pattern in [modules/cloud_run_service/main.tf](../infra/terraform/modules/cloud_run_service/main.tf) is the workaround for google provider 5.x not having `iap_enabled`. When a provider 6.x bump lands (separate PR), the `gcloud beta run services update --iap` step + the gcloud user bindings can be replaced with native `iap_enabled = true` + `google_iap_web_cloud_run_service_iam_member` resources. See the header comment in the module's main.tf for the full migration note.
