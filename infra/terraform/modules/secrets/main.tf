# Secret slots only — values are written manually via:
#   echo -n "$KEY" | gcloud secrets versions add openrouter-api-key-{env} --data-file=-
# Never put secret values in Terraform or tfvars.

resource "google_secret_manager_secret" "openrouter_api_key" {
  secret_id = "openrouter-api-key-${var.env}"
  replication {
    auto {}
  }
}

resource "google_secret_manager_secret" "proxy_credentials" {
  secret_id = "proxy-credentials-${var.env}"
  replication {
    auto {}
  }
}

# Grant worker SA access to both secrets
resource "google_secret_manager_secret_iam_member" "worker_openrouter" {
  secret_id = google_secret_manager_secret.openrouter_api_key.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${var.worker_sa_email}"
}

resource "google_secret_manager_secret_iam_member" "worker_proxy_credentials" {
  secret_id = google_secret_manager_secret.proxy_credentials.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${var.worker_sa_email}"
}
