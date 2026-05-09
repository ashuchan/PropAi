env        = "production"
project_id = "jugnu-494013"
region     = "us-central1"

vpc_self_link = "projects/jugnu-494013/global/networks/default"

deployer_sa_email = "github-deployer@jugnu-494013.iam.gserviceaccount.com"
developer_emails  = ["ashu@surgexdigital.com"] # your gcloud account email

default_task_count = 20 # bumped from 10 for 250 props/shard at the 5000-property scale
browsers_per_task  = 10
task_cpu           = "2"
task_memory        = "4Gi"
# task_count > 15 on db-f1-micro triggers connection exhaustion (validated by
# triggers/run.py:130 guard); upgrade tier to match the new parallelism.
db_tier = "db-g1-small"

# Sharded retry: ceiling on concurrent retry tasks. Operators choose
# how many to actually use at execution time via --tasks N (must be
# <= retry_task_count). 20 keeps retry parity with the daily run so
# a full 5000-property retry batch fans out at the same width.
retry_task_count = 20
retry_timeout    = "3600s"

# ── Email (jugnu-adhoc only) ────────────────────────────────────────────────
# The emailer SA must already exist in the project and be authorised in
# Workspace admin (admin.google.com → Security → API controls →
# Domain-wide delegation) with the scope:
#     https://www.googleapis.com/auth/gmail.send
# Verified end-to-end via scripts/diagnostics/dwd_smoke.py on 2026-05-09.
gmail_emailer_sa_email = "jugnu-emailer@jugnu-494013.iam.gserviceaccount.com"
gmail_delegated_user   = "khabrilal@surgexdigital.com"
email_transport        = "gmail_api"
report_recipients      = "ashu@surgexdigital.com"
report_sender_name     = "PropAi Daily Reports"