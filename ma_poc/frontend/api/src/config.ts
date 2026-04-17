import { resolve } from 'path';
import { fileURLToPath } from 'url';
import { dirname, join } from 'path';
const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
export const config = {
  port: parseInt(process.env.API_PORT || '3001', 10),
  dataDir: process.env.DATA_DIR || resolve(join(__dirname, '..', '..', '..', 'data')),
  corsOrigin: process.env.CORS_ORIGIN || 'http://localhost:5173',
  logLevel: process.env.LOG_LEVEL || 'info',
  schemaVersion: (process.env.SCHEMA_VERSION || 'v1') as 'v1' | 'v2',
};
