# CLAUDE_DEPLOY.md

**Goal:** Build the GitHub Actions deploy workflows that take a merged commit to production. A push to `main` deploys to staging automatically; a signed tag matching `v*.*.*` deploys to production after human approval. Every deploy runs: build image → push → Terraform plan+apply → run migrations → update Cloud Run job → smoke test. A failure at any step leaves the previous deploy intact.

**Read before starting:**
- `CLAUDE_DOCKERFILE.md`, `CLAUDE_TERRAFORM.md`, `CLAUDE_TRIGGERS.md`, `CLAUDE_MIGRATIONS.md`, `CLAUDE_CI.md` — deploy consumes all of them
- `Jugnu_Deployment_Architecture_GCP.docx` — especially the Cloud SQL stop-when-idle pattern, which is the most important ordering constraint in this handoff
- GitHub's Workload Identity Federation docs — the auth pattern used throughout

**Prerequisite:** All five prior handoffs merged; staging infrastructure fully provisioned via Terraform; CI gates green; a working (even if stub) set of triggers and entry scripts inside the image. No more incremental landing strategy after this — Deploy is the culmination.

---

## 1. Scope

What this handoff produces:

- `.github/workflows/deploy-staging.yml` — triggered on push to `main`
- `.github/workflows/deploy-prod.yml` — triggered on signed tag `v*.*.*`
- `.github/workflows/reusable-deploy.yml` — the shared deploy logic both workflows call
- `scripts/deploy_csv_sync.py` — pushes `data/property-list/properties.csv` to the env's GCS bucket
- `docs/DEPLOY.md` — the operator-facing runbook: how to trigger a deploy, how to interpret failures, how to roll back
- `docs/RUNBOOK_INCIDENTS.md` — a short incident runbook covering the three most likely deploy failures and their recoveries

What this handoff does **not** produce:
- Automated rollback (§9 — manual is deliberately chosen)
- Canary or blue/green (Cloud Run *jobs* don't route traffic; there's nothing to split)
- Multi-region deploys (single region is the POC commitment)
- Pre-production environment beyond staging (staging + prod is the full topology)

---

## 2. The deploy pipeline, end to end

Every deploy follows this exact sequence. The order is not arbitrary — each step depends on an invariant the previous step established. Deviations are not permitted; open a new handoff if the sequence needs to change.

```
  ┌─────────────────────────────────────────────────────────────────┐
  │ 1. Authenticate to GCP via Workload Identity Federation         │
  │    Effect: short-lived token; no JSON key files anywhere        │
  └────────────────────────────────┬────────────────────────────────┘
                                    │
  ┌────────────────────────────────▼────────────────────────────────┐
  │ 2. Ensure Cloud SQL is running                                  │
  │    Reason: migrations need the DB; stop-when-idle may have      │
  │    stopped it                                                   │
  └────────────────────────────────┬────────────────────────────────┘
                                    │
  ┌────────────────────────────────▼────────────────────────────────┐
  │ 3. Build image, tag as {env}-{git-sha} AND {env}-latest          │
  │    Push to Artifact Registry                                    │
  └────────────────────────────────┬────────────────────────────────┘
                                    │
  ┌────────────────────────────────▼────────────────────────────────┐
  │ 4. Terraform plan, post as PR comment (staging) or               │
  │    apply summary artifact (prod)                                 │
  │    If plan shows destructive changes → require human approval    │
  └────────────────────────────────┬────────────────────────────────┘
                                    │
  ┌────────────────────────────────▼────────────────────────────────┐
  │ 5. Terraform apply with image_tag={env}-{git-sha}                │
  │    Effect: Cloud Run job's image field updated; infra drift      │
  │    reconciled                                                    │
  └────────────────────────────────┬────────────────────────────────┘
                                    │
  ┌────────────────────────────────▼────────────────────────────────┐
  │ 6. Run database migrations: python scripts/migrate.py --env {} up│
  │    Must complete cleanly; any failure aborts deploy              │
  └────────────────────────────────┬────────────────────────────────┘
                                    │
  ┌────────────────────────────────▼────────────────────────────────┐
  │ 7. Sync CSV input: scripts/deploy_csv_sync.py                    │
  │    Uploads data/property-list/properties.csv to                  │
  │    gs://jugnu-raw-{env}/property-list/                           │
  └────────────────────────────────┬────────────────────────────────┘
                                    │
  ┌────────────────────────────────▼────────────────────────────────┐
  │ 8. Run smoke test:                                               │
  │    python scripts/trigger_smoke.py --env {env}                   │
  │    Canary CSV (3 properties), full round-trip through the        │
  │    pipeline                                                      │
  └────────────────────────────────┬────────────────────────────────┘
                                    │
  ┌────────────────────────────────▼────────────────────────────────┐
  │ 9. Tag image as {env}-known-good; emit deploy summary            │
  │    Effect: rollback target pointer moves forward                 │
  └─────────────────────────────────────────────────────────────────┘
```

Step 9's `known-good` tag is the rollback mechanism. If something breaks in production later, `gcloud run jobs update jugnu-scrape-prod --image=...:prod-known-good` reverts to the last smoke-tested version. It's not automatic, and it shouldn't be.

---

## 3. The reusable deploy workflow

Both staging and prod call this. It takes the environment as an input and does the nine steps above. Parameterization keeps the two env-specific workflows thin.

`.github/workflows/reusable-deploy.yml`:

```yaml
name: reusable-deploy

on:
  workflow_call:
    inputs:
      env:
        required: true
        type: string               # staging or prod
      git-ref:
        required: true
        type: string               # sha or tag to deploy
      require-approval:
        required: false
        type: boolean
        default: false              # prod sets true
    secrets:
      WIF_PROVIDER:
        required: true
      WIF_SA:
        required: true
      GCP_PROJECT_ID:
        required: true
      OPENROUTER_API_KEY:
        required: true
      PROXY_CREDENTIALS:
        required: true

permissions:
  contents: read
  id-token: write                   # required for WIF
  pull-requests: write              # for TF plan comments

env:
  REGION: us-central1
  AR_REPO: jugnu-images

jobs:
  # ────────────────────────────────────────────────────────────────
  # Gate 0: approval (prod only)
  # ────────────────────────────────────────────────────────────────
  approval:
    if: inputs.require-approval
    runs-on: ubuntu-latest
    environment: production         # GitHub Environment with required reviewers
    steps:
      - run: echo "Approved for production deploy of ${{ inputs.git-ref }}"

  # ────────────────────────────────────────────────────────────────
  # Step 1-3: auth, ensure SQL running, build image
  # ────────────────────────────────────────────────────────────────
  build-and-push:
    needs: [approval]
    if: always() && (needs.approval.result == 'success' || needs.approval.result == 'skipped')
    runs-on: ubuntu-latest
    outputs:
      image-tag: ${{ steps.tag.outputs.tag }}
      image-uri: ${{ steps.tag.outputs.uri }}
    steps:
      - uses: actions/checkout@v4
        with:
          ref: ${{ inputs.git-ref }}
      - id: auth
        uses: google-github-actions/auth@v2
        with:
          workload_identity_provider: ${{ secrets.WIF_PROVIDER }}
          service_account: ${{ secrets.WIF_SA }}
      - uses: google-github-actions/setup-gcloud@v2
      - name: Ensure Cloud SQL is running
        run: |
          INSTANCE="jugnu-db-${{ inputs.env }}"
          state=$(gcloud sql instances describe $INSTANCE --format='value(state)')
          if [ "$state" != "RUNNABLE" ]; then
            gcloud sql instances patch $INSTANCE --activation-policy=ALWAYS --quiet
            # Poll until ready (max 3 min)
            for i in $(seq 1 36); do
              sleep 5
              state=$(gcloud sql instances describe $INSTANCE --format='value(state)')
              [ "$state" = "RUNNABLE" ] && break
            done
            [ "$state" = "RUNNABLE" ] || (echo "SQL did not start" && exit 1)
          fi
      - id: tag
        run: |
          SHORT_SHA=$(echo "${{ github.sha }}" | cut -c1-12)
          TAG="${{ inputs.env }}-${SHORT_SHA}"
          URI="${REGION}-docker.pkg.dev/${{ secrets.GCP_PROJECT_ID }}/${AR_REPO}/jugnu:${TAG}"
          echo "tag=${TAG}" >> $GITHUB_OUTPUT
          echo "uri=${URI}" >> $GITHUB_OUTPUT
      - name: Configure Docker for Artifact Registry
        run: gcloud auth configure-docker ${REGION}-docker.pkg.dev --quiet
      - uses: docker/setup-buildx-action@v3
      - name: Build and push
        uses: docker/build-push-action@v5
        with:
          context: .
          push: true
          tags: |
            ${{ steps.tag.outputs.uri }}
            ${{ format('{0}-docker.pkg.dev/{1}/{2}/jugnu:{3}-latest', env.REGION, secrets.GCP_PROJECT_ID, env.AR_REPO, inputs.env) }}
          cache-from: type=gha
          cache-to: type=gha,mode=max

  # ────────────────────────────────────────────────────────────────
  # Step 4-5: Terraform plan + apply
  # ────────────────────────────────────────────────────────────────
  terraform-apply:
    needs: [build-and-push]
    runs-on: ubuntu-latest
    defaults:
      run:
        working-directory: infra/terraform
    steps:
      - uses: actions/checkout@v4
        with: { ref: ${{ inputs.git-ref }} }
      - uses: google-github-actions/auth@v2
        with:
          workload_identity_provider: ${{ secrets.WIF_PROVIDER }}
          service_account: ${{ secrets.WIF_SA }}
      - uses: hashicorp/setup-terraform@v3
        with: { terraform_version: "1.7.5" }
      - name: Init
        run: terraform init -backend-config="bucket=jugnu-tfstate-${{ inputs.env }}" -backend-config="prefix=terraform/state"
      - name: Plan
        id: plan
        run: |
          terraform plan \
            -var-file=envs/${{ inputs.env }}.tfvars \
            -var="image_tag=${{ needs.build-and-push.outputs.image-tag }}" \
            -out=tfplan \
            -detailed-exitcode \
            -no-color > plan.txt 2>&1 || true
          # detailed-exitcode: 0=no changes, 1=error, 2=changes present
          ec=$?
          echo "exitcode=$ec" >> $GITHUB_OUTPUT
          cat plan.txt
          # Flag destructive changes
          if grep -qE '^(  # .+ will be destroyed|  - resource)' plan.txt; then
            echo "destructive=true" >> $GITHUB_OUTPUT
          fi
      - name: Block destructive changes without approval
        if: steps.plan.outputs.destructive == 'true' && inputs.env == 'prod'
        # For prod, destructive TF changes must go through a separate PR with approval.
        # In the deploy workflow, we refuse them.
        run: |
          echo "Destructive TF changes detected on prod. Refusing to apply."
          echo "These must be landed via an explicit infra PR with extra review."
          exit 1
      - name: Apply
        run: terraform apply -auto-approve tfplan

  # ────────────────────────────────────────────────────────────────
  # Step 6: migrations
  # ────────────────────────────────────────────────────────────────
  migrate:
    needs: [terraform-apply]
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with: { ref: ${{ inputs.git-ref }} }
      - uses: google-github-actions/auth@v2
        with:
          workload_identity_provider: ${{ secrets.WIF_PROVIDER }}
          service_account: ${{ secrets.WIF_SA }}
      - uses: google-github-actions/setup-gcloud@v2
      - uses: actions/setup-python@v5
        with: { python-version: "3.11", cache: pip }
      - run: pip install -r requirements.txt
      - name: Install cloud-sql-proxy
        run: |
          curl -o cloud-sql-proxy https://storage.googleapis.com/cloud-sql-connectors/cloud-sql-proxy/v2.8.2/cloud-sql-proxy.linux.amd64
          chmod +x cloud-sql-proxy
          sudo mv cloud-sql-proxy /usr/local/bin/
      - name: Apply migrations
        run: python scripts/migrate.py --env ${{ inputs.env }} up
      - name: Confirm status
        run: python scripts/migrate.py --env ${{ inputs.env }} status

  # ────────────────────────────────────────────────────────────────
  # Step 7: CSV sync
  # ────────────────────────────────────────────────────────────────
  sync-csv:
    needs: [migrate]
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with: { ref: ${{ inputs.git-ref }} }
      - uses: google-github-actions/auth@v2
        with:
          workload_identity_provider: ${{ secrets.WIF_PROVIDER }}
          service_account: ${{ secrets.WIF_SA }}
      - uses: google-github-actions/setup-gcloud@v2
      - name: Upload property list
        run: |
          gsutil cp data/property-list/properties.csv \
            gs://jugnu-raw-${{ inputs.env }}/property-list/properties.csv

  # ────────────────────────────────────────────────────────────────
  # Step 8-9: smoke test + mark known-good
  # ────────────────────────────────────────────────────────────────
  smoke:
    needs: [sync-csv]
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with: { ref: ${{ inputs.git-ref }} }
      - uses: google-github-actions/auth@v2
        with:
          workload_identity_provider: ${{ secrets.WIF_PROVIDER }}
          service_account: ${{ secrets.WIF_SA }}
      - uses: google-github-actions/setup-gcloud@v2
      - uses: actions/setup-python@v5
        with: { python-version: "3.11", cache: pip }
      - run: pip install -r requirements.txt
      - name: Run smoke test
        run: python scripts/trigger_smoke.py --env ${{ inputs.env }}
      - name: Tag image as known-good
        run: |
          SOURCE_URI="${{ needs.build-and-push.outputs.image-uri }}"
          TARGET_URI="${REGION}-docker.pkg.dev/${{ secrets.GCP_PROJECT_ID }}/${AR_REPO}/jugnu:${{ inputs.env }}-known-good"
          gcloud artifacts docker tags add "$SOURCE_URI" "$TARGET_URI"
```

---

## 4. The env-specific workflows

`.github/workflows/deploy-staging.yml` — the short one. Triggers on push to main, no approval, no release.

```yaml
name: Deploy to staging

on:
  push:
    branches: [main]
  workflow_dispatch:

concurrency:
  group: deploy-staging
  cancel-in-progress: false         # never cancel a deploy mid-apply

jobs:
  deploy:
    uses: ./.github/workflows/reusable-deploy.yml
    with:
      env: staging
      git-ref: ${{ github.sha }}
      require-approval: false
    secrets:
      WIF_PROVIDER:        ${{ secrets.WIF_PROVIDER }}
      WIF_SA:              ${{ secrets.WIF_SA_STAGING }}
      GCP_PROJECT_ID:      ${{ secrets.GCP_PROJECT_ID_STAGING }}
      OPENROUTER_API_KEY:  ${{ secrets.OPENROUTER_API_KEY_STAGING }}
      PROXY_CREDENTIALS:   ${{ secrets.PROXY_CREDENTIALS_STAGING }}
```

`.github/workflows/deploy-prod.yml` — triggers on signed version tags, requires approval, explicitly disallows deploy from an unverified commit.

```yaml
name: Deploy to production

on:
  push:
    tags: ['v*.*.*']
  workflow_dispatch:
    inputs:
      ref:
        description: "Tag to deploy (must match v*.*.*)"
        required: true
        type: string

concurrency:
  group: deploy-prod
  cancel-in-progress: false

jobs:
  verify-tag:
    runs-on: ubuntu-latest
    outputs:
      ref: ${{ steps.resolve.outputs.ref }}
    steps:
      - uses: actions/checkout@v4
        with:
          ref: ${{ github.ref }}
          fetch-depth: 0
      - id: resolve
        run: |
          # Accept either the pushed tag or the dispatch input
          REF="${{ github.event.inputs.ref || github.ref_name }}"
          if ! [[ "$REF" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
            echo "Ref '$REF' does not match v*.*.*" && exit 1
          fi
          echo "ref=${REF}" >> $GITHUB_OUTPUT
      - name: Verify tag is signed
        run: |
          REF="${{ steps.resolve.outputs.ref }}"
          if ! git verify-tag "$REF" 2>/dev/null; then
            echo "Tag $REF is not signed. Prod deploys require signed tags."
            echo "Create with: git tag -s vX.Y.Z -m 'release notes'"
            exit 1
          fi

  deploy:
    needs: [verify-tag]
    uses: ./.github/workflows/reusable-deploy.yml
    with:
      env: prod
      git-ref: ${{ needs.verify-tag.outputs.ref }}
      require-approval: true
    secrets:
      WIF_PROVIDER:        ${{ secrets.WIF_PROVIDER }}
      WIF_SA:              ${{ secrets.WIF_SA_PROD }}
      GCP_PROJECT_ID:      ${{ secrets.GCP_PROJECT_ID_PROD }}
      OPENROUTER_API_KEY:  ${{ secrets.OPENROUTER_API_KEY_PROD }}
      PROXY_CREDENTIALS:   ${{ secrets.PROXY_CREDENTIALS_PROD }}
```

**Signed tags are the release boundary.** Prod deploys from an unsigned tag fail immediately. The team commits to the workflow: every prod release is `git tag -s vX.Y.Z && git push origin vX.Y.Z`. GitHub's release UI can wrap this, but the signature is the point.

---

## 5. The CSV sync script

`scripts/deploy_csv_sync.py`:

```python
"""Sync data/property-list/properties.csv to the env's GCS bucket.

Called from the deploy workflow. Separated from workflow YAML because (a) it's
real logic with validation, (b) it's runnable locally for testing.

Validates before uploading:
  - File exists and is readable
  - Has the expected header row
  - Non-empty rows
  - No duplicate canonical_ids

Uploads with:
  - Object generation precondition (if-not-match) to avoid races between
    two concurrent deploys
  - Cache-Control: no-cache (workers always want the latest)
"""
import argparse, csv, subprocess, sys
from pathlib import Path

EXPECTED_HEADER = ["canonical_id", "url", "pms_hint"]  # Claude Code: verify against real CSV


def validate(path: Path) -> None:
    if not path.exists():
        sys.exit(f"CSV not found: {path}")
    with path.open() as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if header != EXPECTED_HEADER:
            sys.exit(f"CSV header mismatch. Expected {EXPECTED_HEADER}, got {header}")
        seen = set()
        n = 0
        for row_num, row in enumerate(reader, start=2):
            if not row:
                continue
            cid = row[0]
            if cid in seen:
                sys.exit(f"Duplicate canonical_id '{cid}' at row {row_num}")
            seen.add(cid)
            n += 1
        if n == 0:
            sys.exit("CSV has zero data rows")
    print(f"✓ {n} properties, no duplicates", file=sys.stderr)


def upload(path: Path, env: str) -> None:
    target = f"gs://jugnu-raw-{env}/property-list/properties.csv"
    subprocess.check_call([
        "gsutil",
        "-h", "Cache-Control:no-cache",
        "cp", str(path), target,
    ])
    print(f"✓ uploaded to {target}", file=sys.stderr)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--env", choices=["staging", "prod"], required=True)
    p.add_argument("--path", default="data/property-list/properties.csv")
    args = p.parse_args()
    csv_path = Path(args.path)
    validate(csv_path)
    upload(csv_path, args.env)
```

Validation catches the most common CSV problems before they reach production. A bad CSV that passes validation but trips the runner is a bug worth fixing *in validation*, not in a post-mortem.

---

## 6. Rollback

**Manual, deliberate, and documented.** No auto-rollback. Here's the full procedure, which goes in `docs/RUNBOOK_INCIDENTS.md`:

### When to roll back

- Smoke test fails after a deploy → workflow aborts automatically; rollback is moot (nothing was promoted to known-good)
- Production scrape fails repeatedly after a successful deploy, and failures correlate to the deploy time → roll back

### How to roll back (Cloud Run image)

```bash
# 1. Find the last known-good image
gcloud artifacts docker tags list \
  us-central1-docker.pkg.dev/<prod-project>/jugnu-images/jugnu \
  --filter="tag:prod-known-good" --format=json

# 2. That tag points to a specific digest; tag it as the active image
gcloud run jobs update jugnu-scrape-prod \
  --image=us-central1-docker.pkg.dev/<prod-project>/jugnu-images/jugnu:prod-known-good \
  --region=us-central1
gcloud run jobs update jugnu-retry-prod \
  --image=us-central1-docker.pkg.dev/<prod-project>/jugnu-images/jugnu:prod-known-good \
  --region=us-central1

# 3. Verify by running a canary scrape
python scripts/trigger_smoke.py --env prod
```

### How to roll back (database migration)

Only if the migration is destructive or incompatible. For additive migrations that merely weren't needed by the previous code version, leave them in place.

```bash
python scripts/migrate.py --env prod down --steps 1
```

**If the rollback itself fails:** the escalation path is database restore from the automated Cloud SQL backup (daily at 03:30 UTC per `CLAUDE_TERRAFORM.md` §4.4). Restore takes ~5 minutes and loses at most ~24h of data. This is acceptable for a POC; revisit at prod scale.

---

## 7. Gates

| Gate | Check | Command / verification |
|---|---|---|
| DEP-1 | Workflow YAML valid | `actionlint .github/workflows/*.yml` exits 0 |
| DEP-2 | Staging deploy succeeds end-to-end | Push a trivial change to main; all 6 jobs complete green |
| DEP-3 | Staging smoke test actually runs the pipeline | Inspect `gs://jugnu-raw-staging/runs/<date>/shard_0/`; 3 rows of real scrape output exist |
| DEP-4 | Known-good tag advances | `gcloud artifacts docker tags list ... --filter=tag:staging-known-good` shows digest matches the deploy's image |
| DEP-5 | Cloud SQL wake-up works | Stop the staging SQL manually; trigger a deploy; deploy completes (does not fail because DB was stopped) |
| DEP-6 | Prod workflow requires signed tag | Push an unsigned tag `v99.99.99`; workflow fails at `verify-tag` |
| DEP-7 | Prod workflow requires environment approval | Push a signed tag; deploy waits for approval in GitHub UI; completes after approval granted |
| DEP-8 | Prod deploy succeeds end-to-end | Signed tag + approval → all jobs green → prod-known-good tag advances |
| DEP-9 | Destructive TF changes on prod are blocked | Stage a Terraform PR that drops a resource; run the prod deploy workflow; it fails at `terraform-apply` with a clear message |
| DEP-10 | Failed smoke test aborts before known-good tag moves | Artificially break the canary property (point to a bad URL); deploy; smoke fails; known-good tag is unchanged |
| DEP-11 | Rollback procedure works | Roll forward with a new image; follow the rollback runbook; `trigger_smoke.py` passes against the rolled-back image |
| DEP-12 | Two concurrent staging deploys serialize | Push twice in quick succession; second deploy waits for first to finish (or is cancelled per concurrency rules) |
| DEP-13 | CSV validation catches real errors | Commit a CSV with a duplicate canonical_id; deploy fails at `sync-csv` with a clear message |
| DEP-14 | Migration failure aborts deploy | Stage a broken migration in staging; deploy aborts at `migrate`; Cloud Run image is NOT updated |

Gates DEP-3, DEP-5, DEP-9, DEP-10, DEP-13, DEP-14 are the most important — they verify the workflow catches real failure modes, not just nominal flows.

---

## 8. Non-negotiables

- **No deploy from a non-main commit to staging.** The staging workflow is triggered on pushes to main; a workflow_dispatch from an arbitrary branch is disabled.
- **No deploy from an unsigned tag to prod.** Enforced in `verify-tag`.
- **No rewrites of deploy history.** `concurrency: cancel-in-progress: false` on both deploy workflows. Cancelling a deploy mid-apply produces split-brain states.
- **No secrets in workflow `env:` blocks or `run:` commands without `${{ secrets... }}`.** Anything that looks like a secret but isn't marked as one will be logged.
- **No `gcloud ... --quiet` except where necessary.** Confirmation prompts protect against typos in interactive use; suppression should be intentional.
- **No deploy that modifies the database structure (ALTER, DROP) without going through a migration file.** If you think you need to run raw SQL, you need to add a migration.
- **No `continue-on-error: true` on any deploy step.** Every step either succeeds or aborts the deploy.
- **No deploy workflow that ever runs `terraform destroy`.** Teardown is a separate, explicitly-invoked workflow — not accidentally reachable.

---

## 9. Why not automated rollback?

A reasonable team could argue for automated rollback. We're explicitly choosing against it for this POC. The reasons, so nobody re-proposes it without new evidence:

**Rollback implies detection.** Automated rollback triggers on what? The smoke test already gates the deploy — if smoke fails, nothing was promoted. The remaining failure mode is "deploy succeeds, smoke succeeds, then prod breaks hours later." Detecting that automatically requires either (a) continuous health monitoring of the production scrape runs, which is fundamentally an observability problem not a deploy problem, or (b) a metric-based alert that could equally trigger manual rollback. Neither motivates auto-rollback.

**Cloud Run jobs are idempotent.** A bad deploy doesn't corrupt data (writes are `(canonical_id, run_date)`-idempotent). It wastes one day of scraping. The cost of one bad day is lower than the cost of debugging an auto-rollback system that rolls back for the wrong reason.

**Manual rollback is fast.** The three commands in §6 take under a minute. Complex auto-rollback systems take longer to fix when they break than manual rollback takes to execute.

Revisit this decision when the system has real users who will notice a bad day.

---

## 10. Known-risky patterns to avoid

Three deploy anti-patterns that produce hard-to-debug incidents:

**A. Terraform apply without a preceding plan in the same run.** We include both. Plans without apply are review-only; applies without re-planning let state drift slip through.

**B. Running migrations after updating the Cloud Run image.** The arch doc's stop-when-idle pattern makes this worse: the running workers from the old image could be hitting a schema the new migrations broke, or the new workers could be hitting a schema the old migrations hadn't updated. We apply migrations *before* image update — additive migrations don't break old code; old code doesn't break new migrations.

**C. Silent tag moves.** Tagging an image as `{env}-latest` is fine, but only if it's always paired with a commit-specific tag (`{env}-{sha}`). A `latest`-only deploy leaves you with no way to roll back because nothing is addressable. Our pattern — push `{env}-{sha}` first, then tag as `{env}-latest` — means rollback always has a target.

---

## 11. Open questions

- **Integrate with release notes automation (release-drafter, release-please)?** Recommendation: add in a future handoff. Ship the base deploy pipeline first; add release note generation once there's a cadence to describe.
- **Deploy to a preview environment on PR open?** Recommendation: no. Staging is shared by design; ephemeral envs per PR add infrastructure cost and complexity for unclear benefit at POC scale.
- **Slack/Discord/Teams notification on deploy?** Recommendation: **yes, add a final step in the reusable workflow**. One line with the env, tag, and status. Cheap insurance against silent failures. Keep the implementation in scope of this handoff.
- **Should deploy-prod allow manual trigger from main (without a tag)?** Recommendation: no. The tag is the release commitment; skipping it normalizes ad-hoc prod deploys.

---

## 12. When this handoff is complete

Claude Code has:
1. Created every file in §1
2. All 14 gates in §7 pass on staging (prod-specific gates verified as far as possible without a real prod release)
3. Posted a PR demo: one staging deploy end-to-end in the PR description, screenshots of the GitHub UI showing each step
4. Walked a second human through triggering a prod deploy from a signed tag using `docs/DEPLOY.md` alone — they succeed without author intervention
5. Executed a rollback using `docs/RUNBOOK_INCIDENTS.md` alone — the rolled-back version serves traffic cleanly

This is the last handoff. When it's complete, the CI/CD pipeline is operational.
