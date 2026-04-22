FROM mcr.microsoft.com/playwright/python:v1.58.0-noble

# Non-root user for runtime — Playwright image ships with 'pwuser'; reuse it.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Layer 1: system deps (rarely change — cached aggressively)
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Layer 2: Python deps (change when requirements.txt changes)
COPY ma_poc/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Layer 3: source code (changes on every commit — last)
COPY ma_poc/ ./ma_poc/

# Layer 4: drop privileges
USER pwuser

# No CMD — ENTRYPOINT is provided per-job by Cloud Run (container command override).
# Default entrypoint is a sanity-check that imports pass.
ENTRYPOINT ["python", "-c", "import ma_poc; print('jugnu image OK')"]
