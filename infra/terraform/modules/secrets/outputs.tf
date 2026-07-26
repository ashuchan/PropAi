output "openrouter_secret_id" {
  value = google_secret_manager_secret.openrouter_api_key.secret_id
}

output "anthropic_secret_id" {
  value = google_secret_manager_secret.anthropic_api_key.secret_id
  # Force consumers (cloud_run_jobs) to wait for the placeholder version
  # to land before they try to mount secret_key_ref{version="latest"}.
  # Without this, Terraform schedules the Cloud Run job update in
  # parallel with the version create and the update fails with
  # "Secret .../versions/latest was not found".
  depends_on = [null_resource.anthropic_api_key_placeholder]
}

output "proxy_credentials_secret_id" {
  value = google_secret_manager_secret.proxy_credentials.secret_id
}

output "hyperbrowser_secret_id" {
  value = google_secret_manager_secret.hyperbrowser_api_key.secret_id
  # Same ordering guard as anthropic: wait for the placeholder version so the
  # cloud_run_jobs secret_key_ref{version="latest"} mount resolves on first apply.
  depends_on = [null_resource.hyperbrowser_api_key_placeholder]
}
