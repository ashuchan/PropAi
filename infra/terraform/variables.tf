variable "env" {
  type        = string
  description = "Deployment environment: 'staging' or 'production'"
  validation {
    condition     = contains(["staging", "production"], var.env)
    error_message = "env must be 'staging' or 'production'"
  }
}

variable "project_id" {
  type        = string
  description = "GCP project ID for this environment"
}

variable "region" {
  type        = string
  default     = "us-central1"
  description = "GCP region for all resources"
}

variable "vpc_self_link" {
  type        = string
  description = "Self-link of the VPC network (for Cloud SQL private IP)"
}

# Identities bootstrapped outside Terraform (via WIF + human bootstrap)
variable "deployer_sa_email" {
  type        = string
  description = "Service account email for the GitHub Actions deployer"
}

variable "developer_emails" {
  type        = list(string)
  default     = []
  description = "Human developer emails granted Cloud SQL IAM auth"
}

# Image knobs — image_tag is REQUIRED at apply time (supplied by CI)
variable "image_tag" {
  type        = string
  description = "Docker image tag to deploy (e.g. staging-abc1234567ef)"
}

# Parallelism and resource knobs
variable "default_task_count" {
  type    = number
  default = 5
}

variable "browsers_per_task" {
  type    = number
  default = 10
}

variable "task_cpu" {
  type    = string
  default = "2"
}

variable "task_memory" {
  type    = string
  default = "4Gi"
}

variable "db_tier" {
  type        = string
  default     = "db-f1-micro"
  description = "Cloud SQL machine tier; upgrade to db-g1-small when task_count > 15"
}

# LLM model ids passed through to the Cloud Run jobs as env vars.
# These are public identifiers (see openrouter.ai/models), not secrets.
# Override per environment in envs/*.tfvars when staging should differ
# from prod. Defaults match ma_poc/llm/openrouter.py.
variable "openrouter_model" {
  type        = string
  default     = "tencent/hy3-preview:free"
  description = "OpenRouter text model id for ma_poc.llm.openrouter."
}

variable "openrouter_vision_model" {
  type        = string
  default     = "tencent/hy3-preview:free"
  description = "OpenRouter vision model id for ma_poc.llm.openrouter."
}
