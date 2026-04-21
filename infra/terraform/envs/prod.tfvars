env        = "prod"
project_id = "jugnu-494013"
# image_tag supplied by CI at apply time via -var="image_tag=..."
default_task_count = 10
db_tier            = "db-f1-micro"
developer_emails   = ["ashu@surgexdigital.com"]
# vpc_self_link and deployer_sa_email supplied via CI secrets or manual apply
