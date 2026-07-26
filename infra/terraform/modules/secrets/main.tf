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
# immediately.
#
# Why not ``google_secret_manager_secret_version``? The provider does a
# read-after-create on that resource, which requires
# ``secretmanager.versions.access``. The deployer SA in this project has
# ``versions.add`` but not ``versions.access`` (intentional — deployer
# can rotate infra without ever reading payloads), so the managed
# resource fails on first apply. Going through ``gcloud`` keeps the
# privilege boundary intact.
#
# Idempotency: ``null_resource.triggers`` only fires when the underlying
# secret is (re)created, so re-running ``terraform apply`` after the
# operator has written a real version will NOT re-add the placeholder.
resource "null_resource" "anthropic_api_key_placeholder" {
  triggers = {
    secret_name = google_secret_manager_secret.anthropic_api_key.name
  }
  provisioner "local-exec" {
    interpreter = ["bash", "-c"]
    command     = <<-EOT
      set -euo pipefail
      printf 'PLACEHOLDER_REPLACE_VIA_GCLOUD' | \
        gcloud secrets versions add ${google_secret_manager_secret.anthropic_api_key.secret_id} \
          --project=${google_secret_manager_secret.anthropic_api_key.project} \
          --data-file=-
    EOT
  }
}

resource "google_secret_manager_secret" "proxy_credentials" {
  secret_id = "proxy-credentials-${var.env}"
  replication {
    auto {}
  }
}

# Hyperbrowser cloud-browser API key (task #46). Consumed by the switchable
# fetch backend (ma_poc/fetch/hyperbrowser_backend.py) ONLY when the job is
# run with FETCH_BACKEND=hyperbrowser; the key env is wired unconditionally, so
# the placeholder below is required or every execution (incl. the default
# BrightData path) would fail to resolve ``latest``. Operator overwrites with
# the real token out-of-band:
#   echo -n "$HB_KEY" | gcloud secrets versions add hyperbrowser-api-key-${var.env} --data-file=-
resource "google_secret_manager_secret" "hyperbrowser_api_key" {
  secret_id = "hyperbrowser-api-key-${var.env}"
  replication {
    auto {}
  }
}

resource "null_resource" "hyperbrowser_api_key_placeholder" {
  triggers = {
    secret_name = google_secret_manager_secret.hyperbrowser_api_key.name
  }
  provisioner "local-exec" {
    interpreter = ["bash", "-c"]
    command     = <<-EOT
      set -euo pipefail
      printf 'PLACEHOLDER_REPLACE_VIA_GCLOUD' | \
        gcloud secrets versions add ${google_secret_manager_secret.hyperbrowser_api_key.secret_id} \
          --project=${google_secret_manager_secret.hyperbrowser_api_key.project} \
          --data-file=-
    EOT
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

resource "google_secret_manager_secret_iam_member" "worker_hyperbrowser" {
  secret_id = google_secret_manager_secret.hyperbrowser_api_key.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${var.worker_sa_email}"
}
