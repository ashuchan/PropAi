resource "google_artifact_registry_repository" "jugnu_images" {
  location      = var.region
  repository_id = "jugnu-images"
  description   = "Docker images for the Jugnu scraping pipeline"
  format        = "DOCKER"
}

resource "google_artifact_registry_repository_iam_member" "deployer_writer" {
  location   = google_artifact_registry_repository.jugnu_images.location
  repository = google_artifact_registry_repository.jugnu_images.name
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${var.deployer_sa}"
}
