output "worker_sa_email" {
  value = google_service_account.worker.email
}

output "scheduler_sa_email" {
  value = google_service_account.scheduler.email
}

output "cleanup_sa_email" {
  value = google_service_account.cleanup.email
}
