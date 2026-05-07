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
db_tier            = "db-g1-small"

# Sharded retry: ceiling on concurrent retry tasks. Operators choose
# how many to actually use at execution time via --tasks N (must be
# <= retry_task_count). 20 keeps retry parity with the daily run so
# a full 5000-property retry batch fans out at the same width.
retry_task_count = 20
retry_timeout    = "3600s"