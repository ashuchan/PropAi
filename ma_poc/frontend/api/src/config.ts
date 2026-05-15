import { config as dotenvConfig } from 'dotenv';
import { isAbsolute, resolve } from 'path';
import { fileURLToPath } from 'url';
import { dirname, join } from 'path';
const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

// ma_poc/ root — 3 levels up from api/src/. Anchors .env and relative DATA_DIR.
const maPocRoot = resolve(__dirname, '..', '..', '..');
dotenvConfig({ path: join(maPocRoot, '.env') });

// Relative DATA_DIR (e.g. "./data" in ma_poc/.env) must resolve against the ma_poc
// root so the API finds the same directory the Python scripts do — not against the
// API process cwd (ma_poc/frontend/api).
function resolveDataDir(): string {
  const raw = process.env.DATA_DIR;
  if (!raw) return join(maPocRoot, 'data');
  return isAbsolute(raw) ? raw : resolve(maPocRoot, raw);
}

export const config = {
  // Cloud Run injects PORT; honour it before the local-dev API_PORT fallback.
  port: parseInt(process.env.PORT || process.env.API_PORT || '3005', 10),
  dataDir: resolveDataDir(),
  corsOrigin: process.env.CORS_ORIGIN || 'http://localhost:5173',
  logLevel: process.env.LOG_LEVEL || 'info',
  schemaVersion: (process.env.SCHEMA_VERSION || 'v1') as 'v1' | 'v2',
  // SPA serving for the Cloud Run single-container deploy. Off in local dev
  // (Vite serves the SPA at :5173 and proxies /api to this server at :3005).
  serveStatic: process.env.SERVE_STATIC === 'true',
  staticDir: process.env.STATIC_DIR || resolve(maPocRoot, 'frontend', 'dist'),
};
