# syntax=docker/dockerfile:1.7

# ── Stage 1: builder — install all deps, build the SPA ────────────────────────
# Builds three node packages that ship together in one image:
#   - ma_poc/services         (TS source, loaded at runtime by tsx)
#   - ma_poc/frontend         (Vite build → static dist/)
#   - ma_poc/frontend/api     (Express + TS, runtime entrypoint)
#
# The api server imports services via the relative path
# `../../../services/src/factory.js` (api/src/server.ts:13) — so the runtime
# image MUST preserve the `ma_poc/{services,frontend,frontend/api}` directory
# layout. We keep services as TS source (loaded by tsx) instead of building
# it to dist/ because that's what the in-tree `npm start` script does, and
# duplicating that contract here keeps prod ≈ dev.
#
# Why ONE COPY per source path + cache mount, not the
# "package.json early → npm ci → COPY source" pattern: BuildKit (5.x in CI)
# was failing the second COPY of an already-partially-copied path with
# "failed to compute cache key … not found" — a known interaction between
# granular file COPYs and a subsequent whole-directory COPY of the same
# source. `COPY --link` + cache mounts on /root/.npm gets us back the
# performance the granular pattern was buying without that pitfall.

FROM node:20-slim AS builder

WORKDIR /build

# --link decouples this layer from its parent in BuildKit's storage model,
# avoiding the cache-key churn that the previous granular+bulk COPY pattern
# tripped on. Dockerignore at ma_poc/frontend/Dockerfile.dockerignore keeps
# node_modules/dist/git out of the upload.
COPY --link ma_poc/services ./services
COPY --link ma_poc/frontend ./frontend

# All three installs share the same /root/.npm cache mount, so a re-run
# after a source change skips redownloading any package it has seen before.
# Fused into one RUN so npm doesn't have to warm its cache three times.
RUN --mount=type=cache,target=/root/.npm \
    cd /build/services && npm ci --no-audit --no-fund \
 && cd /build/frontend && npm ci --no-audit --no-fund \
 && cd /build/frontend/api && npm ci --no-audit --no-fund

# Build the SPA. `tsc && vite build` is the npm script; output is
# /build/frontend/dist (static files + index.html, ready for express.static).
RUN cd /build/frontend && npm run build

# ── Stage 2: runtime — slim node + production deps + built SPA ────────────────
FROM node:20-slim

ENV NODE_ENV=production \
    SERVE_STATIC=true \
    STATIC_DIR=/app/ma_poc/frontend/dist \
    DATA_DIR=/app/ma_poc/data

WORKDIR /app/ma_poc/frontend/api

# Non-root runtime user.
RUN groupadd --system --gid 1001 propai \
 && useradd --system --uid 1001 --gid propai --create-home propai

# Re-install production-only deps in the runtime stage so dev deps (vitest,
# etc.) don't ship. tsx is in `dependencies` and survives --omit=dev.
COPY --link --from=builder /build/services/package.json /build/services/package-lock.json* /app/ma_poc/services/
COPY --link --from=builder /build/frontend/api/package.json /build/frontend/api/package-lock.json* /app/ma_poc/frontend/api/

RUN --mount=type=cache,target=/root/.npm \
    cd /app/ma_poc/services && npm ci --omit=dev --no-audit --no-fund \
 && cd /app/ma_poc/frontend/api && npm ci --omit=dev --no-audit --no-fund

# TS sources (runtime — tsx loads them on demand)
COPY --link --from=builder /build/services/src /app/ma_poc/services/src
COPY --link --from=builder /build/services/tsconfig.json /app/ma_poc/services/tsconfig.json
COPY --link --from=builder /build/frontend/api/src /app/ma_poc/frontend/api/src
COPY --link --from=builder /build/frontend/api/tsconfig.json /app/ma_poc/frontend/api/tsconfig.json

# Built SPA (static-only at runtime — no source needed)
COPY --link --from=builder /build/frontend/dist /app/ma_poc/frontend/dist

RUN chown -R propai:propai /app

USER propai

# Cloud Run injects PORT (default 8080). config.ts reads PORT then API_PORT.
EXPOSE 8080

# tsx resolves `.js` imports → `.ts` source per moduleResolution=bundler.
# Identical to `npm start` in api/package.json — prod ≈ dev path.
CMD ["npx", "--no-install", "tsx", "src/server.ts"]
