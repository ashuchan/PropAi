# CLAUDE_TERRAFORM.md

**Goal:** Provision all GCP infrastructure for the Jugnu scraping pipeline as declarative Terraform, deployable to staging and production with one command per environment. After this handoff lands, manual `gcloud` clicks to create or modify infrastructure are **forbidden** — every change goes through a PR against `infra/terraform/`.

**Read before starting:**
- `Jugnu_Deployment_Architecture_GCP.docx` — the architecture spec this implements
- `scripts/jugnu_runner.py` — understand the CLI surface (`--csv`, `--limit`, `--run-date`, `--schema-version`, `--proxy`)
- `scripts/jugnu_retry_runner.py` — understand the retry CLI surface (`--retry-errors`, `--resume`, `--run-date`)
- `README.md` — the two-pipeline layout (Jugnu + legacy) and state file contract

**Do not start implementation until `CLAUDE_DOCKERFILE.md` is merged and an image exists in Artifact Registry.** Terraform references an image; without one, `terraform apply` fails at the Cloud Run job creation step.

---

## 1. Scope

What this handoff produces:

- `infra/terraform/` — root module with per-environment workspaces
- Six GCP resource groups: Artifact Registry, Cloud Run (two jobs), Cloud SQL, Cloud Storage, Cloud Scheduler, IAM + Secret Manager
- Two environments: `staging` and `prod`, isolated by project ID and by tfstate bucket
- A Makefile target and a GitHub Actions job that runs `terraform plan` on every PR touching `infra/`

What this handoff does **not** produce:
- The Docker image (that's `CLAUDE_DOCKERFILE.md`)
- The GitHub Actions deploy workflows (that's `CLAUDE_DEPLOY.md`)
- Alembic migrations (that's `CLAUDE_MIGRATIONS.md`)
- Workload Identity Federation bootstrap (human does this once, before you run — see §8)

---

## 2. Directory layout to create

```
infra/
├── terraform/
│   ├── README.md                    # ops runbook — apply order, state recovery, etc.
│   ├── backend.tf                   # GCS backend config (per-env via -backend-config)
│   ├── providers.tf                 # google + google-beta provider pins
│   ├── variables.tf                 # all tunables (task_count, csv_path, proxy_host, ...)
│   ├── main.tf                      # wires modules together
│   ├── outputs.tf                   # exports job names, SA emails, bucket names
│   ├── envs/
│   │   ├── staging.tfvars
│   │   └── prod.tfvars
│   └── modules/
│       ├── artifact_registry/
│       ├── iam/
│       ├── storage/
│       ├── cloud_sql/
│       ├── secrets/
│       ├── cloud_run_jobs/
│       └── scheduler/
└── sql/
    └── migrations/                  # placeholder — populated by CLAUDE_MIGRATIONS.md
```

Each module is self-contained: `main.tf`, `variables.tf`, `outputs.tf`. No module imports another — wiring happens in the root `main.tf`.

---

## 3. Required providers and pins

```hcl
# infra/terraform/providers.tf
terraform {
  required_version = ">= 1.7.0, < 2.0.0"
  required_providers {
    google      = { source = "hashicorp/google",      version = "~> 5.40" }
    google-beta = { source = "hashicorp/google-beta", version = "~> 5.40" }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

provider "google-beta" {
  project = var.project_id
  region  = var.region
}
```

**Pin majors, allow patches.** The `~> 5.40` pattern allows 5.40.x updates but blocks 6.x. This matches the discipline in `claude_refactor.md` around static analysis — we want reproducibility, not automatic upgrades.

---

## 4. Module specifications

Each subsection below is a direct handoff for one module. Treat them as independent PRs if the full set is too large for one review.

### 4.1 Module: `artifact_registry`

**Purpose:** Docker image repository for the scraper image.

**Resources:**
- `google_artifact_registry_repository` named `jugnu-images`, format `DOCKER`, region matches `var.region`
- One IAM binding: the deployer SA (name passed in as input) gets `roles/artifactregistry.writer`

**Outputs:**
- `repository_url` → `{region}-docker.pkg.dev/{project_id}/jugnu-images` — consumed by Cloud Run job module and by the deploy workflow

**Why not Container Registry (gcr.io):** Google is sunsetting it. Artifact Registry is the current recommendation and the API surface is cleaner.

### 4.2 Module: `iam`

**Purpose:** Create the three service accounts the system needs and bind roles at the project level where appropriate.

**Service accounts to create:**

| Name | Purpose | Roles (project-level) |
|---|---|---|
| `jugnu-worker-{env}` | Runtime identity for both Cloud Run jobs | `roles/cloudsql.client`, `roles/logging.logWriter`, `roles/secretmanager.secretAccessor`, `roles/storage.objectCreator`, `roles/storage.objectViewer` |
| `jugnu-scheduler-{env}` | Identity Cloud Scheduler uses to trigger the Cloud Run jobs | `roles/run.invoker` on both jobs (bound in the `cloud_run_jobs` module, not here) |
| `jugnu-cleanup-{env}` | Identity for any future cleanup/admin jobs that need delete permissions | `roles/storage.objectAdmin` |

**Note on worker permissions:** The arch doc says `roles/storage.objectAdmin`. We are deliberately narrower — `objectCreator` + `objectViewer`. Workers should never delete; the lifecycle rules do that. If a worker needs to delete (it shouldn't), that's a code smell worth catching via the IAM error.

**Outputs:**
- `worker_sa_email`, `scheduler_sa_email`, `cleanup_sa_email` — all consumed by other modules

### 4.3 Module: `storage`

**Purpose:** The single `jugnu-raw-{env}` bucket with three-zone lifecycle rules.

**Resource:** `google_storage_bucket` with:

```hcl
name          = "jugnu-raw-${var.env}"
location      = var.region
storage_class = "STANDARD"
force_destroy = var.env == "staging"   # prod must be false — prevents accidental state loss

uniform_bucket_level_access = true
public_access_prevention    = "enforced"

versioning { enabled = true }

# Zone 1: runs/ — hot for 7 days
lifecycle_rule {
  condition {
    age            = 7
    matches_prefix = ["runs/"]
  }
  action {
    type          = "SetStorageClass"
    storage_class = "NEARLINE"
  }
}

# Zone 2: runs/ — cold at 30 days
lifecycle_rule {
  condition {
    age            = 30
    matches_prefix = ["runs/"]
  }
  action {
    type          = "SetStorageClass"
    storage_class = "COLDLINE"
  }
}

# Zone 3: runs/ — deleted at 90 days
lifecycle_rule {
  condition {
    age            = 90
    matches_prefix = ["runs/"]
  }
  action { type = "Delete" }
}

# Non-current object versions of property-list/: keep 30 days
lifecycle_rule {
  condition {
    num_newer_versions = 3
    matches_prefix     = ["property-list/"]
  }
  action { type = "Delete" }
}
```

**Structure inside the bucket (Terraform does not create these objects — the deploy workflow and runtime do):**

```
gs://jugnu-raw-{env}/
├── property-list/properties.csv    # versioned input; deploy workflow uploads
├── canary/properties.csv           # 3 properties for smoke tests
├── runs/{run_date}/shard_{idx}/*   # runtime output; lifecycle-managed
└── profiles/                        # future: nightly export from Postgres
```

**Outputs:** `bucket_name`, `bucket_url` (`gs://...`).

### 4.4 Module: `cloud_sql`

**Purpose:** Postgres instance with private IP, IAM authentication, and stop-when-idle support.

**Resources:**

1. `google_sql_database_instance`:
   - `database_version = "POSTGRES_15"`
   - `tier = var.db_tier` (default `db-f1-micro` per arch doc; `db-g1-small` when task_count > 15)
   - `availability_type = "ZONAL"` (no HA — arch doc §7)
   - `backup_configuration { enabled = true, start_time = "03:30" }` — after the 3am SQL stop window, before the 2am start
   - `ip_configuration { ipv4_enabled = false, private_network = var.vpc_self_link }`
   - `deletion_protection = var.env == "prod"` — **critical**; without this, a bad `terraform destroy` on prod wipes the database
   - `database_flags { name = "cloudsql.iam_authentication", value = "on" }`

2. `google_sql_database` named `jugnu` on the instance

3. `google_sql_user` for each human developer (emails passed in via `var.developer_emails`), type `CLOUD_IAM_USER`

4. `google_sql_user` for the worker SA, type `CLOUD_IAM_SERVICE_ACCOUNT`

5. `google_vpc_access_connector` for the Cloud Run jobs to reach the private IP — `machine_type = "e2-micro"`, `min_instances = 2`, `max_instances = 3`

**Stop-when-idle:** Terraform does **not** manage the stopped/running state. The Cloud Scheduler module creates two scheduler entries (start at 2am, stop at 3am) that call the SQL Admin API. Terraform sets `activation_policy = "ALWAYS"` as the *initial* state; scheduler overrides it during the day.

**CI/CD trap this creates:** migrations during deploy will fail if the scheduler has just stopped the database. `CLAUDE_DEPLOY.md` handles this — deploy workflow must call `gcloud sql instances patch --activation-policy=ALWAYS`, wait for READY, then run migrations. Document this in the module's README.

**Outputs:** `instance_connection_name` (for the auth proxy), `private_ip`, `database_name`.

### 4.5 Module: `secrets`

**Purpose:** Create secret slots; do **not** write values.

**Resources:**

```hcl
resource "google_secret_manager_secret" "openrouter_api_key" {
  secret_id = "openrouter-api-key-${var.env}"
  replication { auto {} }
}

resource "google_secret_manager_secret" "proxy_credentials" {
  secret_id = "proxy-credentials-${var.env}"
  replication { auto {} }
}

# IAM: worker SA can access these secrets
resource "google_secret_manager_secret_iam_member" "worker_openrouter" {
  secret_id = google_secret_manager_secret.openrouter_api_key.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${var.worker_sa_email}"
}
# ... same for proxy_credentials
```

**Do not `google_secret_manager_secret_version` the values in Terraform.** Putting secret values in tfvars or in the state file is the most common way secrets leak. Values are written once by a human via `gcloud secrets versions add` during the one-time bootstrap (see §8).

**Outputs:** `openrouter_secret_id`, `proxy_credentials_secret_id` — consumed by the Cloud Run job module to construct `env { value_source { secret_key_ref {} } }` blocks.

### 4.6 Module: `cloud_run_jobs`

**Purpose:** Both Cloud Run jobs — the main scrape job and the retry job. Same image, different entry points.

**Resource 1: `google_cloud_run_v2_job.jugnu_scrape`**

```hcl
name     = "jugnu-scrape-${var.env}"
location = var.region

template {
  parallelism = var.default_task_count   # 5 for prod; overridable per-execution
  task_count  = var.default_task_count

  template {
    service_account  = var.worker_sa_email
    timeout          = "14400s"          # 4h hard ceiling — matches budget
    max_retries      = 1
    execution_environment = "EXECUTION_ENVIRONMENT_GEN2"

    vpc_access {
      connector = var.vpc_connector_id
      egress    = "PRIVATE_RANGES_ONLY"  # proxy + SQL stay on private; public web goes direct
    }

    containers {
      image   = "${var.repository_url}/jugnu:${var.image_tag}"
      command = ["python", "scripts/jugnu_shard_entry.py"]

      resources {
        limits = {
          cpu    = var.task_cpu          # "2"
          memory = var.task_memory       # "4Gi"
        }
      }

      env {
        name  = "BROWSERS_PER_TASK"
        value = tostring(var.browsers_per_task)  # default 10
      }
      env {
        name  = "CSV_GCS_URI"
        value = "gs://${var.bucket_name}/property-list/properties.csv"
      }
      env {
        name  = "DATABASE_URL"
        value = "postgresql://${var.worker_sa_email}@${var.sql_private_ip}:5432/jugnu?sslmode=require"
      }
      env {
        name = "OPENROUTER_API_KEY"
        value_source {
          secret_key_ref {
            secret  = var.openrouter_secret_id
            version = "latest"
          }
        }
      }
      env {
        name = "PROXY_CREDENTIALS"
        value_source {
          secret_key_ref {
            secret  = var.proxy_credentials_secret_id
            version = "latest"
          }
        }
      }
      # CLOUD_RUN_TASK_INDEX and CLOUD_RUN_TASK_COUNT are injected by Cloud Run automatically
    }
  }
}
```

**Resource 2: `google_cloud_run_v2_job.jugnu_retry`**

Same shape as scrape, with these overrides:
- `name = "jugnu-retry-${var.env}"`
- `parallelism = 1`, `task_count = 1` (retry volume is small; no sharding)
- `command = ["python", "scripts/jugnu_retry_entry.py"]`
- Additional env var: `RETRY_MODE` with default value `"errors"` (overridable at execution time via `--update-env-vars`)
- `timeout = "3600s"` — retries of 50-100 failed properties should never take an hour

**Resource 3: Run invoker binding for the scheduler SA**

```hcl
resource "google_cloud_run_v2_job_iam_member" "scheduler_invokes_scrape" {
  name     = google_cloud_run_v2_job.jugnu_scrape.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "serviceAccount:${var.scheduler_sa_email}"
}
# Same for jugnu_retry — scheduler may or may not use it depending on §4.7
```

**Critical: `image_tag` is a variable, not a literal.** The deploy workflow passes the git SHA as `-var="image_tag=sha-abc123"`. Terraform updates the job's `image` field; Cloud Run picks this up for the next execution. **Never hardcode tags like `latest`** — it defeats the whole point of immutable deploys.

**Outputs:** `scrape_job_name`, `retry_job_name`, `scrape_job_id`, `retry_job_id`.

### 4.7 Module: `scheduler`

**Purpose:** Four scheduler entries, all with the scheduler SA as the OIDC identity.

**Entries:**

| Name | Schedule | Action | Notes |
|---|---|---|---|
| `jugnu-daily-scrape-{env}` | `0 2 * * *` (2am UTC) | POST to Cloud Run job `:run` endpoint | Main scheduled scrape |
| `jugnu-sql-start-{env}` | `30 1 * * *` (1:30am UTC) | PATCH SQL instance `activation_policy=ALWAYS` | 30 min before scrape; gives DB time to warm up |
| `jugnu-sql-stop-{env}` | `0 6 * * *` (6am UTC) | PATCH SQL instance `activation_policy=NEVER` | After 4h scrape budget + buffer for retry job |
| `jugnu-daily-retry-{env}` | `30 6 * * *` (6:30am UTC) | POST to retry job `:run` endpoint | **DISABLED by default** (set `paused = true`); enable only when ready for auto-retry |

**The retry scheduler is paused by default** per the decision in the previous turn — start with human-triggered retries, turn auto-retry on after you have confidence in it.

**One gotcha:** scheduling SQL start/stop requires the scheduler SA to have `roles/cloudsql.admin` on the instance. That's a broader role than the worker SA has. Bind it in this module, scoped to the SQL instance only (not project-level):

```hcl
resource "google_sql_database_instance_iam_member" "scheduler_can_patch" {
  instance = var.sql_instance_name
  role     = "roles/cloudsql.admin"
  member   = "serviceAccount:${var.scheduler_sa_email}"
}
```

*(Note: if this resource type doesn't exist in the provider version, fall back to a project-level IAM condition that restricts to the specific instance by resource name. Check provider docs at implementation time.)*

**Outputs:** `scheduler_job_names` (list, for visibility in `outputs.tf`).

---

## 5. Root module wiring

```hcl
# infra/terraform/main.tf
module "artifact_registry" {
  source       = "./modules/artifact_registry"
  env          = var.env
  project_id   = var.project_id
  region       = var.region
  deployer_sa  = var.deployer_sa_email   # from WIF bootstrap; see §8
}

module "iam" {
  source     = "./modules/iam"
  env        = var.env
  project_id = var.project_id
}

module "storage" {
  source = "./modules/storage"
  env    = var.env
  region = var.region
}

module "cloud_sql" {
  source             = "./modules/cloud_sql"
  env                = var.env
  region             = var.region
  db_tier            = var.db_tier
  vpc_self_link      = var.vpc_self_link
  developer_emails   = var.developer_emails
  worker_sa_email    = module.iam.worker_sa_email
}

module "secrets" {
  source          = "./modules/secrets"
  env             = var.env
  worker_sa_email = module.iam.worker_sa_email
}

module "cloud_run_jobs" {
  source                       = "./modules/cloud_run_jobs"
  env                          = var.env
  region                       = var.region
  repository_url               = module.artifact_registry.repository_url
  image_tag                    = var.image_tag
  worker_sa_email              = module.iam.worker_sa_email
  scheduler_sa_email           = module.iam.scheduler_sa_email
  vpc_connector_id             = module.cloud_sql.vpc_connector_id
  sql_private_ip               = module.cloud_sql.private_ip
  bucket_name                  = module.storage.bucket_name
  openrouter_secret_id         = module.secrets.openrouter_secret_id
  proxy_credentials_secret_id  = module.secrets.proxy_credentials_secret_id

  default_task_count = var.default_task_count
  browsers_per_task  = var.browsers_per_task
  task_cpu           = var.task_cpu
  task_memory        = var.task_memory
}

module "scheduler" {
  source               = "./modules/scheduler"
  env                  = var.env
  region               = var.region
  scheduler_sa_email   = module.iam.scheduler_sa_email
  scrape_job_id        = module.cloud_run_jobs.scrape_job_id
  retry_job_id         = module.cloud_run_jobs.retry_job_id
  sql_instance_name    = module.cloud_sql.instance_name
  retry_scheduler_paused = true  # disabled by default
}
```

---

## 6. Variables

```hcl
# infra/terraform/variables.tf
variable "env"        { type = string }             # "staging" or "prod"
variable "project_id" { type = string }
variable "region"     { type = string default = "us-central1" }
variable "vpc_self_link" { type = string }

# Identities bootstrapped outside TF
variable "deployer_sa_email"   { type = string }
variable "developer_emails"    { type = list(string) default = [] }

# Knobs
variable "image_tag"          { type = string }              # REQUIRED at apply time
variable "default_task_count" { type = number default = 5 }
variable "browsers_per_task"  { type = number default = 10 }
variable "task_cpu"           { type = string default = "2" }
variable "task_memory"        { type = string default = "4Gi" }
variable "db_tier"            { type = string default = "db-f1-micro" }
```

**`envs/staging.tfvars`:**
```hcl
env        = "staging"
project_id = "jugnu-staging-<unique>"
# image_tag supplied by CI at apply time
default_task_count = 3                # smaller for staging
developer_emails   = ["you@company.com"]
```

**`envs/prod.tfvars`:**
```hcl
env        = "prod"
project_id = "jugnu-prod-<unique>"
default_task_count = 5                # per arch doc
db_tier            = "db-f1-micro"    # upgrade to g1-small if task_count > 15
developer_emails   = ["you@company.com", "teammate@company.com"]
```

---

## 7. Backend configuration

```hcl
# infra/terraform/backend.tf
terraform {
  backend "gcs" {
    # bucket and prefix supplied via -backend-config at init time
  }
}
```

Init command per environment:
```bash
terraform init \
  -backend-config="bucket=jugnu-tfstate-staging" \
  -backend-config="prefix=terraform/state"
```

**Both tfstate buckets must be created manually (or via a separate bootstrap TF root) before the first `terraform init`.** Chicken-and-egg: you can't use TF to create the bucket TF stores state in. Bootstrap bucket creation is part of the human prerequisite in §8.

---

## 8. Human bootstrap prerequisites

These must be done **once per GCP project** by a human with org-level access. Terraform can't do them because they're the things Terraform uses to authenticate.

**Per environment (staging, then prod):**

1. Create the GCP project, link billing
2. Enable APIs: `run.googleapis.com`, `sqladmin.googleapis.com`, `artifactregistry.googleapis.com`, `secretmanager.googleapis.com`, `cloudscheduler.googleapis.com`, `vpcaccess.googleapis.com`, `servicenetworking.googleapis.com`, `compute.googleapis.com`
3. Create VPC with a private services access range for Cloud SQL peering (one-time network setup; documented in `infra/terraform/README.md`)
4. Create tfstate bucket: `gsutil mb -l {region} -b on gs://jugnu-tfstate-{env}`; enable versioning: `gsutil versioning set on gs://jugnu-tfstate-{env}`
5. Configure Workload Identity Federation pool + provider pointing at the GitHub repo
6. Create `github-deployer-{env}` SA, bind it to the WIF principal, grant project-level `roles/owner` (narrower roles can come later, once you know exactly what TF touches)
7. Write secret values: `echo -n "$OPENROUTER_KEY" | gcloud secrets versions add openrouter-api-key-{env} --data-file=-` (after first `terraform apply` creates the secret slot; the versions add is idempotent to run manually)

Store in GitHub repo secrets: `GCP_PROJECT_ID_STAGING`, `GCP_PROJECT_ID_PROD`, `WIF_PROVIDER`, `WIF_SA_STAGING`, `WIF_SA_PROD`, `REGION`.

This bootstrap is intentionally not in Terraform — the tradeoff is "write it down once and don't automate it" vs. "spend three days getting WIF-by-Terraform to work." Pick the first one.

---

## 9. Gates

Each gate is a binary pass/fail check. Claude Code must verify each before marking the handoff complete. Model this on `scripts/gate_refactor.py` — one function per gate, returns pass/fail with reasons.

| Gate | Check | How to verify |
|---|---|---|
| TF-1 | `terraform fmt -recursive -check` clean | `cd infra/terraform && terraform fmt -recursive -check` exits 0 |
| TF-2 | `terraform validate` passes for all modules | Run in root with each tfvars: `terraform validate -var-file=envs/staging.tfvars` |
| TF-3 | `terraform plan` succeeds against staging | With real backend init; exit 0, non-empty plan |
| TF-4 | `terraform apply` succeeds against staging | Human-run on first apply; subsequent applies show zero-diff plans when nothing changed |
| TF-5 | All six expected resource types present | `terraform state list` includes artifact_registry, cloud_run_v2_job (×2), sql_database_instance, storage_bucket, secret (×2), cloud_scheduler_job (×4), service_account (×3) |
| TF-6 | Worker SA has exactly 5 project roles | `gcloud projects get-iam-policy {project} --flatten=bindings --filter='bindings.members:jugnu-worker-*'` shows exactly: `cloudsql.client`, `logging.logWriter`, `secretmanager.secretAccessor`, `storage.objectCreator`, `storage.objectViewer` — no more, no less |
| TF-7 | Cloud Run scrape job can be executed manually with default params | `gcloud run jobs execute jugnu-scrape-staging --wait` — succeeds even if the run itself does no useful work (tests plumbing: image pull, SA auth, VPC, secrets) |
| TF-8 | Cloud Run retry job can be executed manually | Same as TF-7 for `jugnu-retry-staging` |
| TF-9 | Bucket lifecycle rules applied | `gcloud storage buckets describe gs://jugnu-raw-staging --format='value(lifecycle)'` shows 4 rules (NEARLINE, COLDLINE, Delete, version-prune) |
| TF-10 | SQL instance has IAM auth flag and private IP | `gcloud sql instances describe jugnu-db-staging` shows `databaseFlags[].cloudsql.iam_authentication=on` and no `ipAddresses[].ipAddressType=PRIMARY` |
| TF-11 | Scheduler has exactly 4 jobs, retry paused | `gcloud scheduler jobs list --location={region}` shows 4 jobs; `jugnu-daily-retry-staging` has state `PAUSED` |

Non-gate verification the implementer should do manually before declaring done:
- `terraform destroy` on staging succeeds cleanly (important: confirms there are no circular dependencies or stuck resources)
- Re-apply after destroy produces identical resources — tests idempotency
- Running `terraform plan` on an unchanged state produces zero diff — tests that all attributes are fully captured in code

---

## 10. Non-negotiables

Patterns this handoff must enforce, borrowed from `claude_refactor.md`:

- **No module imports another module.** Wiring is the root's job. A module should be droppable into another project unchanged.
- **No hardcoded project IDs, region names, or email addresses.** Everything goes through variables.
- **No `google_project_iam_binding`.** That resource type is authoritative and will silently remove IAM bindings created outside TF. Always use `google_project_iam_member` (additive).
- **No secret values in tfvars or code.** Secret *slots* are TF-managed; values are written manually.
- **No `for_each` over a list of resources where a map would do.** Maps are stable under reorder; lists aren't — a reorder causes Terraform to destroy and recreate everything after the changed position.
- **No `depends_on` blocks unless a comment explains why Terraform's implicit ordering isn't sufficient.** Implicit ordering via resource references is more robust.

---

## 11. Open questions to resolve with operator before starting

- **VPC:** do we have an existing VPC in each project, or does Terraform create one? If creating, does that live in this root or in a separate "bootstrap" root? Recommendation: separate root, applied once, output fed into this root's variables.
- **tfstate bucket location:** multi-region US, or same region as everything else? Recommendation: same region — cross-region state reads add latency to every plan.
- **Deployer SA scope:** start with `roles/owner`, narrow later, or start narrow? Starting narrow means a week of "what permission is missing now" pain. Recommendation: `roles/owner` on staging for week one; narrow before prod ever gets applied.
- **Pin `google` provider to 5.40 or track latest?** Recommendation: pin now, revisit quarterly. Provider upgrades occasionally rename attributes.

---

## 12. When this handoff is complete

Claude Code has:
1. Created every file in §2
2. All gates in §9 pass on staging
3. Written `infra/terraform/README.md` as an ops runbook covering: one-time bootstrap, per-env apply, state recovery from a corrupted state, emergency "disable the scheduled scrape" procedure
4. Posted a summary PR description listing every GCP resource created, its purpose, and its monthly cost estimate

Only then start on `CLAUDE_DEPLOY.md`.
