env        = "staging"
project_id = "jugnu-staging-<unique>"
# image_tag supplied by CI at apply time via -var="image_tag=..."
default_task_count = 3
developer_emails   = ["you@company.com"]
# vpc_self_link and deployer_sa_email supplied via CI secrets or manual apply
