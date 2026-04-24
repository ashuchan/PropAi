variable "env" { type = string }
variable "region" { type = string }
variable "repository_url" { type = string }
variable "image_tag" { type = string }
variable "worker_sa_email" { type = string }
variable "scheduler_sa_email" { type = string }
variable "vpc_connector_id" { type = string }
variable "sql_private_ip" { type = string }

# Full instance id in ``project:region:instance`` form (from
# ``module.cloud_sql.instance_connection_name``). Read by
# ``data_provider/sql/engine.py:_make_cloud_sql_engine`` so Cloud Run
# tasks authenticate via the Cloud SQL Python Connector (IAM OAuth
# tokens). Required because ``cloudsql.iam_authentication=on`` rejects
# password auth outright — a bare ``postgresql://user@host`` URL gets
# "no password supplied" and nothing lands in Postgres.
variable "sql_instance_connection_name" { type = string }
variable "bucket_name" { type = string }
variable "openrouter_secret_id" { type = string }
variable "proxy_credentials_secret_id" { type = string }

# Model ids are public (openrouter.ai/models) — kept as plain strings,
# not secrets. Defaults match ma_poc/llm/openrouter.py. Override per env
# in envs/*.tfvars when you want a different model in staging vs prod.
variable "openrouter_model" {
  type        = string
  description = "OpenRouter text model id (e.g. 'google/gemini-2.5-flash')."
  default     = "qwen/qwen3-235b-a22b-2507"
}

variable "openrouter_vision_model" {
  type        = string
  description = "OpenRouter vision model id."
  default     = "qwen/qwen3-235b-a22b-2507"
}
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
