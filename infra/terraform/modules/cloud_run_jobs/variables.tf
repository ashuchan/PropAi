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
variable "anthropic_secret_id" { type = string }
variable "proxy_credentials_secret_id" { type = string }

# Selects which provider ma_poc/llm/factory.py instantiates at runtime.
# Valid values: "anthropic", "openrouter", "azure". Default is "anthropic"
# (cheapest with Haiku 4.5 and matches factory.py's own default).
variable "llm_provider" {
  type        = string
  description = "LLM provider for ma_poc.llm.factory: anthropic | openrouter | azure."
  default     = "anthropic"
  validation {
    condition     = contains(["anthropic", "openrouter", "azure"], var.llm_provider)
    error_message = "llm_provider must be one of: anthropic, openrouter, azure"
  }
}

# Model ids are public — kept as plain strings, not secrets. Defaults
# match ma_poc/llm/openrouter.py and ma_poc/llm/anthropic.py. Override
# per env in envs/*.tfvars when you want a different model in staging
# vs prod.
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

# Haiku 4.5 is the cheapest current Anthropic model and supports vision,
# so it's the default for both text and vision. Upgrade vision to Sonnet
# in envs/*.tfvars if you need higher accuracy on screenshot extraction.
variable "anthropic_model" {
  type        = string
  description = "Anthropic text model id."
  default     = "claude-haiku-4-5-20251001"
}

variable "anthropic_vision_model" {
  type        = string
  description = "Anthropic vision model id."
  default     = "claude-haiku-4-5-20251001"
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
