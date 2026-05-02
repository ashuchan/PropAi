/**
 * @file provider.ts (postgres adapter)
 * @description Composite DataProvider wrapping a pg.Pool with five stores.
 *
 * Constructed via the async static `create()` — pool building is async
 * to accommodate the Cloud SQL Connector path, which resolves TLS +
 * IAM auth via `getOptions()` before the first query.
 */

import type { IDataProvider } from '../contracts.js';
import { PgArtifactStore } from './ArtifactStore.js';
import { buildPool, type PgPool, type PooledConnection } from './client.js';
import { PgFloorPlanComparisonStore } from './FloorPlanComparisonStore.js';
import { PgPropertyStateStore } from './PropertyStateStore.js';
import { PgPropertyStore } from './PropertyStore.js';
import { PgRunStore } from './RunStore.js';
import { PgUnitStore } from './UnitStore.js';

export class PostgresDataProvider implements IDataProvider {
  readonly name = 'postgres';
  readonly properties: PgPropertyStore;
  readonly propertyState: PgPropertyStateStore;
  readonly units: PgUnitStore;
  readonly runs: PgRunStore;
  readonly artifacts: PgArtifactStore;
  readonly floorPlanComparisons: PgFloorPlanComparisonStore;
  private readonly pool: PgPool;
  private readonly closePool: () => Promise<void>;

  private constructor(conn: PooledConnection) {
    this.pool = conn.pool;
    this.closePool = conn.close;
    this.properties = new PgPropertyStore(this.pool);
    this.propertyState = new PgPropertyStateStore(this.pool);
    this.units = new PgUnitStore(this.pool);
    this.runs = new PgRunStore(this.pool);
    this.artifacts = new PgArtifactStore(this.pool);
    this.floorPlanComparisons = new PgFloorPlanComparisonStore(this.pool);
  }

  /**
   * Resolve the pool (direct or Cloud SQL) and wire the stores.
   */
  static async create(
    databaseUrl: string,
    env?: NodeJS.ProcessEnv,
  ): Promise<PostgresDataProvider> {
    const conn = await buildPool(databaseUrl, env);
    return new PostgresDataProvider(conn);
  }

  async close(): Promise<void> {
    await this.closePool();
  }
}
