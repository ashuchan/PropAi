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
  port: parseInt(process.env.API_PORT || '3001', 10),
  dataDir: resolveDataDir(),
  corsOrigin: process.env.CORS_ORIGIN || 'http://localhost:5173',
  logLevel: process.env.LOG_LEVEL || 'info',
  schemaVersion: (process.env.SCHEMA_VERSION || 'v1') as 'v1' | 'v2',
};
