env        = "production"
project_id = "jugnu-494013"
region     = "us-central1"

vpc_self_link = "projects/jugnu-494013/global/networks/default"

deployer_sa_email = "github-deployer@jugnu-494013.iam.gserviceaccount.com"
developer_emails  = ["ashu@surgexdigital.com"] # your gcloud account email

default_task_count = 10 # smaller for staging
browsers_per_task  = 10
task_cpu           = "2"
task_memory        = "4Gi"
db_tier            = "db-f1-micro"

# Sharded retry: ceiling on concurrent retry tasks. Operators choose
# how many to actually use at execution time via --tasks N (must be
# <= retry_task_count). Set to 5 in staging so we can validate sharding
# end-to-end without burning full prod-scale resources.
retry_task_count = 5
retry_timeout    = "3600s"