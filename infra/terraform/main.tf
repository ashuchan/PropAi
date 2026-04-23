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
  source                      = "./modules/cloud_run_jobs"
  env                         = var.env
  region                      = var.region
  repository_url              = module.artifact_registry.repository_url
  image_tag                   = var.image_tag
  worker_sa_email             = module.iam.worker_sa_email
  scheduler_sa_email          = module.iam.scheduler_sa_email
  vpc_connector_id            = module.cloud_sql.vpc_connector_id
  sql_private_ip              = module.cloud_sql.private_ip
  bucket_name                 = module.storage.bucket_name
  openrouter_secret_id        = module.secrets.openrouter_secret_id
  proxy_credentials_secret_id = module.secrets.proxy_credentials_secret_id
  openrouter_model            = var.openrouter_model
  openrouter_vision_model     = var.openrouter_vision_model
  default_task_count          = var.default_task_count
  browsers_per_task           = var.browsers_per_task
  task_cpu                    = var.task_cpu
  task_memory                 = var.task_memory
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
