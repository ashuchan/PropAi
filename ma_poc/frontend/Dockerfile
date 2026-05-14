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
FROM node:20-slim AS builder

WORKDIR /build

# Install deps first so the layer caches across source-only changes.
# Each subpackage has its own lockfile; using `npm ci` enforces the lock.
COPY ma_poc/services/package.json ma_poc/services/package-lock.json* ./services/
COPY ma_poc/frontend/package.json ma_poc/frontend/package-lock.json* ./frontend/
COPY ma_poc/frontend/api/package.json ma_poc/frontend/api/package-lock.json* ./frontend/api/

RUN cd services && npm ci --no-audit --no-fund
RUN cd frontend && npm ci --no-audit --no-fund
RUN cd frontend/api && npm ci --no-audit --no-fund

# Sources — copied after deps so source edits don't bust npm-install layers.
COPY ma_poc/services ./services
COPY ma_poc/frontend ./frontend

# Build the SPA. `tsc && vite build` is the npm script; output is
# /build/frontend/dist (static files + index.html, ready for express.static).
RUN cd frontend && npm run build

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

# Re-install production deps in the runtime stage so dev deps (tsx is a
# `dependencies` entry, so it stays; vitest etc. drop out) are not shipped.
COPY --from=builder /build/services/package.json /build/services/package-lock.json* /app/ma_poc/services/
COPY --from=builder /build/frontend/api/package.json /build/frontend/api/package-lock.json* /app/ma_poc/frontend/api/

RUN cd /app/ma_poc/services && npm ci --omit=dev --no-audit --no-fund \
 && cd /app/ma_poc/frontend/api && npm ci --omit=dev --no-audit --no-fund \
 && npm cache clean --force

# TS sources (runtime — tsx loads them on demand)
COPY --from=builder /build/services/src /app/ma_poc/services/src
COPY --from=builder /build/services/tsconfig.json /app/ma_poc/services/tsconfig.json
COPY --from=builder /build/frontend/api/src /app/ma_poc/frontend/api/src
COPY --from=builder /build/frontend/api/tsconfig.json /app/ma_poc/frontend/api/tsconfig.json

# Built SPA (static-only at runtime — no source needed)
COPY --from=builder /build/frontend/dist /app/ma_poc/frontend/dist

RUN chown -R propai:propai /app

USER propai

# Cloud Run injects PORT (default 8080). config.ts reads PORT then API_PORT.
EXPOSE 8080

# tsx resolves `.js` imports → `.ts` source per moduleResolution=bundler.
# Identical to `npm start` in api/package.json — prod ≈ dev path.
CMD ["npx", "--no-install", "tsx", "src/server.ts"]
