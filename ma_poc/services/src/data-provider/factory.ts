/**
 * @file factory.ts (data-provider)
 * @description Reads DATA_PROVIDER env var and returns the matching provider.
 *
 * Accepted values (matches the Python side):
 *   - filesystem (default)  → JsonFileDataProvider
 *   - postgres              → PostgresDataProvider  (requires DATABASE_URL)
 *                             When CLOUD_SQL_INSTANCE is also set, the pool
 *                             is brokered through the Cloud SQL Connector
 *                             with IAM auth — mirrors the Python engine's
 *                             _make_cloud_sql_engine() branch.
 *
 * sqlite / dual are not implemented on the TS side yet; they'll throw.
 */

import type { IDataProvider } from './contracts.js';
import { JsonFileDataProvider } from './json-file/provider.js';
import { PostgresDataProvider } from './postgres/provider.js';
import { logger } from '../logger.js';

export type ProviderName = 'filesystem' | 'postgres' | 'sqlite' | 'dual';

export interface ProviderConfig {
  /** Value of DATA_PROVIDER env var (normalised to lowercase). */
  provider: ProviderName;
  /** Absolute path to the data directory (needed by filesystem). */
  dataDir: string;
  /** DATABASE_URL (needed by postgres). */
  databaseUrl?: string;
  /** Full env snapshot — threaded through so the postgres adapter can
   *  read CLOUD_SQL_INSTANCE / CLOUD_SQL_IP_TYPE. */
  env?: NodeJS.ProcessEnv;
}

export function resolveProviderConfig(
  dataDir: string,
  env: NodeJS.ProcessEnv = process.env,
): ProviderConfig {
  const raw = (env.DATA_PROVIDER ?? 'filesystem').trim().toLowerCase();
  if (!['filesystem', 'postgres', 'sqlite', 'dual'].includes(raw)) {
    logger.warn({ raw }, 'unknown DATA_PROVIDER, falling back to filesystem');
    return { provider: 'filesystem', dataDir, env };
  }
  return {
    provider: raw as ProviderName,
    dataDir,
    databaseUrl: env.DATABASE_URL,
    env,
  };
}

export async function createDataProvider(cfg: ProviderConfig): Promise<IDataProvider> {
  switch (cfg.provider) {
    case 'filesystem':
      return new JsonFileDataProvider(cfg.dataDir);
    case 'postgres': {
      if (!cfg.databaseUrl) {
        throw new Error(
          'DATA_PROVIDER=postgres requires DATABASE_URL to be set. ' +
          'Example: postgresql://user:pass@host:5432/db',
        );
      }
      return await PostgresDataProvider.create(cfg.databaseUrl, cfg.env);
    }
    case 'sqlite':
    case 'dual':
      throw new Error(
        `DATA_PROVIDER=${cfg.provider} is not yet implemented for the API layer. ` +
        `Use 'filesystem' or 'postgres'.`,
      );
  }
}
