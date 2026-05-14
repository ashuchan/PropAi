output "service_name" {
  value       = google_cloud_run_v2_service.propai_frontend.name
  description = "Name of the deployed Cloud Run service."
}

output "service_url" {
  value       = google_cloud_run_v2_service.propai_frontend.uri
  description = "Auto-generated *.run.app URL. Hit this from a browser; IAP gates access."
}

output "service_location" {
  value = google_cloud_run_v2_service.propai_frontend.location
}
