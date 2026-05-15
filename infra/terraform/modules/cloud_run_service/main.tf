# ── Cloud Run service: propai-frontend ────────────────────────────────────────
#
# Single-container deploy that serves both:
#   - GET /api/*       → Express routes (ma_poc/frontend/api)
#   - GET /*           → React SPA static files (ma_poc/frontend/dist)
#
# Auth model: IAP (Identity-Aware Proxy) is enabled OUT-OF-BAND by the
# GitHub workflow's post-apply gcloud steps. The hashicorp/google provider
# pinned to 5.x in this repo predates the `iap_enabled` attribute on
# `google_cloud_run_v2_service` (added in provider 6.x). Rather than bump
# the provider — which risks breaking the scrape modules — Terraform creates
# the service in a closed-by-default posture (no allUsers binding, ingress
# allowed but unauth requests hit Cloud Run's 403 because there's no
# run.invoker for anonymous) and the workflow then runs:
#
#   gcloud beta run services update propai-frontend-<env> --iap --region=...
#   gcloud beta iap web add-iam-policy-binding --resource-type=cloud-run ...
#
# What Terraform still owns:
#   - The Cloud Run service itself (image, env, scaling, VPC)
#   - The IAP API enablement on the project
#   - The IAP service identity creation
#   - The run.invoker binding from the IAP service identity → this service
#     (so once IAP is on, IAP's forwarded requests are accepted)
#
# Migration TODO: when the time comes to land a provider 6.x bump, the
# `gcloud beta run services update --iap` step can be replaced with a
# `iap_enabled = true` attribute here, and the user grants can be moved
# back to `google_iap_web_cloud_run_service_iam_member` resources.

# Enable the IAP API. Idempotent — no-ops if already enabled.
resource "google_project_service" "iap" {
  project            = var.project_id
  service            = "iap.googleapis.com"
  disable_on_destroy = false
}

# Materialise the IAP service identity (`service-{PROJECT_NUMBER}@gcp-sa-iap...`)
# so its email is available for the run.invoker binding below. The identity is
# normally created lazily on first IAP use; doing it explicitly here makes the
# IAM binding orderable.
resource "google_project_service_identity" "iap" {
  provider = google-beta
  project  = var.project_id
  service  = "iap.googleapis.com"

  depends_on = [google_project_service.iap]
}

resource "google_cloud_run_v2_service" "propai_frontend" {
  name     = "propai-frontend-${var.env}"
  location = var.region

  ingress = "INGRESS_TRAFFIC_ALL"

  template {
    service_account = var.worker_sa_email
    timeout         = "300s"

    scaling {
      min_instance_count = var.min_instances
      max_instance_count = var.max_instances
    }

    # Private Cloud SQL IP reachability. PRIVATE_RANGES_ONLY mirrors the
    # scrape job module — it's the safest egress posture and keeps the
    # service from accidentally bypassing the VPC for outbound calls.
    vpc_access {
      connector = var.vpc_connector_id
      egress    = "PRIVATE_RANGES_ONLY"
    }

    containers {
      image = "${var.repository_url}/${var.image_name}:${var.image_tag}"

      # Cloud Run injects PORT (8080 default). The container honours it via
      # api/src/config.ts; no need to override here.
      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = var.cpu
          memory = var.memory
        }
        # cpu_idle = true lets CPU throttle to 0 between requests, which is
        # what makes scale-to-zero meaningful cost-wise.
        cpu_idle          = true
        startup_cpu_boost = true
      }

      # ── Data provider env — same wiring as the scrape jobs ──────────────
      env {
        name  = "DATA_PROVIDER"
        value = "postgres"
      }
      env {
        name  = "CLOUD_SQL_INSTANCE"
        value = var.sql_instance_connection_name
      }
      env {
        name  = "CLOUD_SQL_IP_TYPE"
        value = "PRIVATE"
      }
      env {
        name  = "CLOUD_SQL_AUTH_TYPE"
        value = "IAM"
      }
      # Host + port are stripped by the connector path (see client.ts:99);
      # only user + database are parsed out.
      #
      # The user is the SA email WITHOUT `.gserviceaccount.com` and with the
      # `@` URL-encoded as `%40`. This is the Cloud SQL IAM-auth convention:
      # `gcloud sql users create --type=cloud_iam_service_account` creates a
      # Postgres role named `jugnu-worker-production@jugnu-494013.iam`, and
      # alembic migration 0007 grants tables to that exact name. The Python
      # connector strips `.gserviceaccount.com` automatically when
      # `enable_iam_auth=True` — the Node connector does NOT, so the trim
      # must happen here. Without it, pg.Pool hands the full SA email to
      # Postgres, Postgres can't find the role, and the error surfaces as
      # `password authentication failed for user "<full-sa-email>"`.
      env {
        name  = "DATABASE_URL"
        value = "postgresql://${replace(trimsuffix(var.worker_sa_email, ".gserviceaccount.com"), "@", "%40")}@/jugnu?sslmode=require"
      }
      env {
        name  = "SCHEMA_VERSION"
        value = var.schema_version
      }
      env {
        name  = "SERVE_STATIC"
        value = "true"
      }
      env {
        name  = "STATIC_DIR"
        value = "/app/ma_poc/frontend/dist"
      }
      env {
        name  = "LOG_LEVEL"
        value = var.log_level
      }
      env {
        name  = "CORS_ORIGIN"
        value = "*"
      }
    }
  }

  # Lifecycle ignores image churn driven outside Terraform — useful when
  # operators bump the image with `gcloud run services update` between
  # full deploys.
  lifecycle {
    ignore_changes = [
      client,
      client_version,
    ]
  }

  depends_on = [google_project_service.iap]
}

# Once the GitHub workflow flips IAP on, requests are forwarded to Cloud
# Run as the IAP service identity. Without this run.invoker binding,
# IAP-authenticated requests would still 403 at Cloud Run's edge.
resource "google_cloud_run_v2_service_iam_member" "iap_invoker" {
  project  = var.project_id
  location = google_cloud_run_v2_service.propai_frontend.location
  name     = google_cloud_run_v2_service.propai_frontend.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_project_service_identity.iap.email}"
}
