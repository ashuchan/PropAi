resource "google_storage_bucket" "jugnu_raw" {
  name          = "jugnu-raw-${var.env}"
  location      = var.region
  storage_class = "STANDARD"

  # Staging can be destroyed; prod is protected
  force_destroy = var.env == "staging"

  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  versioning {
    enabled = true
  }

  # Zone 1: runs/ → NEARLINE at 7 days
  lifecycle_rule {
    condition {
      age            = 7
      matches_prefix = ["runs/"]
    }
    action {
      type          = "SetStorageClass"
      storage_class = "NEARLINE"
    }
  }

  # Zone 2: runs/ → COLDLINE at 30 days
  lifecycle_rule {
    condition {
      age            = 30
      matches_prefix = ["runs/"]
    }
    action {
      type          = "SetStorageClass"
      storage_class = "COLDLINE"
    }
  }

  # Zone 3: runs/ → deleted at 90 days
  lifecycle_rule {
    condition {
      age            = 90
      matches_prefix = ["runs/"]
    }
    action {
      type = "Delete"
    }
  }

  # Prune old non-current versions of property-list/
  lifecycle_rule {
    condition {
      num_newer_versions = 3
      matches_prefix     = ["property-list/"]
    }
    action {
      type = "Delete"
    }
  }
}
