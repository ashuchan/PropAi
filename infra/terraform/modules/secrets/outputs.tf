output "openrouter_secret_id" {
  value = google_secret_manager_secret.openrouter_api_key.secret_id
}

output "proxy_credentials_secret_id" {
  value = google_secret_manager_secret.proxy_credentials.secret_id
}
