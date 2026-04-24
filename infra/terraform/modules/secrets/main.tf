# Secret slots only — values are written manually via:
#   echo -n "$KEY" | gcloud secrets versions add openrouter-api-key-{env} --data-file=-
#   echo -n "$KEY" | gcloud secrets versions add anthropic-api-key-{env} --data-file=-
# Never put secret values in Terraform or tfvars.

resource "google_secret_manager_secret" "openrouter_api_key" {
  secret_id = "openrouter-api-key-${var.env}"
  replication {
    auto {}
  }
}

resource "google_secret_manager_secret" "anthropic_api_key" {
  secret_id = "anthropic-api-key-${var.env}"
  replication {
    auto {}
  }
}

# Bootstrap placeholder so the first ``terraform apply`` that also wires
# this secret into the Cloud Run jobs doesn't fail with "Secret ...
# /versions/latest was not found". Operator overwrites with the real
# key via ``gcloud secrets versions add`` — Cloud Run resolves ``latest``
# at each execution, so new executions pick up the real value
# immediately. ``lifecycle.ignore_changes`` keeps Terraform from fighting
# the operator's manually-added versions on subsequent applies.
resource "google_secret_manager_secret_version" "anthropic_api_key_placeholder" {
  secret      = google_secret_manager_secret.anthropic_api_key.id
  secret_data = "PLACEHOLDER_REPLACE_VIA_GCLOUD"
  lifecycle {
    ignore_changes = [secret_data, enabled]
  }
}

resource "google_secret_manager_secret" "proxy_credentials" {
  secret_id = "proxy-credentials-${var.env}"
  replication {
    auto {}
  }
}

# Grant worker SA access to all secrets
resource "google_secret_manager_secret_iam_member" "worker_openrouter" {
  secret_id = google_secret_manager_secret.openrouter_api_key.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${var.worker_sa_email}"
}

resource "google_secret_manager_secret_iam_member" "worker_anthropic" {
  secret_id = google_secret_manager_secret.anthropic_api_key.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${var.worker_sa_email}"
}

resource "google_secret_manager_secret_iam_member" "worker_proxy_credentials" {
  secret_id = google_secret_manager_secret.proxy_credentials.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${var.worker_sa_email}"
}
