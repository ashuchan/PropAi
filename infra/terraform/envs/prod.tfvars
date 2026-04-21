env        = "prod"
project_id = "jugnu-prod-<unique>"
# image_tag supplied by CI at apply time via -var="image_tag=..."
default_task_count = 5
db_tier            = "db-f1-micro"
developer_emails   = ["you@company.com", "teammate@company.com"]
# vpc_self_link and deployer_sa_email supplied via CI secrets or manual apply
