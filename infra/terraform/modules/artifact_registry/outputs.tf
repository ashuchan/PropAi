output "repository_url" {
  description = "Full repository URL for use in Docker tags and Cloud Run image references"
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/jugnu-images"
}

output "repository_name" {
  value = google_artifact_registry_repository.jugnu_images.name
}
