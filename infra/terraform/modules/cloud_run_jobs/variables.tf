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
variable "hyperbrowser_secret_id" { type = string }

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
  type        = number
  default     = 100
  description = "parallelism / task_count for the scrape job. 100-way is the post-2026-05-11 production default; db_tier must match (see ../cloud_sql/variables.tf tier table)."
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

# Default parallelism for jugnu-retry. Setting this >1 enables sharded
# retries: Cloud Run spawns N parallel tasks, each picks a disjoint slice
# of the failure list (CLOUD_RUN_TASK_INDEX/COUNT round-robin) and writes
# to its own shard file. The merge job (runners/jugnu_retry_merge.py) consolidates
# afterwards. 1 = legacy single-task behaviour.
# Override at execution time without re-applying via:
#   gcloud run jobs execute jugnu-retry-{env} --tasks=N --parallelism=N
variable "retry_task_count" {
  type        = number
  default     = 1
  description = "Default tasks/parallelism for jugnu-retry. 1 = single shard."
}

# Wall-clock timeout per retry task. Default 1h is generous for ~100
# properties per shard. Bump if shards run more or browser launches
# trend slow.
variable "retry_timeout" {
  type        = string
  default     = "3600s"
  description = "Per-task timeout for jugnu-retry, e.g. '3600s' or '7200s'."
}

# ── Email transport (jugnu-adhoc only) ──────────────────────────────────────
#
# The adhoc job runs the four scripts/email/* entrypoints. Sending is
# routed by the EMAIL_TRANSPORT env var (read by
# scripts/email/daily.py::_email_transport):
#
#   "gmail_api" (default) — Workspace domain-wide delegation. The runtime
#       SA (worker SA) impersonates the dedicated emailer SA via
#       iamcredentials.signJwt, then trades the JWT for a user-impersonating
#       access token at oauth2.googleapis.com/token. The exchange is
#       authorised by Workspace's DWD entry on the emailer SA's client ID.
#       This is the same code path used locally — see
#       _build_gmail_api_credentials in scripts/email/daily.py.
#   "mcp" — legacy `@gongrzhe/server-gmail-autoauth-mcp` over stdio.
#       Requires Node + ~/.gmail-mcp/credentials.json baked into the
#       image. Not currently set up; kept as a switch for workstation use.
#
# Why impersonation rather than running as the emailer SA directly: the
# adhoc job needs Cloud SQL IAM auth, which requires the runtime SA to
# match the DB user (and it's already the worker SA). Switching to the
# emailer SA would break every adhoc backfill/sync/migration script.
# Keeping the worker SA as runtime + impersonating to send mail isolates
# the two capabilities — a leaked worker token can write to the DB but
# can only send mail when the explicit serviceAccountTokenCreator
# binding (managed below) lets it mint a JWT for the emailer SA.

variable "gmail_emailer_sa_email" {
  type        = string
  default     = ""
  description = "Service account email authorised in Workspace DWD with the gmail.send scope. The worker SA gets serviceAccountTokenCreator on this SA so the email scripts can mint impersonated tokens at runtime. Empty string disables the binding (email scripts then fail at send time with a clear error; everything else still works)."
}

variable "gmail_delegated_user" {
  type        = string
  default     = ""
  description = "Workspace mailbox the emailer SA impersonates (the From address). Empty string disables email; the scripts surface a clear error if invoked."
}

variable "email_transport" {
  type        = string
  default     = "gmail_api"
  description = "Which email transport scripts/email/* use: gmail_api (DWD via Gmail API) or mcp (legacy stdio Gmail MCP)."
  validation {
    condition     = contains(["gmail_api", "mcp"], var.email_transport)
    error_message = "email_transport must be one of: gmail_api, mcp"
  }
}

variable "report_recipients" {
  type        = string
  default     = ""
  description = "Comma-separated default recipient list for daily/failure/merge/refactor email reports. Per-invocation override via SCRIPT_ARGS=--recipients ..."
}

variable "report_sender_name" {
  type        = string
  default     = "PropAi Daily Reports"
  description = "Display name in the email From header."
}
