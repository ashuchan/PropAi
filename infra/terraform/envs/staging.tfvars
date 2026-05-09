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

# ── Email (jugnu-adhoc only) ────────────────────────────────────────────────
# Single-tenant project — staging shares the same emailer SA + DWD entry
# as prod. If a separate staging mailbox is desired, create a new
# Workspace user, add a second DWD entry against the same SA's client
# ID, and override gmail_delegated_user / report_recipients here.
gmail_emailer_sa_email = "jugnu-emailer@jugnu-494013.iam.gserviceaccount.com"
gmail_delegated_user   = "khabrilal@surgexdigital.com"
email_transport        = "gmail_api"
report_recipients      = "ashu@surgexdigital.com"
report_sender_name     = "PropAi Daily Reports (staging)"