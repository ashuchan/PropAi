output "scheduler_job_names" {
  value = [
    google_cloud_scheduler_job.sql_start.name,
    google_cloud_scheduler_job.daily_scrape.name,
    google_cloud_scheduler_job.sql_stop.name,
    google_cloud_scheduler_job.daily_retry.name,
  ]
}
