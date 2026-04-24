# ── Scrape job ────────────────────────────────────────────────────────────────

resource "google_cloud_run_v2_job" "jugnu_scrape" {
  name     = "jugnu-scrape-${var.env}"
  location = var.region

  template {
    parallelism = var.default_task_count
    task_count  = var.default_task_count

    template {
      service_account       = var.worker_sa_email
      timeout               = "14400s" # 4h hard ceiling
      max_retries           = 1
      execution_environment = "EXECUTION_ENVIRONMENT_GEN2"

      vpc_access {
        connector = var.vpc_connector_id
        egress    = "PRIVATE_RANGES_ONLY"
      }

      containers {
        image   = "${var.repository_url}/jugnu:${var.image_tag}"
        command = ["python", "ma_poc/scripts/jugnu_shard_entry.py"]

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
        # (ma_poc/scripts/jugnu_runner.py:_resolve_data_dirs).
        env {
          name  = "SCHEMA_VERSION"
          value = "v2"
        }
        # Selects the text LLM provider in ma_poc/llm/factory.py. Default
        # there is "anthropic", which reads ANTHROPIC_API_KEY — not the
        # key this job has. Without this override the canary's LLM
        # fallback path fails with "Could not resolve authentication
        # method" and the shard exits 1.
        env {
          name  = "LLM_PROVIDER"
          value = "openrouter"
        }
        # Model IDs — read by ma_poc/llm/openrouter.py. Tune per env in
        # envs/*.tfvars if staging should differ from prod.
        env {
          name  = "OPENROUTER_MODEL"
          value = var.openrouter_model
        }
        env {
          name  = "OPENROUTER_VISION_MODEL"
          value = var.openrouter_vision_model
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
    parallelism = 1 # retry volume is small; no sharding needed
    task_count  = 1

    template {
      service_account       = var.worker_sa_email
      timeout               = "3600s" # 1h is generous for 50-100 retries
      max_retries           = 0
      execution_environment = "EXECUTION_ENVIRONMENT_GEN2"

      vpc_access {
        connector = var.vpc_connector_id
        egress    = "PRIVATE_RANGES_ONLY"
      }

      containers {
        image   = "${var.repository_url}/jugnu:${var.image_tag}"
        command = ["python", "ma_poc/scripts/jugnu_retry_entry.py"]

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
          value = "openrouter"
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
      }
    }
  }
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
