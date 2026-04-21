locals {
  scrape_job_name = element(split("/", var.scrape_job_id), length(split("/", var.scrape_job_id)) - 1)
  retry_job_name  = element(split("/", var.retry_job_id), length(split("/", var.retry_job_id)) - 1)

  # Cloud Run Jobs :run endpoint
  scrape_run_uri = "https://${var.region}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${var.project_id}/jobs/${local.scrape_job_name}:run"
  retry_run_uri  = "https://${var.region}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${var.project_id}/jobs/${local.retry_job_name}:run"

  # Cloud SQL Admin API patch endpoint
  sql_patch_uri = "https://sqladmin.googleapis.com/sql/v1beta4/projects/${var.project_id}/instances/${var.sql_instance_name}"
}

# 1:30am UTC — start SQL 30 min before the scrape
resource "google_cloud_scheduler_job" "sql_start" {
  name             = "jugnu-sql-start-${var.env}"
  description      = "Start Cloud SQL before the daily scrape window"
  schedule         = "30 1 * * *"
  time_zone        = "UTC"
  region           = var.region
  attempt_deadline = "180s"

  http_target {
    uri         = local.sql_patch_uri
    http_method = "PATCH"
    body        = base64encode(jsonencode({ settings = { activationPolicy = "ALWAYS" } }))
    headers = {
      "Content-Type" = "application/json"
    }
    oauth_token {
      service_account_email = var.scheduler_sa_email
    }
  }
}

# 2:00am UTC — daily scrape
resource "google_cloud_scheduler_job" "daily_scrape" {
  name             = "jugnu-daily-scrape-${var.env}"
  description      = "Trigger the nightly Jugnu scrape"
  schedule         = "0 2 * * *"
  time_zone        = "UTC"
  region           = var.region
  attempt_deadline = "180s"

  http_target {
    uri         = local.scrape_run_uri
    http_method = "POST"
    body        = base64encode("{}")
    headers = {
      "Content-Type" = "application/json"
    }
    oauth_token {
      service_account_email = var.scheduler_sa_email
    }
  }
}

# 6:00am UTC — stop SQL (after 4h scrape budget + buffer)
resource "google_cloud_scheduler_job" "sql_stop" {
  name             = "jugnu-sql-stop-${var.env}"
  description      = "Stop Cloud SQL after the daily scrape window"
  schedule         = "0 6 * * *"
  time_zone        = "UTC"
  region           = var.region
  attempt_deadline = "180s"

  http_target {
    uri         = local.sql_patch_uri
    http_method = "PATCH"
    body        = base64encode(jsonencode({ settings = { activationPolicy = "NEVER" } }))
    headers = {
      "Content-Type" = "application/json"
    }
    oauth_token {
      service_account_email = var.scheduler_sa_email
    }
  }
}

# 6:30am UTC — retry job (PAUSED by default)
resource "google_cloud_scheduler_job" "daily_retry" {
  name             = "jugnu-daily-retry-${var.env}"
  description      = "Auto-trigger retry job (paused until validated)"
  schedule         = "30 6 * * *"
  time_zone        = "UTC"
  region           = var.region
  attempt_deadline = "180s"
  paused           = var.retry_scheduler_paused

  http_target {
    uri         = local.retry_run_uri
    http_method = "POST"
    body        = base64encode("{}")
    headers = {
      "Content-Type" = "application/json"
    }
    oauth_token {
      service_account_email = var.scheduler_sa_email
    }
  }
}

# Grant scheduler SA roles/cloudsql.admin scoped to this project (for SQL start/stop)
# Note: ideally scoped to the instance, but Cloud SQL instance IAM binding resource
# may not be available in all provider versions; project-level is the safe fallback.
resource "google_project_iam_member" "scheduler_cloudsql_admin" {
  project = var.project_id
  role    = "roles/cloudsql.admin"
  member  = "serviceAccount:${var.scheduler_sa_email}"
}
