# ── Stage 1: builder — install Python deps into an isolated venv ─────────────
FROM python:3.11-slim-bookworm AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY ma_poc/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# ── Stage 2: runtime — slim base + chromium only + prebuilt venv + app ───────
FROM python:3.11-slim-bookworm

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
 && playwright install --with-deps chromium \
 && chown -R pwuser:pwuser /ms-playwright \
 && rm -rf /var/lib/apt/lists/* /tmp/* /root/.cache

# Source code — ownership set during copy so no duplicate layer.
COPY --chown=pwuser:pwuser ma_poc/ ./ma_poc/

USER pwuser

# No CMD — ENTRYPOINT is provided per-job by Cloud Run (container command override).
# Default entrypoint is a sanity-check that imports pass.
ENTRYPOINT ["python", "-c", "import ma_poc; print('jugnu image OK')"]
