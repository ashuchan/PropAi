# ── Service accounts ──────────────────────────────────────────────────────────

resource "google_service_account" "worker" {
  account_id   = "jugnu-worker-${var.env}"
  display_name = "Jugnu worker — Cloud Run tasks (${var.env})"
  project      = var.project_id
}

resource "google_service_account" "scheduler" {
  account_id   = "jugnu-scheduler-${var.env}"
  display_name = "Jugnu scheduler — Cloud Scheduler invoker (${var.env})"
  project      = var.project_id
}

resource "google_service_account" "cleanup" {
  account_id   = "jugnu-cleanup-${var.env}"
  display_name = "Jugnu cleanup — GCS object admin (${var.env})"
  project      = var.project_id
}

# ── Worker project-level roles (additive; never use google_project_iam_binding) ─

resource "google_project_iam_member" "worker_cloudsql_client" {
  project = var.project_id
  role    = "roles/cloudsql.client"
  member  = "serviceAccount:${google_service_account.worker.email}"
}

resource "google_project_iam_member" "worker_log_writer" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.worker.email}"
}

resource "google_project_iam_member" "worker_secret_accessor" {
  project = var.project_id
  role    = "roles/secretmanager.secretAccessor"
  member  = "serviceAccount:${google_service_account.worker.email}"
}

resource "google_project_iam_member" "worker_storage_creator" {
  project = var.project_id
  role    = "roles/storage.objectCreator"
  member  = "serviceAccount:${google_service_account.worker.email}"
}

resource "google_project_iam_member" "worker_storage_viewer" {
  project = var.project_id
  role    = "roles/storage.objectViewer"
  member  = "serviceAccount:${google_service_account.worker.email}"
}

# ── Cleanup project-level roles ────────────────────────────────────────────────

resource "google_project_iam_member" "cleanup_storage_admin" {
  project = var.project_id
  role    = "roles/storage.objectAdmin"
  member  = "serviceAccount:${google_service_account.cleanup.email}"
}
