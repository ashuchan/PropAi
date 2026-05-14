variable "env" {
  type        = string
  description = "Deployment environment (production | staging)."
}

variable "project_id" {
  type        = string
  description = "GCP project ID — needed for the IAP service identity binding."
}

variable "region" {
  type = string
}

variable "repository_url" {
  type        = string
  description = "Artifact Registry repository URL (without trailing slash)."
}

variable "image_tag" {
  type        = string
  description = "Docker image tag pushed to the propai-frontend image."
}

variable "image_name" {
  type        = string
  default     = "propai-frontend"
  description = "Image name inside the Artifact Registry repository."
}

variable "worker_sa_email" {
  type        = string
  description = "Service account email the Cloud Run service runs as. Reused from the scrape jobs so the existing Cloud SQL IAM grants (cloudsql.client + cloudsql.instanceUser + the DB role created by alembic) apply unchanged."
}

variable "vpc_connector_id" {
  type        = string
  description = "Serverless VPC Access connector ID. Required so the service can reach Cloud SQL on its private IP."
}

variable "sql_instance_connection_name" {
  type        = string
  description = "Cloud SQL instance connection name (project:region:instance) — used by @google-cloud/cloud-sql-connector to broker the mTLS IAM-auth socket."
}

variable "schema_version" {
  type        = string
  default     = "v2"
  description = "SCHEMA_VERSION env (matches the alembic migration ID — see ma_poc/data_provider/sql)."
}

variable "log_level" {
  type    = string
  default = "info"
}

variable "min_instances" {
  type        = number
  default     = 0
  description = "Min idle instances. 0 = scale to zero (cold-start latency on first request after idle, but $0 when idle)."
}

variable "max_instances" {
  type        = number
  default     = 4
  description = "Hard ceiling on parallel instances. Each instance opens up to 20 pg connections — 4 instances × 20 = 80, comfortably under db-custom-2-7680's ~200 max_conn budget shared with the scrape fleet."
}

variable "cpu" {
  type    = string
  default = "1"
}

variable "memory" {
  type    = string
  default = "512Mi"
}

# IAP user grants are applied OUT-OF-BAND by the GitHub workflow's gcloud
# post-apply step (see modules/cloud_run_service/main.tf header for the
# provider-version rationale). The list of allowed Google identities lives
# in the workflow file's IAP_MEMBERS env var. When provider 6.x lands, this
# variable + the corresponding resource come back here.
