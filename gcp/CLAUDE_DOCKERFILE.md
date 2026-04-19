# CLAUDE_DOCKERFILE.md

**Goal:** Produce a production-grade Docker image that runs `scripts/jugnu_shard_entry.py` (scrape) or `scripts/jugnu_retry_entry.py` (retry) inside a Cloud Run task. The image must be reproducible, cache-friendly, and small enough that cold-start pull time doesn't dominate the 4h run budget.

**Read before starting:**
- `Jugnu_Deployment_Architecture_GCP.docx` — especially §2 (worker contract) and §3 (shard sizing)
- `README.md` — the Playwright install and environment setup patterns
- `requirements.txt` — the full Python dependency set
- `scripts/jugnu_runner.py` — note the runner expects `ma_poc/` to be importable; paths matter

**This handoff runs first.** It has no dependencies. Every other handoff references an image in Artifact Registry; without one, Terraform can't create the Cloud Run jobs.

---

## 1. Scope

What this handoff produces:

- `Dockerfile` at repo root
- `.dockerignore` at repo root
- `scripts/jugnu_shard_entry.py` (stub — full implementation lives in `CLAUDE_TRIGGERS.md`, but a minimal version needs to exist so the image has a working `ENTRYPOINT`)
- `scripts/jugnu_retry_entry.py` (same — minimal stub)
- Local build and smoke validation via a `Makefile` target

What this handoff does **not** produce:
- The full `jugnu_shard_entry.py` logic (deferred to `CLAUDE_TRIGGERS.md`)
- Any cloud resources (deferred to `CLAUDE_TERRAFORM.md`)
- Image push automation (deferred to `CLAUDE_DEPLOY.md`)

The scope is intentionally narrow: produce an image that builds locally, runs locally, and can be validated before any cloud infrastructure exists.

---

## 2. Base image choice

**Use `mcr.microsoft.com/playwright/python:v1.47.0-jammy`.**

Why this exact image:
- Playwright browsers are already baked in — `playwright install chromium` adds 400MB and 2 minutes to every build if you install it yourself
- Ubuntu 22.04 (jammy) is the version Playwright's install scripts test against; alpine variants miss glibc dependencies Chromium needs
- Pinned version `v1.47.0` — not `latest`. Playwright browser version drift silently changes extraction behavior; the day a background rebuild pulls a new Chromium is the day Tier 1 extraction flakes for reasons that take a day to diagnose

**Do not** use:
- `python:3.11-slim` + `playwright install chromium` — adds ~400MB and 90s to every cold build
- `python:3.11-alpine` — Chromium has native deps that don't resolve cleanly on musl
- `mcr.microsoft.com/playwright:latest` — unpinned; your prod runs become non-reproducible the moment MSFT ships an update

When you upgrade Playwright: change the version in **two places** (Dockerfile FROM line and `requirements.txt` playwright pin). Mismatches cause a runtime import error that's easy to misdiagnose.

---

## 3. Dockerfile specification

The file must follow these constraints. Every constraint has a reason; don't drop any without understanding the tradeoff.

### Layering for cache efficiency

```dockerfile
FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

# Non-root user for runtime (security baseline)
# Playwright image ships with a 'pwuser' already; reuse it.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Layer 1: system deps (rarely change — cached aggressively)
# Only add what the Playwright base image doesn't already provide.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Layer 2: Python deps (change when requirements.txt changes)
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Layer 3: source code (changes on every commit — last)
COPY ma_poc/ ./ma_poc/
COPY scripts/ ./scripts/
COPY config/ ./config/

# Layer 4: drop privileges
USER pwuser

# No CMD — ENTRYPOINT is provided per-job by Cloud Run (container command override)
# Default entrypoint is a sanity-check that imports pass.
ENTRYPOINT ["python", "-c", "import ma_poc; print('jugnu image OK')"]
```

**Why this ordering matters:** changes to source trigger only the last layer rebuild (~2s). Changes to `requirements.txt` trigger the last two layers (~45s). Changes to system packages trigger everything (~2min). Operators edit source 100× more than they edit requirements, and edit requirements 100× more than they edit system packages. This ordering makes the common case fast.

### What goes in `.dockerignore`

```
# State and output — never want these in the image
data/
config/profiles/
logs/
*.db
*.sqlite

# Dev artifacts
.venv/
.env
.env.*
__pycache__/
*.pyc
.pytest_cache/
.mypy_cache/
.ruff_cache/
htmlcov/
.coverage
coverage.xml

# Git and CI
.git/
.github/
.gitignore

# Documentation — doesn't belong at runtime
docs/
*.md
!requirements.txt

# IDE
.vscode/
.idea/
*.swp

# Build artifacts
dist/
build/
*.egg-info/

# Large tests data that shouldn't ship
tests/fixtures/large/
```

**The `config/profiles/` exclusion is critical.** Profiles are runtime state, not code — they live in Postgres in the deployed system. Including them in the image would mean every deploy reverts profiles to whatever was in the repo at image build time. Silent data loss.

### Entrypoint scripts — minimal stubs

`scripts/jugnu_shard_entry.py` — the real implementation is in `CLAUDE_TRIGGERS.md`, but this handoff needs a working stub so the image validates:

```python
"""scripts/jugnu_shard_entry.py — stub; real implementation in CLAUDE_TRIGGERS.md."""
import os, sys

if __name__ == "__main__":
    task_idx = os.environ.get("CLOUD_RUN_TASK_INDEX", "0")
    task_count = os.environ.get("CLOUD_RUN_TASK_COUNT", "1")
    print(f"jugnu_shard_entry stub: task {task_idx}/{task_count}")
    # Import sanity check — catches path/package regressions at deploy time
    from ma_poc.pms import scraper  # noqa: F401
    sys.exit(0)
```

`scripts/jugnu_retry_entry.py` — same pattern:

```python
"""scripts/jugnu_retry_entry.py — stub; real implementation in CLAUDE_TRIGGERS.md."""
import os, sys

if __name__ == "__main__":
    mode = os.environ.get("RETRY_MODE", "errors")
    print(f"jugnu_retry_entry stub: mode={mode}")
    from ma_poc.pms import scraper  # noqa: F401
    sys.exit(0)
```

These stubs exist solely so `CLAUDE_TERRAFORM.md` can deploy the Cloud Run jobs and verify they run end-to-end. `CLAUDE_TRIGGERS.md` replaces them with the real implementations.

---

## 4. Image size budget

**Target: under 1.5GB.** The Playwright base is already ~1.1GB; your additions should stay under 400MB.

Why this matters: Cloud Run task cold-start includes image pull time. At 5 parallel tasks pulling simultaneously from Artifact Registry in the same region, pull time is ~10-15s per GB. A 2.5GB image adds 40s to every run's wall clock; a 1.5GB image adds 15s. Over 30 daily runs that's a 12-minute difference — trivial, but worth the discipline.

Size-check commands to run during development:

```bash
# See the layer breakdown — flags oversized layers
docker history jugnu:local

# Full image size
docker images jugnu:local --format '{{.Size}}'

# What's actually in the image (useful for finding accidental inclusions)
docker run --rm jugnu:local du -sh /app/* /usr/local/lib/python3.11/site-packages 2>/dev/null | sort -h
```

If you blow the budget, the usual culprits (in order of frequency):
1. A dev dependency accidentally in `requirements.txt` (pandas, jupyter, torch are common offenders)
2. Test fixtures not excluded in `.dockerignore`
3. A forgotten `git clone` in a `RUN` instruction bundling a repo's `.git/` history

---

## 5. Makefile targets

Add to repo root `Makefile`:

```makefile
IMAGE_NAME ?= jugnu
IMAGE_TAG  ?= local

.PHONY: build
build:  ## Build the Docker image locally
	docker build -t $(IMAGE_NAME):$(IMAGE_TAG) .

.PHONY: smoke
smoke: build  ## Run the entrypoint sanity check
	docker run --rm $(IMAGE_NAME):$(IMAGE_TAG)

.PHONY: smoke-shard
smoke-shard: build  ## Run shard entry stub with fake task env
	docker run --rm \
		-e CLOUD_RUN_TASK_INDEX=0 \
		-e CLOUD_RUN_TASK_COUNT=1 \
		$(IMAGE_NAME):$(IMAGE_TAG) \
		python scripts/jugnu_shard_entry.py

.PHONY: smoke-retry
smoke-retry: build  ## Run retry entry stub
	docker run --rm \
		-e RETRY_MODE=errors \
		$(IMAGE_NAME):$(IMAGE_TAG) \
		python scripts/jugnu_retry_entry.py

.PHONY: shell
shell: build  ## Open a shell in the image for debugging
	docker run --rm -it --entrypoint bash $(IMAGE_NAME):$(IMAGE_TAG)

.PHONY: size
size: build  ## Report the image size and layer breakdown
	@docker images $(IMAGE_NAME):$(IMAGE_TAG) --format 'size: {{.Size}}'
	@echo "---"
	@docker history $(IMAGE_NAME):$(IMAGE_TAG) --format 'table {{.CreatedBy}}\t{{.Size}}' | head -20
```

These targets are what Claude Code uses to validate the handoff locally before anything cloud-side exists.

---

## 6. Gates

| Gate | Check | Command |
|---|---|---|
| DOCKER-1 | Image builds without error | `make build` exits 0 |
| DOCKER-2 | Image size under 1.5GB | `docker images jugnu:local --format '{{.Size}}'` reports < 1500MB |
| DOCKER-3 | Default entrypoint runs clean | `make smoke` prints "jugnu image OK" and exits 0 |
| DOCKER-4 | Shard entry stub runs clean | `make smoke-shard` prints task info and exits 0 |
| DOCKER-5 | Retry entry stub runs clean | `make smoke-retry` prints mode info and exits 0 |
| DOCKER-6 | Core imports succeed inside container | `docker run --rm jugnu:local python -c "import playwright; from ma_poc.pms import scraper; import asyncio"` exits 0 |
| DOCKER-7 | Playwright chromium is present | `docker run --rm jugnu:local playwright --version` reports version matching Dockerfile pin |
| DOCKER-8 | Runs as non-root | `docker run --rm jugnu:local id -u` reports non-zero (pwuser's UID) |
| DOCKER-9 | No profiles or data in image | `docker run --rm jugnu:local ls /app/` does not list `data/`, `config/profiles/`, `logs/` |
| DOCKER-10 | `.dockerignore` prevents bloat | `docker build` log shows COPY operations transferring < 50MB total; `htmlcov/` / `.venv/` / `.git/` not in build context |
| DOCKER-11 | Second build uses cache | Run `make build` twice in a row; second run completes in < 10s |
| DOCKER-12 | Source-only changes trigger only last layer rebuild | Touch `scripts/jugnu_runner.py`, rerun `make build`; only source-layer COPY reruns |

---

## 7. Non-negotiables

- **No `ADD` with remote URLs.** Always `COPY`. `ADD` with URLs is non-reproducible across runs.
- **No `RUN apt-get update` without matching `apt-get install` in the same layer.** Splitting them means the cached update layer goes stale silently.
- **No pip install from git URLs in `requirements.txt`.** If you need an unreleased package, vendor it or fork it; git URLs re-fetch on every build and fail when GitHub has an outage.
- **No secrets in the image, ever.** Not in env vars, not in config files, not in encrypted form. Secrets come from Secret Manager at runtime, bound by Cloud Run's `secret_key_ref`.
- **No `USER root` at the end of the Dockerfile.** Always drop privileges for the runtime process.
- **No `latest` tags anywhere.** Not on the base image, not on the Playwright version, not on Python packages.

---

## 8. Open questions

- **Multi-stage build for even smaller image?** Possible to strip ~200MB by separating build-time deps (pip wheels, compilers) from runtime. Recommendation: **not in v1**. Single-stage is 2× faster to build and debug, and 1.5GB is fine for this workload. Revisit if image size becomes a real problem.
- **Pin Python patch version (3.11.9) or minor only (3.11)?** The base image pins minor; that's inherited. Recommendation: let the Playwright base image own Python version; don't second-guess it.
- **Include `tini` as PID 1?** Needed if your container spawns subprocesses that might orphan. `jugnu_runner.py` does spawn Playwright subprocesses, but Cloud Run handles signal propagation cleanly. Recommendation: skip `tini`; add it only if you see zombie processes in prod.

---

## 9. When this handoff is complete

Claude Code has:
1. Created `Dockerfile` and `.dockerignore` at repo root
2. Created stub versions of both entry scripts
3. Added the Makefile targets
4. All gates in §6 pass locally
5. Posted a PR description with image size, layer count, and time-to-build numbers

Only then proceed to `CLAUDE_TERRAFORM.md`.
