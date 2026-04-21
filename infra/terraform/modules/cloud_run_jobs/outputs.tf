output "scrape_job_name" {
  value = google_cloud_run_v2_job.jugnu_scrape.name
}

output "retry_job_name" {
  value = google_cloud_run_v2_job.jugnu_retry.name
}

output "scrape_job_id" {
  value = google_cloud_run_v2_job.jugnu_scrape.id
}

output "retry_job_id" {
  value = google_cloud_run_v2_job.jugnu_retry.id
}
