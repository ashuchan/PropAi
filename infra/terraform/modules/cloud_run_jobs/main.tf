# ── Scrape job ────────────────────────────────────────────────────────────────

resource "google_cloud_run_v2_job" "jugnu_scrape" {
  name     = "jugnu-scrape-${var.env}"
  location = var.region

  template {
    parallelism = var.default_task_count
    task_count  = var.default_task_count

    template {
      service_account       = var.worker_sa_email
      timeout               = "7200s" # 2h hard ceiling
      max_retries           = 1
      execution_environment = "EXECUTION_ENVIRONMENT_GEN2"

      vpc_access {
        connector = var.vpc_connector_id
        egress    = "PRIVATE_RANGES_ONLY"
      }

      containers {
        image   = "${var.repository_url}/jugnu:${var.image_tag}"
        command = ["python", "ma_poc/scripts/runners/shard_entry.py"]

        resources {
          limits = {
            cpu    = var.task_cpu
            memory = var.task_memory
          }
        }

        env {
          name  = "BROWSERS_PER_TASK"
          value = tostring(var.browsers_per_task)
        }
        env {
          name  = "CSV_GCS_URI"
          value = "gs://${var.bucket_name}/property-list/properties.csv"
        }
        env {
          name  = "BUCKET_NAME"
          value = var.bucket_name
        }
        # Scheme must be ``postgresql+psycopg://`` — SQLAlchemy maps the
        # bare ``postgresql://`` prefix to the psycopg2 dialect, which is
        # not installed (requirements.txt only has psycopg v3). The
        # connector overrides host+port at runtime, but we still parse
        # user/db from this URL — keep them correct.
        env {
          name  = "DATABASE_URL"
          value = "postgresql+psycopg://${var.worker_sa_email}@${var.sql_private_ip}:5432/jugnu?sslmode=require"
        }
        # Routes every SQLAlchemy connection through the Cloud SQL
        # Python Connector (see data_provider/sql/engine.py). The
        # connector fetches a short-lived OAuth token from the worker SA
        # and hands it to psycopg as the Postgres password — required
        # because ``cloudsql.iam_authentication=on`` rejects password
        # auth outright.
        env {
          name  = "CLOUD_SQL_INSTANCE"
          value = var.sql_instance_connection_name
        }
        env {
          name  = "CLOUD_SQL_IP_TYPE"
          value = "PRIVATE"
        }
        # Kept for downstream consumers that still read it (e.g. the
        # data-provider factory in contexts that don't go through the
        # shard entry's explicit sync). The scrape runner itself now
        # writes to FS first and calls sync_run_to_pg.py after the run
        # completes, regardless of this value.
        env {
          name  = "DATA_PROVIDER"
          value = "postgres"
        }
        # The Postgres schema this deploy's migrations created is v2
        # (alembic 0002_v2_strict). Jugnu runner reads SCHEMA_VERSION to
        # pick the correct unit-dict shape and output directory
        # (ma_poc/scripts/runners/jugnu.py:_resolve_data_dirs).
        env {
          name  = "SCHEMA_VERSION"
          value = "v2"
        }
        # Selects the text/vision LLM provider in ma_poc/llm/factory.py.
        # Driven by var.llm_provider (anthropic | openrouter | azure).
        # Both API keys are mounted below so switching providers is just
        # a tfvars change — no infra rewrite required.
        env {
          name  = "LLM_PROVIDER"
          value = var.llm_provider
        }
        # Model IDs — read by ma_poc/llm/openrouter.py and
        # ma_poc/llm/anthropic.py. Tune per env in envs/*.tfvars if
        # staging should differ from prod. Keys are injected for both
        # providers; only the one matching LLM_PROVIDER is actually used.
        env {
          name  = "OPENROUTER_MODEL"
          value = var.openrouter_model
        }
        env {
          name  = "OPENROUTER_VISION_MODEL"
          value = var.openrouter_vision_model
        }
        env {
          name  = "ANTHROPIC_MODEL"
          value = var.anthropic_model
        }
        env {
          name  = "ANTHROPIC_VISION_MODEL"
          value = var.anthropic_vision_model
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
          name = "ANTHROPIC_API_KEY"
          value_source {
            secret_key_ref {
              secret  = var.anthropic_secret_id
              version = "latest"
            }
          }
        }
        # L1 Fetcher (ma_poc/fetch/fetcher.py:616) reads PROXY_POOL_URLS
        # — a comma-separated list of full proxy URLs with embedded
        # creds, e.g. ``http://user:pass@brd.superproxy.io:33335``. The
        # secret's string value is injected verbatim, so pre-format it
        # that way with ``gcloud secrets versions add`` (one URL, or
        # comma-separated for a pool).
        env {
          name = "PROXY_POOL_URLS"
          value_source {
            secret_key_ref {
              secret  = var.proxy_credentials_secret_id
              version = "latest"
            }
          }
        }
        # Hyperbrowser API key (task #46). Wired unconditionally so a canary is
        # a pure config flip (set FETCH_BACKEND=hyperbrowser at execution via
        # --update-env-vars); the switchable backend only READS this when that
        # flag is on. The secret always has a placeholder version, so this
        # mount resolves on the default BrightData path too.
        env {
          name = "HYPERBROWSER_API_KEY"
          value_source {
            secret_key_ref {
              secret  = var.hyperbrowser_secret_id
              version = "latest"
            }
          }
        }
        # CLOUD_RUN_TASK_INDEX and CLOUD_RUN_TASK_COUNT are injected automatically by Cloud Run
      }
    }
  }
}

# ── Retry job ─────────────────────────────────────────────────────────────────

resource "google_cloud_run_v2_job" "jugnu_retry" {
  name     = "jugnu-retry-${var.env}"
  location = var.region

  template {
    # Sharded retry: parallelism > 1 spawns N parallel tasks, each
    # processing a disjoint slice of the failure list via
    # CLOUD_RUN_TASK_INDEX/COUNT round-robin in runners/jugnu_retry.py.
    # The merge job (runners/jugnu_retry_merge.py) consolidates shard outputs.
    # 1 = legacy single-task behaviour.
    parallelism = var.retry_task_count
    task_count  = var.retry_task_count

    template {
      service_account       = var.worker_sa_email
      timeout               = var.retry_timeout
      max_retries           = 0
      execution_environment = "EXECUTION_ENVIRONMENT_GEN2"

      vpc_access {
        connector = var.vpc_connector_id
        egress    = "PRIVATE_RANGES_ONLY"
      }

      containers {
        image   = "${var.repository_url}/jugnu:${var.image_tag}"
        command = ["python", "ma_poc/scripts/runners/retry_entry.py"]

        resources {
          limits = {
            cpu    = var.task_cpu
            memory = var.task_memory
          }
        }

        env {
          name  = "RETRY_MODE"
          value = "errors" # overridable at execution time via --update-env-vars
        }
        env {
          name  = "CSV_GCS_URI"
          value = "gs://${var.bucket_name}/property-list/properties.csv"
        }
        env {
          name  = "BUCKET_NAME"
          value = var.bucket_name
        }
        env {
          name  = "DATABASE_URL"
          value = "postgresql+psycopg://${var.worker_sa_email}@${var.sql_private_ip}:5432/jugnu?sslmode=require"
        }
        env {
          name  = "CLOUD_SQL_INSTANCE"
          value = var.sql_instance_connection_name
        }
        env {
          name  = "CLOUD_SQL_IP_TYPE"
          value = "PRIVATE"
        }
        # See scrape-job block above — same rationale for all three.
        env {
          name  = "DATA_PROVIDER"
          value = "postgres"
        }
        env {
          name  = "SCHEMA_VERSION"
          value = "v2"
        }
        env {
          name  = "LLM_PROVIDER"
          value = var.llm_provider
        }
        env {
          name  = "OPENROUTER_MODEL"
          value = var.openrouter_model
        }
        env {
          name  = "OPENROUTER_VISION_MODEL"
          value = var.openrouter_vision_model
        }
        env {
          name  = "ANTHROPIC_MODEL"
          value = var.anthropic_model
        }
        env {
          name  = "ANTHROPIC_VISION_MODEL"
          value = var.anthropic_vision_model
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
          name = "ANTHROPIC_API_KEY"
          value_source {
            secret_key_ref {
              secret  = var.anthropic_secret_id
              version = "latest"
            }
          }
        }
        # L1 Fetcher (ma_poc/fetch/fetcher.py:616) reads PROXY_POOL_URLS
        # — a comma-separated list of full proxy URLs with embedded
        # creds, e.g. ``http://user:pass@brd.superproxy.io:33335``. The
        # secret's string value is injected verbatim, so pre-format it
        # that way with ``gcloud secrets versions add`` (one URL, or
        # comma-separated for a pool).
        env {
          name = "PROXY_POOL_URLS"
          value_source {
            secret_key_ref {
              secret  = var.proxy_credentials_secret_id
              version = "latest"
            }
          }
        }
        # Hyperbrowser API key (task #46) — mirror the scrape job so a retry
        # pass with FETCH_BACKEND=hyperbrowser can reach the CF-walled cohort.
        env {
          name = "HYPERBROWSER_API_KEY"
          value_source {
            secret_key_ref {
              secret  = var.hyperbrowser_secret_id
              version = "latest"
            }
          }
        }
      }
    }
  }
}

# ── Ad-hoc script runner job ──────────────────────────────────────────────────
#
# Generic on-demand dispatcher. Operators pick a script from the Cloud Run
# console at execute time:
#   Console → Jobs → jugnu-adhoc-{env} → EXECUTE
#     → Container, variables & secrets
#     → set SCRIPT_NAME (e.g. "validate_outputs")
#     → optionally set SCRIPT_ARGS (e.g. "--csv config/properties.csv")
#     → Run
#
# Reuses the jugnu image so any script in ma_poc/scripts/ that touches
# Cloud SQL or GCS works without extra wiring (worker SA, VPC connector,
# Cloud SQL Connector envs, bucket name, and provider secrets are all
# present, identical to jugnu_scrape above).
#
# parallelism=1, task_count=1 — these are interactive one-offs, not batch
# work. timeout is generous because some scripts (backfills, syncs) run
# long; bump retry_timeout / a dedicated var if 2h proves tight.

resource "google_cloud_run_v2_job" "jugnu_adhoc" {
  name     = "jugnu-adhoc-${var.env}"
  location = var.region

  template {
    parallelism = 1
    task_count  = 1

    template {
      service_account       = var.worker_sa_email
      timeout               = "7200s"
      max_retries           = 0
      execution_environment = "EXECUTION_ENVIRONMENT_GEN2"

      vpc_access {
        connector = var.vpc_connector_id
        egress    = "PRIVATE_RANGES_ONLY"
      }

      containers {
        image   = "${var.repository_url}/jugnu:${var.image_tag}"
        command = ["python", "-m", "ma_poc.scripts.runners.dispatcher"]

        resources {
          limits = {
            cpu    = var.task_cpu
            memory = var.task_memory
          }
        }

        # Empty defaults — operator overrides at execute time. The
        # dispatcher exits with a clear "SCRIPT_NAME is required" error
        # if neither env nor argv supplies one, so an accidental click of
        # EXECUTE without overrides fails fast instead of running stale
        # work.
        env {
          name  = "SCRIPT_NAME"
          value = ""
        }
        env {
          name  = "SCRIPT_ARGS"
          value = ""
        }

        # Cloud SQL + GCS wiring — identical to jugnu_scrape so any
        # script can ``from ma_poc.data_provider...`` or read the
        # properties bucket without extra setup.
        env {
          name  = "CSV_GCS_URI"
          value = "gs://${var.bucket_name}/property-list/properties.csv"
        }
        env {
          name  = "BUCKET_NAME"
          value = var.bucket_name
        }
        env {
          name  = "DATABASE_URL"
          value = "postgresql+psycopg://${var.worker_sa_email}@${var.sql_private_ip}:5432/jugnu?sslmode=require"
        }
        env {
          name  = "CLOUD_SQL_INSTANCE"
          value = var.sql_instance_connection_name
        }
        env {
          name  = "CLOUD_SQL_IP_TYPE"
          value = "PRIVATE"
        }
        env {
          name  = "DATA_PROVIDER"
          value = "postgres"
        }
        env {
          name  = "SCHEMA_VERSION"
          value = "v2"
        }
        env {
          name  = "LLM_PROVIDER"
          value = var.llm_provider
        }
        env {
          name  = "OPENROUTER_MODEL"
          value = var.openrouter_model
        }
        env {
          name  = "OPENROUTER_VISION_MODEL"
          value = var.openrouter_vision_model
        }
        env {
          name  = "ANTHROPIC_MODEL"
          value = var.anthropic_model
        }
        env {
          name  = "ANTHROPIC_VISION_MODEL"
          value = var.anthropic_vision_model
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
          name = "ANTHROPIC_API_KEY"
          value_source {
            secret_key_ref {
              secret  = var.anthropic_secret_id
              version = "latest"
            }
          }
        }
        env {
          name = "PROXY_POOL_URLS"
          value_source {
            secret_key_ref {
              secret  = var.proxy_credentials_secret_id
              version = "latest"
            }
          }
        }
        # Hyperbrowser API key (task #46) — available to adhoc canary runs.
        env {
          name = "HYPERBROWSER_API_KEY"
          value_source {
            secret_key_ref {
              secret  = var.hyperbrowser_secret_id
              version = "latest"
            }
          }
        }

        # ── Email transport ────────────────────────────────────────────
        # Read by scripts/email/daily.py::_email_transport and friends.
        # gmail_api is the production transport; mcp is kept as a
        # workstation-only fallback (Node + ~/.gmail-mcp/credentials.json
        # are not present in the image).
        env {
          name  = "EMAIL_TRANSPORT"
          value = var.email_transport
        }
        # The runtime SA is the worker SA (Cloud SQL IAM auth needs that),
        # so the gmail_api path uses the iam.Signer impersonation chain.
        # GMAIL_EMAILER_SA tells _build_gmail_api_credentials which SA to
        # impersonate via signJwt.
        env {
          name  = "GMAIL_EMAILER_SA"
          value = var.gmail_emailer_sa_email
        }
        env {
          name  = "GMAIL_DELEGATED_USER"
          value = var.gmail_delegated_user
        }
        env {
          name  = "REPORT_RECIPIENTS"
          value = var.report_recipients
        }
        env {
          name  = "REPORT_SENDER_NAME"
          value = var.report_sender_name
        }
      }
    }
  }
}

# Lets the worker SA mint impersonated tokens for the emailer SA via
# iamcredentials.signJwt. Without this, the gmail_api transport on
# Cloud Run fails at refresh time with
# `Permission 'iam.serviceAccounts.signJwt' denied`.
#
# Conditionally created — when gmail_emailer_sa_email is empty (e.g.
# initial bootstrap before the emailer SA exists), no binding is made
# and the email scripts simply fail at send time.
resource "google_service_account_iam_member" "worker_can_impersonate_emailer" {
  count              = var.gmail_emailer_sa_email != "" ? 1 : 0
  service_account_id = "projects/-/serviceAccounts/${var.gmail_emailer_sa_email}"
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:${var.worker_sa_email}"
}

# ── Scheduler SA run invoker bindings ─────────────────────────────────────────

resource "google_cloud_run_v2_job_iam_member" "scheduler_invokes_scrape" {
  name     = google_cloud_run_v2_job.jugnu_scrape.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "serviceAccount:${var.scheduler_sa_email}"
}

resource "google_cloud_run_v2_job_iam_member" "scheduler_invokes_retry" {
  name     = google_cloud_run_v2_job.jugnu_retry.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "serviceAccount:${var.scheduler_sa_email}"
}
