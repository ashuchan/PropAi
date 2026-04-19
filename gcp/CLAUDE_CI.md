# CLAUDE_CI.md

**Goal:** Build the GitHub Actions PR-gate workflow. Every pull request against `main` must pass a defined set of checks before merge. Failures block the merge button — no human override except for repo admins in emergencies.

**Read before starting:**
- `claude_refactor.md` — the existing gate discipline (`scripts/gate_refactor.py phase N`) that this CI workflow enforces
- `CLAUDE_DOCKERFILE.md`, `CLAUDE_TERRAFORM.md`, `CLAUDE_TRIGGERS.md`, `CLAUDE_MIGRATIONS.md` — the code this CI gates
- Existing `pyproject.toml` / `requirements-dev.txt` for the toolchain (ruff, mypy, pytest versions)

**Prerequisite:** All four prior handoffs merged. CI that tests code that doesn't exist yet is premature.

---

## 1. Scope

What this handoff produces:

- `.github/workflows/ci.yml` — the main PR-gate workflow
- `.github/workflows/reusable-python-setup.yml` — factored-out setup steps (shared with deploy workflows)
- `.github/dependabot.yml` — weekly dependency PRs for Actions and pip
- Branch protection rules document in `docs/BRANCH_PROTECTION.md` (applied in GitHub UI, can't be code)

What this handoff does **not** produce:
- Deploy workflows (that's `CLAUDE_DEPLOY.md`)
- Any non-GitHub CI (no CircleCI, no Jenkins — we're committed to GitHub Actions)
- Self-hosted runners (ubuntu-latest hosted runners are adequate for this workload)

---

## 2. Workflow triggers and structure

The CI workflow runs on:

```yaml
on:
  pull_request:
    branches: [main]
  push:
    branches: [main]                 # catches direct pushes (rare) and main-branch revalidation
  workflow_dispatch:                  # manual trigger for debugging
```

**Not `on: push` for all branches.** Running CI on every developer branch push is wasteful — we only care at PR-open and PR-update.

**Jobs, parallelized where possible:**

```
            ┌── python-lint ──┐
            │                 │
            ├── python-type ──┤
  setup ────┤                 ├──── gate-summary (required status check)
            ├── python-test ──┤
            │                 │
            ├── docker-build ─┤
            │                 │
            ├── terraform ────┤
            │                 │
            └── migration-rt ─┘
```

- **setup** is a no-op that proves the `reusable-python-setup.yml` workflow works; other jobs depend on it implicitly via the reusable workflow
- **gate-summary** is the single "required status check" branch protection targets — it depends on all others and fails if any fail. One check to manage in GitHub's UI instead of seven

---

## 3. The reusable Python setup workflow

Factor it out now, even though only CI uses it initially — `CLAUDE_DEPLOY.md` will import the same workflow, and having two copies of the setup block is the first step toward them drifting.

`.github/workflows/reusable-python-setup.yml`:

```yaml
name: reusable-python-setup

on:
  workflow_call:
    inputs:
      python-version:
        required: false
        type: string
        default: "3.11"
      install-dev:
        required: false
        type: boolean
        default: true

jobs:
  setup:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: ${{ inputs.python-version }}
          cache: pip
          cache-dependency-path: |
            requirements.txt
            requirements-dev.txt
      - name: Install deps
        run: |
          python -m pip install --upgrade pip
          pip install -r requirements.txt
          if ${{ inputs.install-dev }}; then
            pip install -r requirements-dev.txt
          fi
      - name: Cache Playwright browsers
        # Only needed for tests that actually launch browsers — most don't
        uses: actions/cache@v4
        with:
          path: ~/.cache/ms-playwright
          key: playwright-${{ runner.os }}-${{ hashFiles('requirements.txt') }}
```

**The caching matters.** Without `pip` caching, every CI run takes ~3 minutes just to install dependencies. With caching, ~15 seconds on a cache hit. The `cache-dependency-path` ensures the cache invalidates when requirements change, not on every run.

---

## 4. The main CI workflow

`.github/workflows/ci.yml`:

```yaml
name: CI

on:
  pull_request:
    branches: [main]
  push:
    branches: [main]
  workflow_dispatch:

# Cancel in-progress runs on the same PR when a new push arrives
concurrency:
  group: ci-${{ github.ref }}
  cancel-in-progress: true

# Minimal permissions — tighten wherever possible
permissions:
  contents: read
  pull-requests: write              # for PR comments; see terraform job

jobs:
  python-lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.11", cache: pip }
      - run: pip install -r requirements-dev.txt
      - run: ruff check ma_poc/ scripts/ tests/
      - run: ruff format --check ma_poc/ scripts/ tests/

  python-type:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.11", cache: pip }
      - run: pip install -r requirements.txt -r requirements-dev.txt
      # Match the strictness from claude_refactor.md
      - run: mypy --strict ma_poc/pms/ ma_poc/reporting/ scripts/_trigger_common.py

  python-test:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: postgres:15-alpine
        env:
          POSTGRES_USER: test
          POSTGRES_PASSWORD: test
          POSTGRES_DB: jugnu_test
        ports: ["5432:5432"]
        options: >-
          --health-cmd pg_isready
          --health-interval 10s
          --health-timeout 5s
          --health-retries 5
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.11", cache: pip }
      - run: pip install -r requirements.txt -r requirements-dev.txt
      - name: Run tests with coverage gates
        env:
          DATABASE_URL: postgresql://test:test@localhost:5432/jugnu_test
        run: |
          pytest tests/ \
            --cov=ma_poc.pms \
            --cov=ma_poc.reporting \
            --cov=scripts._trigger_common \
            --cov-report=term-missing \
            --cov-report=xml \
            --cov-fail-under=80 \
            --ignore=data \
            --ignore=config
      - name: Enforce per-package floors
        # Re-check specific packages with their own thresholds from claude_refactor.md
        run: |
          python -c "
          import xml.etree.ElementTree as ET
          tree = ET.parse('coverage.xml')
          pkgs = {p.attrib['name']: float(p.attrib['line-rate']) * 100 for p in tree.iter('package')}
          thresholds = {'ma_poc.pms': 85, 'ma_poc.reporting': 80}
          failed = [(pkg, pct, thr) for pkg, thr in thresholds.items() for p, pct in pkgs.items() if p == pkg and pct < thr]
          if failed:
              for pkg, pct, thr in failed:
                  print(f'FAIL: {pkg} coverage {pct:.1f}% < {thr}%')
              exit(1)
          "
      - uses: actions/upload-artifact@v4
        if: always()
        with:
          name: coverage
          path: coverage.xml

  refactor-gates:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.11", cache: pip }
      - run: pip install -r requirements.txt -r requirements-dev.txt
      - name: Run existing refactor gates
        # Integrates with the existing discipline from claude_refactor.md
        run: python scripts/gate_refactor.py all

  docker-build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: docker/setup-buildx-action@v3
      - name: Build (no push)
        uses: docker/build-push-action@v5
        with:
          context: .
          push: false
          tags: jugnu:ci
          cache-from: type=gha
          cache-to: type=gha,mode=max
      - name: Smoke test the built image
        run: |
          docker run --rm jugnu:ci
          docker run --rm -e CLOUD_RUN_TASK_INDEX=0 -e CLOUD_RUN_TASK_COUNT=1 \
            jugnu:ci python scripts/jugnu_shard_entry.py
      - name: Image size check
        run: |
          size_bytes=$(docker image inspect jugnu:ci --format='{{.Size}}')
          size_mb=$((size_bytes / 1024 / 1024))
          echo "Image size: ${size_mb}MB"
          if [ "$size_mb" -gt 1500 ]; then
            echo "FAIL: image exceeds 1500MB budget"
            exit 1
          fi

  terraform:
    runs-on: ubuntu-latest
    defaults:
      run:
        working-directory: infra/terraform
    steps:
      - uses: actions/checkout@v4
      - uses: hashicorp/setup-terraform@v3
        with:
          terraform_version: "1.7.5"
      - run: terraform fmt -recursive -check
      - name: Init (no backend — we're validating only)
        run: terraform init -backend=false
      - run: terraform validate
      - name: Validate each environment
        run: |
          for env in staging prod; do
            echo "::group::Validating $env"
            # Plan requires backend+auth; we validate config only
            terraform validate -var-file=envs/${env}.tfvars \
              -var="image_tag=ci-validation" \
              -var="vpc_self_link=projects/dummy/global/networks/default" \
              -var="deployer_sa_email=dummy@example.com"
            echo "::endgroup::"
          done

  migration-round-trip:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.11", cache: pip }
      - run: pip install -r requirements.txt -r requirements-dev.txt
      # testcontainers handles Postgres lifecycle
      - run: pytest tests/migrations/ -v

  gate-summary:
    # This is the single "required status check" in branch protection
    needs:
      - python-lint
      - python-type
      - python-test
      - refactor-gates
      - docker-build
      - terraform
      - migration-round-trip
    runs-on: ubuntu-latest
    if: always()
    steps:
      - name: Check all jobs passed
        run: |
          if [ "${{ contains(needs.*.result, 'failure') || contains(needs.*.result, 'cancelled') }}" = "true" ]; then
            echo "One or more required jobs failed"
            exit 1
          fi
          echo "All gates passed"
```

### Why these jobs, and why in parallel

- **Parallelism is ~free** — GitHub gives you 20 concurrent jobs on free tier; we use 7. Serializing these would make CI take 8 minutes instead of 3.
- **Each job installs its own deps.** Pip cache is per-job but hits the same key, so it's fast. The alternative — one setup job that uploads artifacts — adds complexity for no gain.
- **`gate-summary` is the "one required check" pattern.** GitHub's branch protection UI requires checks to be listed by name. If a required check is renamed or removed, PRs on old branches get stuck. One aggregate check avoids this class of breakage.

---

## 5. Dependabot configuration

`.github/dependabot.yml`:

```yaml
version: 2
updates:
  - package-ecosystem: "pip"
    directory: "/"
    schedule: { interval: "weekly" }
    open-pull-requests-limit: 5
    groups:
      # Batch minor/patch updates into one PR per ecosystem
      pip-minor:
        update-types: ["minor", "patch"]
    ignore:
      # Playwright version is pinned to match the Dockerfile base
      # Upgrade requires a coordinated Dockerfile change (see CLAUDE_DOCKERFILE.md §2)
      - dependency-name: "playwright"

  - package-ecosystem: "github-actions"
    directory: "/"
    schedule: { interval: "weekly" }
    groups:
      actions-minor:
        update-types: ["minor", "patch"]

  - package-ecosystem: "docker"
    directory: "/"
    schedule: { interval: "weekly" }
    # Base image updates — expect these to break things occasionally; review carefully
```

Dependabot PRs pass or fail the same CI workflow. That's the point — if an upgrade breaks something, CI catches it before merge.

---

## 6. Branch protection configuration

Document in `docs/BRANCH_PROTECTION.md` — these are applied in the GitHub UI (Settings → Branches → Branch protection rules) and cannot be code:

**Rule: `main`**
- Require a pull request before merging: **enabled**
- Require approvals: **1** (2 for repos with > 3 maintainers; 1 is fine at POC team size)
- Dismiss stale pull request approvals when new commits are pushed: **enabled**
- Require review from Code Owners: **enabled** (once `CODEOWNERS` exists)
- Require status checks to pass before merging: **enabled**
  - Required checks: `gate-summary` (one check; §4 explains why)
  - Require branches to be up to date before merging: **enabled**
- Require conversation resolution before merging: **enabled**
- Do not allow bypassing the above settings: **enabled for prod-adjacent repos; optional elsewhere**
- Restrict who can push to matching branches: **enabled** (only GitHub Actions and specific maintainers)
- Allow force pushes: **disabled**
- Allow deletions: **disabled**

**CODEOWNERS file** (`.github/CODEOWNERS`):

```
# Require review from the infra owner for infrastructure changes
infra/                  @<infra-owner-handle>
.github/workflows/      @<infra-owner-handle>
Dockerfile              @<infra-owner-handle>

# Rest of the repo — default reviewers
*                       @<team-handle>
```

---

## 7. Gates

The CI workflow gates itself — if the workflow is broken, no PRs can merge. But Claude Code still needs to verify the workflow works. These gates run manually during handoff:

| Gate | Check | How |
|---|---|---|
| CI-1 | Workflow YAML is valid | `actionlint .github/workflows/*.yml` exits 0 (install locally or as a pre-commit hook) |
| CI-2 | Workflow runs on a test PR | Open a throwaway PR with a trivial change; all 7 jobs run |
| CI-3 | `gate-summary` fails when a dependency fails | Push a commit with a lint error; `python-lint` fails; `gate-summary` fails; PR merge is blocked |
| CI-4 | `gate-summary` passes when all pass | Fix the lint error; all jobs pass; merge is unblocked |
| CI-5 | pip cache works on second run | Run workflow twice; second run's setup step completes in < 30s |
| CI-6 | Docker cache works on second run | Run workflow twice; second `docker-build` completes in < 60s |
| CI-7 | Coverage gate enforces floor | Artificially lower test coverage on a PR; `python-test` fails |
| CI-8 | Terraform validate catches real errors | Introduce a typo in a tfvars file; `terraform` job fails |
| CI-9 | Migration round-trip catches bad migrations | Introduce a broken `downgrade()`; `migration-round-trip` fails |
| CI-10 | Branch protection is actually applied | On a PR with a failing gate, the "Merge" button is disabled |
| CI-11 | Concurrency group cancels superseded runs | Push twice in quick succession to the same PR; only the latest run continues |
| CI-12 | Dependabot PRs trigger the same workflow | A Dependabot PR shows the same 7 jobs |

Gate CI-10 is the most important — branch protection applied in the UI but not enforced is the most common failure mode of CI setups. **Test this by trying to merge a failing PR.**

---

## 8. Non-negotiables

- **No workflow with unpinned Actions versions.** `actions/checkout@v4`, not `actions/checkout@main`. Unpinned Actions break silently when upstream changes.
- **No workflow with `write-all` permissions.** Specify exactly what's needed at the workflow level; default to read.
- **No workflow that sets `continue-on-error: true` on a gate job.** If it's not required to pass, remove it from CI.
- **No skipping CI via commit message tags.** `[skip ci]` and friends bypass the gates. Don't configure branch protection to allow admins to skip; use the GitHub UI's explicit bypass when a true emergency needs it, and log the reason.
- **No third-party Actions without review.** Every Action in the workflow is either from a trusted publisher (`actions/`, `docker/`, `hashicorp/`, `google-github-actions/`) or has its SHA pinned explicitly after review.
- **No `GITHUB_TOKEN` with `contents: write` in CI.** CI doesn't write to the repo. That's deploy's job.

---

## 9. Known-risky patterns to avoid

Three CI anti-patterns that produce hard-to-debug failures:

**A. Caching that doesn't invalidate cleanly.** The `cache-dependency-path` pattern in the reusable workflow works because `requirements.txt` is stable. If you start including dynamic paths, cache keys collide and old deps get resurrected silently.

**B. Services that aren't actually ready when tests start.** The Postgres service in `python-test` uses `--health-cmd pg_isready`, which is necessary. Without it, tests start before the DB is ready and fail in a confusing way.

**C. Tests that pass locally but fail in CI (or vice versa).** Usually a path assumption, a timezone assumption, or a file-system case-sensitivity mismatch (macOS is case-insensitive by default; Linux is not). When you see this pattern, the fix is almost always in the test, not in CI.

---

## 10. Open questions

- **Should we block merge on coverage decrease, not just absolute floor?** Recommendation: **not in v1**. Absolute floor is easier to reason about; delta-based gates are notorious for false positives on test refactors.
- **Separate workflow for security scanning (trivy, bandit, etc)?** Recommendation: add one in a future handoff. Ship the base CI first; layer security scans on once the pipeline is stable.
- **Self-hosted runners for faster builds?** Recommendation: **no, not at POC scale**. Hosted runners are 90% as fast and 100% less to operate. Revisit at prod scale.
- **GitHub Environments for CI (vs. only Deploy)?** Recommendation: no — Environments are for deploys, not CI. CI runs against ephemeral test resources.

---

## 11. When this handoff is complete

Claude Code has:
1. Created every file in §1
2. All gates in §7 pass
3. Branch protection is applied to `main` and verified (gate CI-10)
4. A throwaway PR has gone through the full cycle: open → CI runs → fails for a real reason → fix → passes → merge enabled
5. Dependabot has opened at least one PR and it has been merged cleanly

Only then proceed to `CLAUDE_DEPLOY.md`.
