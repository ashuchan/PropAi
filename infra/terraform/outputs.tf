output "repository_url" {
  description = "Artifact Registry repository URL for Docker images"
  value       = module.artifact_registry.repository_url
}

output "bucket_name" {
  description = "GCS bucket name for scrape artifacts"
  value       = module.storage.bucket_name
}

output "bucket_url" {
  description = "GCS bucket URL (gs://...)"
  value       = module.storage.bucket_url
}

output "worker_sa_email" {
  description = "Service account email for Cloud Run workers"
  value       = module.iam.worker_sa_email
}

output "scheduler_sa_email" {
  description = "Service account email for Cloud Scheduler"
  value       = module.iam.scheduler_sa_email
}

output "sql_instance_connection_name" {
  description = "Cloud SQL instance connection name (project:region:instance)"
  value       = module.cloud_sql.instance_connection_name
}

output "sql_private_ip" {
  description = "Cloud SQL instance private IP address"
  value       = module.cloud_sql.private_ip
}

output "scrape_job_name" {
  description = "Cloud Run scrape job name"
  value       = module.cloud_run_jobs.scrape_job_name
}

output "retry_job_name" {
  description = "Cloud Run retry job name"
  value       = module.cloud_run_jobs.retry_job_name
}

output "adhoc_job_name" {
  description = "Cloud Run on-demand script-runner job name. Execute from the console with SCRIPT_NAME and SCRIPT_ARGS overrides."
  value       = module.cloud_run_jobs.adhoc_job_name
}

output "scheduler_job_names" {
  description = "Cloud Scheduler job names"
  value       = module.scheduler.scheduler_job_names
}

output "frontend_service_url" {
  description = "Public *.run.app URL for the propai-frontend service. Open in a browser; IAP gates access via Google login."
  value       = module.cloud_run_service.service_url
}

output "frontend_service_name" {
  description = "Cloud Run service name (e.g. propai-frontend-production). Used by gcloud to push image updates outside Terraform."
  value       = module.cloud_run_service.service_name
}
