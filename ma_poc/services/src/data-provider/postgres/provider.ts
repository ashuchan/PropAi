/**
 * @file provider.ts (postgres adapter)
 * @description Composite DataProvider wrapping a pg.Pool with five stores.
 */

import type { IDataProvider } from '../contracts.js';
import { PgArtifactStore } from './ArtifactStore.js';
import { buildPool, type PgPool } from './client.js';
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
  private readonly pool: PgPool;

  constructor(databaseUrl: string) {
    this.pool = buildPool(databaseUrl);
    this.properties = new PgPropertyStore(this.pool);
    this.propertyState = new PgPropertyStateStore(this.pool);
    this.units = new PgUnitStore(this.pool);
    this.runs = new PgRunStore(this.pool);
    this.artifacts = new PgArtifactStore(this.pool);
  }

  async close(): Promise<void> {
    await this.pool.end();
  }
}
