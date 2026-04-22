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