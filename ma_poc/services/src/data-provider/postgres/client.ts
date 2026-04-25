/**
 * @file client.ts (postgres adapter)
 * @description Shared pg connection pool.
 *
 * Two resolution paths — gated by env, not by caller:
 *
 *  1. Direct TCP (local dev, proxy sidecar, public IP):
 *       DATABASE_URL=postgresql://user:pass@host:port/db
 *     The URL is handed to `pg.Pool` unchanged (after stripping any
 *     SQLAlchemy-style `+pg8000` / `+psycopg` driver suffix).
 *
 *  2. Cloud SQL Connector + IAM auth (Cloud Run / GKE):
 *       CLOUD_SQL_INSTANCE=project:region:instance
 *       CLOUD_SQL_IP_TYPE=PRIVATE | PUBLIC        (default: PRIVATE)
 *       DATABASE_URL=postgresql://user@/db        (user + db only)
 *     `@google-cloud/cloud-sql-connector` brokers an mTLS socket via
 *     short-lived OAuth tokens; passwords are rejected when
 *     `cloudsql.iam_authentication=on`. This is the Node equivalent of
 *     the Python pg8000 + Cloud SQL Connector path used by
 *     `ma_poc/data_provider/sql/engine.py`.
 */

import pg from 'pg';
import { logger } from '../../logger.js';

const { Pool } = pg;
export type PgPool = pg.Pool;

/**
 * A pg pool plus any resources that need tearing down alongside it —
 * the Cloud SQL connector holds a token-refresh loop that must be
 * stopped explicitly; the direct path has nothing extra.
 */
export interface PooledConnection {
  pool: PgPool;
  close(): Promise<void>;
}

/**
 * Build a pg.Pool from env. Async because the Cloud SQL Connector path
 * resolves TLS + auth lazily via `getOptions()`; callers must await.
 */
export async function buildPool(
  databaseUrl: string,
  env: NodeJS.ProcessEnv = process.env,
): Promise<PooledConnection> {
  if (env.CLOUD_SQL_INSTANCE) {
    return buildCloudSqlPool(databaseUrl, env);
  }
  return buildDirectPool(databaseUrl);
}

function buildDirectPool(databaseUrl: string): PooledConnection {
  // Strip SQLAlchemy-style driver suffix — our Python side writes
  // `postgresql+pg8000://` or `postgresql+psycopg://`, neither of
  // which `pg` understands.
  const cleaned = databaseUrl.replace(/^postgresql\+[a-z0-9_]+:\/\//i, 'postgresql://');
  logger.info({ host: redactHost(cleaned) }, 'creating pg pool (direct)');
  const pool = new Pool({
    connectionString: cleaned,
    max: 10,
    idleTimeoutMillis: 30_000,
    connectionTimeoutMillis: 5_000,
  });
  pool.on('error', (err) => {
    logger.error({ err: err.message }, 'pg pool error');
  });
  return {
    pool,
    close: async () => {
      await pool.end();
    },
  };
}

async function buildCloudSqlPool(
  databaseUrl: string,
  env: NodeJS.ProcessEnv,
): Promise<PooledConnection> {
  // Lazy-imported so local dev does not need the connector package
  // installed / network-available when CLOUD_SQL_INSTANCE is unset.
  const { Connector, IpAddressTypes, AuthTypes } = await import(
    '@google-cloud/cloud-sql-connector'
  );

  const instance = env.CLOUD_SQL_INSTANCE!;
  const ipRaw = (env.CLOUD_SQL_IP_TYPE ?? 'PRIVATE').toUpperCase();
  const ipType = ipRaw === 'PUBLIC' ? IpAddressTypes.PUBLIC : IpAddressTypes.PRIVATE;

  // CLOUD_SQL_AUTH_TYPE chooses between built-in Postgres password auth
  // (PASSWORD — needs DATABASE_URL with `user:pass`) and IAM service-account
  // auth (IAM — needs the principal's ADC token + cloudsql.iam_authentication=on).
  // Default IAM to match the Python engine; fall back to PASSWORD when the
  // instance hasn't been flipped to IAM yet (or the SA isn't fully provisioned).
  const authRaw = (env.CLOUD_SQL_AUTH_TYPE ?? 'IAM').toUpperCase();
  const authType = authRaw === 'PASSWORD' ? AuthTypes.PASSWORD : AuthTypes.IAM;

  const { user, password, database } = parseUserDbAndPassword(databaseUrl);
  if (!user || !database) {
    throw new Error(
      'DATABASE_URL must supply at least user + database when ' +
      `CLOUD_SQL_INSTANCE is set (got ${redactHost(databaseUrl)})`,
    );
  }
  if (authType === AuthTypes.PASSWORD && !password) {
    throw new Error(
      'CLOUD_SQL_AUTH_TYPE=PASSWORD requires a password in DATABASE_URL ' +
      '(postgresql://user:pass@/db). For IAM auth, unset CLOUD_SQL_AUTH_TYPE.',
    );
  }

  const connector = new Connector();
  const clientOpts = await connector.getOptions({
    instanceConnectionName: instance,
    ipType,
    authType,
  });

  logger.info(
    { instance, user, database, ipType: ipRaw, authType: authRaw },
    'creating pg pool (cloud-sql-connector)',
  );

  const pool = new Pool({
    ...clientOpts,
    user,
    database,
    ...(authType === AuthTypes.PASSWORD && password ? { password } : {}),
    // Bumped from 10 → 20 because the API issues 5 round-trips per
    // /api/properties* request (listForRun + propertyState.all + ledger
    // + llm report) and the frontend fires three of these in parallel
    // on page load. With max=10 the pool would back up and the next
    // request would time out before getting a connection — manifesting
    // as ECONNRESET / ECONNREFUSED at the Vite proxy.
    max: 20,
    idleTimeoutMillis: 30_000,
    connectionTimeoutMillis: 10_000,
    // Server-side per-statement guard. A wedged Cloud SQL query would
    // otherwise tie up a pool slot indefinitely. The legacy "load all
    // 4482 JSONB snapshots" path can take >30s under contention; the
    // new fast-path summary query is sub-second. 90s leaves headroom
    // for the slow path while still killing truly stuck queries.
    statement_timeout: 90_000,
    // Cloud SQL IAM access tokens last 60 min. A pooled connection
    // that sat idle for an hour holds an expired token and will fail
    // on the next checkout. Recycle under the TTL so we rotate before
    // the token expires — matches the Python engine's pool_recycle=3000.
    // Harmless on the PASSWORD path; the connector still rotates its
    // ephemeral cert under a similar TTL.
    maxLifetimeSeconds: 3000,
  });
  pool.on('error', (err) => {
    logger.error({ err: err.message }, 'pg pool error');
  });

  return {
    pool,
    close: async () => {
      await pool.end();
      connector.close();
    },
  };
}

/**
 * Extract user + database from a DATABASE_URL without caring about the
 * host/port (the connector overrides both). Works with the
 * SQLAlchemy-style `postgresql+pg8000://` prefix and standard libpq URLs.
 * Percent-decoded — IAM usernames are typically `sa%40proj.iam` on the wire.
 */
function parseUserDbAndPassword(
  databaseUrl: string,
): { user: string; password: string; database: string } {
  const cleaned = databaseUrl.replace(/^postgresql\+[a-z0-9_]+:\/\//i, 'postgresql://');
  // `new URL()` rejects URLs with an empty authority (`postgresql://u@/db`
  // is how Terraform renders Cloud-SQL-only DATABASE_URLs — host+port are
  // left blank because the connector overrides them). Inject a placeholder
  // host so the parser accepts it; we only read user/password/pathname.
  const withHost = cleaned.replace(/^(postgresql:\/\/[^/]*@)\/(.*)$/, '$1placeholder/$2');
  try {
    const parsed = new URL(withHost);
    return {
      user: decodeURIComponent(parsed.username || ''),
      password: decodeURIComponent(parsed.password || ''),
      database: decodeURIComponent(parsed.pathname.replace(/^\//, '') || ''),
    };
  } catch {
    return { user: '', password: '', database: '' };
  }
}

function redactHost(url: string): string {
  try {
    const cleaned = url.replace(/^postgresql\+[a-z0-9_]+:\/\//i, 'postgresql://');
    const parsed = new URL(cleaned);
    return `${parsed.hostname}:${parsed.port || '5432'}/${parsed.pathname.replace(/^\//, '')}`;
  } catch {
    return '<unparseable>';
  }
}
