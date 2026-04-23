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
        env {
          name  = "DATABASE_URL"
          value = "postgresql://${var.worker_sa_email}@${var.sql_private_ip}:5432/jugnu?sslmode=require"
        }
        # ma_poc/data_provider/factory.py defaults to "filesystem", which
        # writes events.jsonl + cost_ledger.db to GCS instead of the
        # Postgres tables alembic created. Set "postgres" so scrape
        # output lands in the DB. Swap to "dual" temporarily if you want
        # belt-and-braces GCS + PG writes during a cutover.
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
          value = "postgresql://${var.worker_sa_email}@${var.sql_private_ip}:5432/jugnu?sslmode=require"
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
