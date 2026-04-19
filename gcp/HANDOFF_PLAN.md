# CI/CD Handoff Plan — Jugnu on GCP

**Audience:** Claude Code, executing across multiple sessions.
**Scope:** End-to-end CI/CD for deploying the Jugnu scraping pipeline to GCP, as specified in `Jugnu_Deployment_Architecture_GCP.docx`.
**Status:** Draft v1.0

---

## What this plan is, and what it isn't

This is a **six-handoff sequence**. Each handoff is a separate `CLAUDE_*.md` file with its own scope, gates, and non-negotiables. They are designed to be executed in order, one per Claude Code session, with a real validation step between each. Do not collapse them into a single "build the CI/CD pipeline" prompt — the surface area is too large for reliable context management, and the gates between them catch mistakes before they compound.

This is **not**:
- A Terraform tutorial
- A GCP sales pitch for a different architecture
- A plan that assumes you know Jugnu — it references the existing repo structure by path and the arch doc by section

**Inputs to this plan:**
- `Jugnu_Deployment_Architecture_GCP.docx` — the authoritative architecture spec
- The existing `ma_poc/` + `scripts/` codebase (unchanged by this plan except for additions)
- A new GCP organization with two projects (staging and prod) — see §4

**Outputs of this plan:**
- A GitHub-triggered CI/CD pipeline that deploys image + infra + migrations
- Two Cloud Run jobs (scrape + retry) operable via human- and CI-callable trigger scripts
- An operator runbook documenting every manual trigger path

---

## The six handoffs

| # | Handoff | Produces | Depends on | Approx. effort |
|---|---|---|---|---|
| 1 | `CLAUDE_DOCKERFILE.md` | Production image + `.dockerignore` + local build validation | Nothing — can start immediately | ½ session |
| 2 | `CLAUDE_TERRAFORM.md` | All GCP infra via Terraform; 2 envs; 11 gates | Handoff 1 (needs an image to reference); human bootstrap §4 | 2-3 sessions |
| 3 | `CLAUDE_TRIGGERS.md` | Operator interface: `trigger_run.py`, `trigger_retry.py`, shard + retry entry points | Handoff 2 applied to staging | 1-2 sessions |
| 4 | `CLAUDE_MIGRATIONS.md` | Alembic setup + initial schema + rollback-tested migration runner | Handoff 2 (needs SQL instance); no dependency on 3 | 1 session |
| 5 | `CLAUDE_CI.md` | PR-gate GitHub Actions workflow | Handoffs 1-4 all merged | ½ session |
| 6 | `CLAUDE_DEPLOY.md` | `deploy-staging.yml` + `deploy-prod.yml` + smoke integration | All previous | 1-2 sessions |

**Total effort estimate:** 6-10 focused Claude Code sessions, spread over roughly a week of calendar time if you serialize them and validate between each. Faster if you parallelize handoffs 3 and 4 (they're independent).

---

## Why this order

The dependency chain is physical, not stylistic:

1. **Dockerfile first** because everything downstream references an image. A half-working image caught now is a missing line in a Dockerfile; caught during handoff 3 it's a half-done Terraform apply that needs unwinding.
2. **Terraform second** because it's the biggest file, most likely to need iteration, and nothing else can be validated end-to-end without infrastructure existing.
3. **Triggers and Migrations in parallel** because they're independent — triggers talks to Cloud Run jobs, migrations talks to Cloud SQL; they don't share files.
4. **CI before Deploy** because the CI workflow establishes the gate discipline that the Deploy workflow inherits. Deploy reuses the lint/test jobs from CI; building Deploy first means either duplicating them or refactoring.
5. **Deploy last** because it's the integration point — the workflow that wires together the image build, Terraform plan, migration runner, and smoke test into a single pipeline. Everything it needs must exist first.

Each handoff's own §1 "Scope" and "Prerequisites" sections enforce this ordering at the document level.

---

## Document conventions all six handoffs share

If Claude Code notices inconsistencies between the individual handoff files, these conventions win:

- **Paths are rooted at the repo root.** `scripts/foo.py`, not `./scripts/foo.py` or `/repo/scripts/foo.py`.
- **GCP resource names follow `jugnu-{resource}-{env}` pattern.** `jugnu-scrape-staging`, `jugnu-raw-prod`, `jugnu-db-staging`.
- **`{env}` is always `staging` or `prod`.** Never `dev`, `test`, `production`, `stage`.
- **Commands assume `us-central1`.** Overridable in tfvars but the canonical region for this POC.
- **Python target is 3.11+.** Matches existing `jugnu_runner.py`.
- **Gates are binary.** Pass or fail. Partial credit is failure.
- **Every gate has a verification command.** "It works" is not a gate; `pytest tests/triggers/ --cov-fail-under=80` is.

---

## Human prerequisites (before handoff 1 starts)

These must happen once, by a human with org-level GCP access, before any handoff executes. They are not in Terraform because they're the things Terraform uses to authenticate.

### One-time per organization

1. Verify an org exists and you have permission to create projects in it
2. Identify who pays the bill (billing account linked to the projects you'll create)

### Per environment (do for staging first, validate end-to-end, then do for prod)

1. Create the GCP project: `jugnu-{env}-{unique-suffix}`. Link billing.
2. Enable APIs: `run.googleapis.com`, `sqladmin.googleapis.com`, `artifactregistry.googleapis.com`, `secretmanager.googleapis.com`, `cloudscheduler.googleapis.com`, `vpcaccess.googleapis.com`, `servicenetworking.googleapis.com`, `compute.googleapis.com`, `iamcredentials.googleapis.com`
3. Create a default VPC with private services access enabled for Cloud SQL peering (one-time network setup; Google's docs cover this in 10 minutes)
4. Create the Terraform state bucket: `gsutil mb -l us-central1 -b on gs://jugnu-tfstate-{env}` then `gsutil versioning set on gs://jugnu-tfstate-{env}`
5. Configure Workload Identity Federation: create a pool and a GitHub provider, bind to your GitHub org/repo
6. Create `github-deployer-{env}` service account, grant `roles/owner` on the project (narrow later), bind WIF principal as user
7. Add GitHub repo secrets: `GCP_PROJECT_ID_STAGING`, `GCP_PROJECT_ID_PROD`, `WIF_PROVIDER`, `WIF_SA_STAGING`, `WIF_SA_PROD`, `REGION`
8. After handoff 2 creates the secret *slots*, write secret *values*: `echo -n "$OPENROUTER_KEY" | gcloud secrets versions add openrouter-api-key-{env} --data-file=-`

**Estimated time:** 45 minutes for a first-time run on staging, 15 minutes to repeat on prod.

### Why this is not automated

Three reasons worth knowing:
- WIF bootstrap requires org-level permissions most IaC service accounts shouldn't have
- Secret values in code-reviewable files are a leak waiting to happen
- The human-in-the-loop is a deliberate speed bump — project creation is one of the few actions that's hard to reverse

Accept the cost. Document every step in `infra/README.md` as you do it, so round two (prod) is mechanical.

---

## What "done" looks like

When all six handoffs are complete, the following are true:

**Deployment:** A merge to `main` builds an image, runs migrations against staging, updates the Cloud Run jobs, runs a smoke test, and reports status in the PR checks. A tag matching `v*.*.*` triggers the same sequence against prod, gated on a human approval in GitHub Environments.

**Operation:** Any team member can trigger a manual scrape with `python scripts/trigger_run.py --env prod --target-hours 2` and get a predictable, safety-clamped execution. Retries run the same way with `trigger_retry.py`. The nightly cron continues to work untouched.

**Observability:** Every run writes structured artifacts to `gs://jugnu-raw-{env}/runs/{date}/shard_{idx}/`, retained for 30 days hot and 90 days cold. Cloud Logging captures structured logs from all workers. A failed run leaves a clear trail.

**Safety:** Secrets never touch GitHub or Terraform state. Workers have the minimum viable IAM. `terraform destroy` on prod is blocked by `deletion_protection`. Bad deploys are caught by the smoke test before they replace the last known good image.

**Recoverability:** Losing the Terraform state bucket is the only unrecoverable failure, and that bucket has versioning on. Losing any Cloud Run execution is recoverable via the retry job. Losing the database loses the day's data but not the infrastructure — a re-run next night recovers.

---

## What's explicitly out of scope

Mirroring the arch doc's "deliberately excluded" section, this plan does **not** cover:

- **Frontend or backend API deployment.** They run on laptops per the arch doc. A future handoff can add them when the POC graduates to multi-user.
- **Pub/Sub, Dataflow, or any streaming.** The pipeline is batch by design.
- **Multi-region deployment.** Single region (us-central1) is the POC commitment.
- **Automated rollback.** Manual rollback is 30 seconds (`gcloud run jobs update --image={prev-tag}`) and leaves a clearer audit trail than any automation would.
- **Cost alerting.** `gcloud billing budgets create` is a one-line operator task, not a CI/CD concern.
- **Fine-grained IAM roles for developers.** The `developer_emails` list in tfvars grants everyone the same Cloud SQL access. Role differentiation is a year-two problem.
- **Blue/green, canary, or traffic splitting.** Cloud Run *jobs* (not services) don't accept traffic. There is no concept to split.

If any of these become genuinely needed, they go in a new handoff, not bolted onto an existing one.

---

## Risk register

Three risks worth flagging to whoever sponsors this work:

| Risk | Mitigation | Residual |
|---|---|---|
| Workload Identity Federation is fiddly to set up right the first time | Budget 2 hours; have a GCP docs tab open; don't skip the `iam.workloadIdentityPoolViewer` grant | ~10% chance of 4-hour debug session on first attempt |
| Cloud SQL stop-when-idle collides with deploy-time migrations | `CLAUDE_DEPLOY.md` handles by issuing `activation_policy=ALWAYS` before migrating, documented as a non-negotiable in that handoff | Low — single choke point, tested in smoke |
| Playwright base image version drift | Pin the tag; revisit quarterly; make the version a single variable in the Dockerfile | Low — caught by smoke test on first bad upgrade |

---

## Ready-to-execute checklist

Before giving the first handoff to Claude Code, confirm:

- [ ] Arch doc read and understood by the human sponsor
- [ ] Staging GCP project exists, billing linked, APIs enabled
- [ ] VPC with private services access exists
- [ ] Terraform state bucket exists with versioning on
- [ ] Workload Identity Federation configured; GitHub repo secrets populated
- [ ] The six `CLAUDE_*.md` files are in the repo under `docs/handoffs/`
- [ ] A blank branch `infra/cicd-rollout` is ready to receive the first PR

When every box is checked, open a Claude Code session, hand it `CLAUDE_DOCKERFILE.md`, and let it work. Review the PR before moving to handoff 2. Rinse, repeat.
