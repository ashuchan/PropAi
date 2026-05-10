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

# Parallelism and resource knobs.
# 100 is the post-2026-05-11 production default — 50 props/shard at the
# 5000-property scale. Staging overrides this to 10 in envs/staging.tfvars
# (cost), prod inherits the default. Bumping above 100 requires the db_tier
# to move from db-custom-2-7680 to db-custom-4-15360 (see tier table in
# infra/terraform/README.md).
variable "default_task_count" {
  type    = number
  default = 100
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

# Sharded retry knobs — passed through to module.cloud_run_jobs.
# parallelism on jugnu-retry-{env} is set to retry_task_count, so the
# operator can run up to that many shards concurrently via
# `gcloud run jobs execute --tasks=N`. 1 = legacy single-task retry.
variable "retry_task_count" {
  type        = number
  default     = 1
  description = "Default tasks/parallelism for jugnu-retry. 1 = single shard."
}

variable "retry_timeout" {
  type        = string
  default     = "3600s"
  description = "Per-task timeout for jugnu-retry, e.g. '3600s' or '7200s'."
}

variable "db_tier" {
  type        = string
  default     = "db-custom-2-7680"
  description = <<EOT
Cloud SQL machine tier. Pick from the parallelism table below — connection
ceilings are Cloud SQL's automatic max_connections (scales with memory):

  db-f1-micro       shared / 0.6 GiB / ~25 max_conn  → safe up to  10 tasks
  db-g1-small       shared / 1.7 GiB / ~50 max_conn  → safe up to  20 tasks
  db-custom-1-3840  1 vCPU / 3.75 GiB / ~100 max_conn → safe up to  50 tasks
  db-custom-2-7680  2 vCPU / 7.5 GiB / ~200 max_conn  → safe up to 100 tasks (current prod default)
  db-custom-4-15360 4 vCPU / 15 GiB  / ~400 max_conn  → safe up to 200 tasks

The end-of-run sync_run_to_pg wave is the connection-pressure event;
each shard opens ~2 SQLAlchemy connections during it.
EOT
}

# LLM provider + model ids passed through to the Cloud Run jobs as env
# vars. These are public identifiers, not secrets — the API keys live
# in Secret Manager and are mounted separately (see modules/secrets).
# Override per environment in envs/*.tfvars.
variable "llm_provider" {
  type        = string
  default     = "openrouter"
  description = "LLM provider for ma_poc.llm.factory: anthropic | openrouter | azure."
  validation {
    condition     = contains(["anthropic", "openrouter", "azure"], var.llm_provider)
    error_message = "llm_provider must be one of: anthropic, openrouter, azure"
  }
}

variable "openrouter_model" {
  type        = string
  default     = "qwen/qwen3-235b-a22b-2507"
  description = "OpenRouter text model id for ma_poc.llm.openrouter."
}

variable "openrouter_vision_model" {
  type        = string
  default     = "qwen/qwen3-235b-a22b-2507"
  description = "OpenRouter vision model id for ma_poc.llm.openrouter."
}

# Claude Haiku 4.5 is currently the cheapest Anthropic model and supports
# vision, so both defaults use it. Override vision to a Sonnet model in
# envs/*.tfvars if you need higher screenshot accuracy.
variable "anthropic_model" {
  type        = string
  default     = "claude-haiku-4-5-20251001"
  description = "Anthropic text model id for ma_poc.llm.anthropic."
}

variable "anthropic_vision_model" {
  type        = string
  default     = "claude-haiku-4-5-20251001"
  description = "Anthropic vision model id for ma_poc.llm.anthropic."
}

# ── Email transport for the jugnu-adhoc job ─────────────────────────────────
# The four scripts/email/* entrypoints route through one of two transports
# (see scripts/email/daily.py::_email_transport):
#   - gmail_api (default): Workspace DWD via the Gmail API. Worker SA
#     impersonates gmail_emailer_sa_email and sends as gmail_delegated_user.
#     Requires a one-time DWD entry in admin.google.com (see CLAUDE_ADHOC_RUNNER.md).
#   - mcp: legacy `@gongrzhe/server-gmail-autoauth-mcp`. Workstation use only;
#     the prod image does not bake Node + MCP creds.
# All values default to empty/zero — the email scripts then fail at send
# time with a clear error, but the rest of the adhoc job still works.

variable "gmail_emailer_sa_email" {
  type        = string
  default     = ""
  description = "Service account authorised in Workspace DWD with the gmail.send scope. Worker SA gets serviceAccountTokenCreator on this SA."
}

variable "gmail_delegated_user" {
  type        = string
  default     = ""
  description = "Workspace mailbox the emailer SA impersonates (the From address)."
}

variable "email_transport" {
  type        = string
  default     = "gmail_api"
  description = "Email transport for scripts/email/*: gmail_api (DWD) or mcp (legacy stdio)."
  validation {
    condition     = contains(["gmail_api", "mcp"], var.email_transport)
    error_message = "email_transport must be one of: gmail_api, mcp"
  }
}

variable "report_recipients" {
  type        = string
  default     = ""
  description = "Comma-separated default recipient list. Per-invocation override via SCRIPT_ARGS=--recipients ..."
}

variable "report_sender_name" {
  type        = string
  default     = "PropAi Daily Reports"
  description = "Display name in the email From header."
}
