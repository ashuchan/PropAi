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

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv

# Chromium + its apt dependencies. --with-deps must run as root; Firefox and
# WebKit are intentionally omitted — scraper/browser.py only launches Chromium.
RUN playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --system --gid 1001 pwuser \
    && useradd --system --uid 1001 --gid pwuser --create-home pwuser \
    && chown -R pwuser:pwuser /ms-playwright

COPY ma_poc/ ./ma_poc/
RUN chown -R pwuser:pwuser /app

USER pwuser

# No CMD — ENTRYPOINT is provided per-job by Cloud Run (container command override).
# Default entrypoint is a sanity-check that imports pass.
ENTRYPOINT ["python", "-c", "import ma_poc; print('jugnu image OK')"]
