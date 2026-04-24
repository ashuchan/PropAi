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
# A previous (failed) apply left a managed
# ``google_secret_manager_secret_version.anthropic_api_key_placeholder``
# in tfstate even though its post-create read returned 403. Every
# subsequent ``terraform plan`` then re-issues that read during refresh
# and fails the same way, blocking all infra changes. This block drops
# the resource from state without destroying the actual version
# (``destroy = false``), so the existing Secret Manager version stays
# intact and Cloud Run keeps mounting ``latest``. Safe to leave in place
# permanently — once state is clean it's a no-op.
removed {
  from = google_secret_manager_secret_version.anthropic_api_key_placeholder
  lifecycle {
    destroy = false
  }
}

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
