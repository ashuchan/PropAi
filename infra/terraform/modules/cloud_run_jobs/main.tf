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
