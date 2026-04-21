/**
 * @file client.ts (postgres adapter)
 * @description Shared pg connection pool, resolved from DATABASE_URL.
 */

import pg from 'pg';
import { logger } from '../../logger.js';

const { Pool } = pg;
export type PgPool = pg.Pool;

/**
 * Build a pg.Pool from a DATABASE_URL. The URL format accepted here is the
 * standard libpq form (`postgresql://user:pass@host:port/db`) — if the URL
 * came from the Python side it may be prefixed with `postgresql+pg8000://`
 * or `postgresql+psycopg://`, which we strip before passing to `pg`.
 */
export function buildPool(databaseUrl: string): PgPool {
  const cleaned = databaseUrl.replace(/^postgresql\+[a-z0-9_]+:\/\//i, 'postgresql://');
  logger.info({ host: redactHost(cleaned) }, 'creating pg pool');
  const pool = new Pool({
    connectionString: cleaned,
    max: 10,
    idleTimeoutMillis: 30_000,
    connectionTimeoutMillis: 5_000,
  });
  pool.on('error', (err) => {
    logger.error({ err: err.message }, 'pg pool error');
  });
  return pool;
}

function redactHost(url: string): string {
  try {
    const parsed = new URL(url);
    return `${parsed.hostname}:${parsed.port || '5432'}/${parsed.pathname.replace(/^\//, '')}`;
  } catch {
    return '<unparseable>';
  }
}
