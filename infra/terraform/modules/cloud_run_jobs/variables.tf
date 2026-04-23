variable "env" { type = string }
variable "region" { type = string }
variable "repository_url" { type = string }
variable "image_tag" { type = string }
variable "worker_sa_email" { type = string }
variable "scheduler_sa_email" { type = string }
variable "vpc_connector_id" { type = string }
variable "sql_private_ip" { type = string }
variable "bucket_name" { type = string }
variable "openrouter_secret_id" { type = string }
variable "proxy_credentials_secret_id" { type = string }

# Model ids are public (openrouter.ai/models) — kept as plain strings,
# not secrets. Defaults match ma_poc/llm/openrouter.py. Override per env
# in envs/*.tfvars when you want a different model in staging vs prod.
variable "openrouter_model" {
  type        = string
  description = "OpenRouter text model id (e.g. 'google/gemini-2.5-flash')."
  default     = "tencent/hy3-preview:free"
}

variable "openrouter_vision_model" {
  type        = string
  description = "OpenRouter vision model id."
  default     = "tencent/hy3-preview:free"
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
