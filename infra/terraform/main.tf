module "artifact_registry" {
  source      = "./modules/artifact_registry"
  env         = var.env
  project_id  = var.project_id
  region      = var.region
  deployer_sa = var.deployer_sa_email
}

module "iam" {
  source     = "./modules/iam"
  env        = var.env
  project_id = var.project_id
}

module "storage" {
  source = "./modules/storage"
  env    = var.env
  region = var.region
}

module "cloud_sql" {
  source           = "./modules/cloud_sql"
  env              = var.env
  region           = var.region
  db_tier          = var.db_tier
  vpc_self_link    = var.vpc_self_link
  developer_emails = var.developer_emails
  worker_sa_email  = module.iam.worker_sa_email
}

module "secrets" {
  source          = "./modules/secrets"
  env             = var.env
  worker_sa_email = module.iam.worker_sa_email
}

module "cloud_run_jobs" {
  source                       = "./modules/cloud_run_jobs"
  env                          = var.env
  region                       = var.region
  repository_url               = module.artifact_registry.repository_url
  image_tag                    = var.image_tag
  worker_sa_email              = module.iam.worker_sa_email
  scheduler_sa_email           = module.iam.scheduler_sa_email
  vpc_connector_id             = module.cloud_sql.vpc_connector_id
  sql_private_ip               = module.cloud_sql.private_ip
  sql_instance_connection_name = module.cloud_sql.instance_connection_name
  bucket_name                  = module.storage.bucket_name
  openrouter_secret_id         = module.secrets.openrouter_secret_id
  anthropic_secret_id          = module.secrets.anthropic_secret_id
  proxy_credentials_secret_id  = module.secrets.proxy_credentials_secret_id
  llm_provider                 = var.llm_provider
  openrouter_model             = var.openrouter_model
  openrouter_vision_model      = var.openrouter_vision_model
  anthropic_model              = var.anthropic_model
  anthropic_vision_model       = var.anthropic_vision_model
  default_task_count           = var.default_task_count
  browsers_per_task            = var.browsers_per_task
  task_cpu                     = var.task_cpu
  task_memory                  = var.task_memory
  retry_task_count             = var.retry_task_count
  retry_timeout                = var.retry_timeout
  gmail_emailer_sa_email       = var.gmail_emailer_sa_email
  gmail_delegated_user         = var.gmail_delegated_user
  email_transport              = var.email_transport
  report_recipients            = var.report_recipients
  report_sender_name           = var.report_sender_name
}

module "scheduler" {
  source                 = "./modules/scheduler"
  env                    = var.env
  region                 = var.region
  scheduler_sa_email     = module.iam.scheduler_sa_email
  scrape_job_id          = module.cloud_run_jobs.scrape_job_id
  retry_job_id           = module.cloud_run_jobs.retry_job_id
  sql_instance_name      = module.cloud_sql.instance_name
  project_id             = var.project_id
  retry_scheduler_paused = true # disabled by default; enable when auto-retry is ready
}

# ── propai-frontend Cloud Run service ────────────────────────────────────────
# Public UI + API. IAP gates access; only emails in iap_member_emails can hit
# it. Image is built+pushed separately from the jugnu scraper image — see
# docs/DEPLOY_FRONTEND.md.
#
# TODO(staging): only wired into production today. To enable for staging,
# create envs/staging.tfvars values for iap_member_emails and (optionally)
# frontend_image_tag, then this module call will fan out to both envs.
module "cloud_run_service" {
  source = "./modules/cloud_run_service"

  env                          = var.env
  project_id                   = var.project_id
  region                       = var.region
  repository_url               = module.artifact_registry.repository_url
  image_tag                    = var.frontend_image_tag
  worker_sa_email              = module.iam.worker_sa_email
  vpc_connector_id             = module.cloud_sql.vpc_connector_id
  sql_instance_connection_name = module.cloud_sql.instance_connection_name
  # IAP user grants are applied by the GitHub workflow's post-apply gcloud
  # step (see .github/workflows/deploy-frontend.yml). Provider 5.x lacks
  # the IAP resource family for Cloud Run; this is the documented workaround.
}
