resource "google_sql_database_instance" "jugnu_db" {
  name             = "jugnu-db-${var.env}"
  database_version = "POSTGRES_15"
  region           = var.region

  # Prevent accidental destruction in production
  deletion_protection = var.env == "prod"

  settings {
    tier              = var.db_tier
    availability_type = "ZONAL" # no HA — arch doc §7

    backup_configuration {
      enabled    = true
      start_time = "03:30"
    }

    ip_configuration {
      ipv4_enabled    = false
      private_network = var.vpc_self_link
    }

    database_flags {
      name  = "cloudsql.iam_authentication"
      value = "on"
    }

    # Activation policy set to ALWAYS as the initial state.
    # Cloud Scheduler overrides this to NEVER after the daily run window.
    activation_policy = "ALWAYS"
  }
}

resource "google_sql_database" "jugnu" {
  name     = "jugnu"
  instance = google_sql_database_instance.jugnu_db.name
}

# Human developer IAM users
resource "google_sql_user" "developers" {
  for_each = toset(var.developer_emails)

  name     = each.key
  instance = google_sql_database_instance.jugnu_db.name
  type     = "CLOUD_IAM_USER"
}

# Worker service account IAM user
resource "google_sql_user" "worker_sa" {
  # Cloud SQL IAM SA users use the email without the @project.iam.gserviceaccount.com suffix
  name     = trimsuffix(var.worker_sa_email, ".gserviceaccount.com")
  instance = google_sql_database_instance.jugnu_db.name
  type     = "CLOUD_IAM_SERVICE_ACCOUNT"
}

# VPC access connector for Cloud Run → Cloud SQL private IP
resource "google_vpc_access_connector" "jugnu-con" {
  name          = "jugnu-con-${var.env}"
  region        = var.region
  network       = var.vpc_self_link
  machine_type  = "e2-micro"
  min_instances = 2
  max_instances = 3
  ip_cidr_range = "10.8.0.0/28" # must not overlap existing subnets
}
