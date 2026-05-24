env        = "production"
project_id = "jugnu-494013"
region     = "us-central1"

vpc_self_link = "projects/jugnu-494013/global/networks/default"

deployer_sa_email = "github-deployer@jugnu-494013.iam.gserviceaccount.com"
developer_emails  = ["ashu@surgexdigital.com"] # your gcloud account email

default_task_count = 100 # 100-way scrape: ~50 props/shard at the 5000-property scale
browsers_per_task  = 10
task_cpu           = "2"
task_memory        = "4Gi"
# Sizing rationale (2026-05-11): 100 parallel shards each open ~2 SQLAlchemy
# connections during the end-of-run sync wave, so peak DB load is ~150-200
# concurrent connections. db-g1-small (~50 max_conn) saturates well below
# this; the next dedicated-vCPU tier db-custom-2-7680 (2 vCPU / 7.5 GiB RAM,
# ~200 max_conn) gives ~2x burst headroom for ~$95/mo vs ~$25/mo on g1-small.
# Reverting to g1-small or f1-micro requires lowering default_task_count
# (see ceilings in ma_poc/scripts/_common/trigger.py).
db_tier = "db-custom-2-7680"

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

# ── Tier-escalation + BrightData / Web Unlocker (2026-05-24) ────────────────
#
# Production switches off the legacy PROXY_POOL_URLS-based inline L1
# escalation (still wired so flipping enable_tier_escalation back to false
# is a one-tfvar revert) and onto the fetch/tier_escalator.py ladder:
#
#   DIRECT → RESIDENTIAL (BrightData) → UNLOCKER (Web Unlocker REST API)
#
# DC tier deliberately left off — no BRIGHTDATA_DC_* secrets are
# provisioned, and the BrightDataProvider 2026-05-24 lazy refactor would
# raise RuntimeError on first DC dispatch. Adding DC is a future PR that
# mints brightdata-dc-zone + brightdata-dc-password secrets first, then
# flips enable_dc_proxy_tier to true.
#
# Adapter cross-origin probes (sc_probe, prospectportal_probe, ...) read
# probe_proxy_secret_id via fetch/proxy_gate.py.
# Web Unlocker is used by both pms/adapters/_probe.py (sc_probe_unlocker
# fallback) AND the L1 UnlockerProvider's api-transport mode (no
# brightdata-unlocker-zone/password secrets needed for REST).
enable_tier_escalation             = true
enable_dc_proxy_tier               = false
enable_residential_tier            = true
enable_unlocker_tier               = true
probe_proxy_secret_id              = "brightdata-probe-proxy"
web_unlocker_key_secret_id         = "web-unlocker-key-canary"
brightdata_customer_id_secret_id   = "brightdata-customer-id"
brightdata_resi_zone_secret_id     = "brightdata-resi-zone"
brightdata_resi_password_secret_id = "brightdata-resi-password"
# brightdata_dc_zone_secret_id     = ""   # leave unset until DC zone secret minted
# brightdata_dc_password_secret_id = ""   # leave unset until DC password secret minted

# ── propai-frontend (UI + API) ──────────────────────────────────────────────
# Initial tag is "bootstrap" — only used if someone runs `terraform apply`
# directly without going through .github/workflows/deploy-frontend.yml. The
# workflow always overrides this via -var="frontend_image_tag=frontend-prod-<sha>"
# after pushing a fresh image. IAP user grants live in the workflow file's
# IAP_MEMBERS env var, not here, because provider 5.x cannot express them.
frontend_image_tag = "bootstrap"