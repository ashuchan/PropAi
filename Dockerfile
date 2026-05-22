# ── Stage 1: builder — install Python deps into an isolated venv ─────────────
FROM python:3.14-slim-bookworm AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY ma_poc/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# ── Stage 2: runtime — slim base + chromium only + prebuilt venv + app ───────
FROM python:3.14-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH="/opt/venv/bin:$PATH" \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# Non-root runtime user, created up front so later COPY --chown works.
RUN groupadd --system --gid 1001 pwuser \
    && useradd --system --uid 1001 --gid pwuser --create-home pwuser

# Python environment from the builder stage.
COPY --from=builder /opt/venv /opt/venv

# All root-only work fused into a single layer so the chromium download (~350 MB)
# and its final chown don't produce a second full-size layer. Firefox and WebKit
# are intentionally omitted — scraper/browser.py only launches Chromium.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl \
 && patchright install --with-deps chromium \
 && chown -R pwuser:pwuser /ms-playwright \
 && rm -rf /var/lib/apt/lists/* /tmp/* /root/.cache

# Source code — ownership set during copy so no duplicate layer.
COPY --chown=pwuser:pwuser ma_poc/ ./ma_poc/

# Regenerate the floor-plan mapping artifact against whatever CSV pair was
# baked into the image. Avoids stale-mapping drift if the CSVs are updated
# without re-running the verifier locally. Runs as root so the artifact
# inherits the COPY ownership; verifier itself is read-only on the inputs.
# Failure here is fatal — a bad mapping should fail the build, not ship.
#
# validate_deployment.py runs immediately after to confirm the artifact
# is non-null, the FloorplanCatalog loads with ≥1 000 indexed properties,
# all four prompt templates are present with the {known_floor_plans}
# placeholder intact, and the modules wired into scrape_jugnu still
# import cleanly under the runtime venv. Any failure exits the build.
RUN python -m ma_poc.scripts.checks.csv_mapping \
 && chown pwuser:pwuser /app/ma_poc/config/csv_floorplan_mapping.json \
 && python -m ma_poc.scripts.checks.deployment

# Pre-create the profiles directory so the runtime profile_store mkdir
# doesn't trip on a parent-dir ownership mismatch when running as pwuser.
# (.dockerignore excludes config/profiles/, so without this step the dir
# is missing at runtime and the mkdir lands on a root-owned parent.)
RUN mkdir -p /app/ma_poc/config/profiles \
 && chown -R pwuser:pwuser /app/ma_poc/config

USER pwuser

# No CMD — ENTRYPOINT is provided per-job by Cloud Run (container command override).
# Default entrypoint is a sanity-check that imports pass.
ENTRYPOINT ["python", "-c", "import ma_poc; print('jugnu image OK')"]
