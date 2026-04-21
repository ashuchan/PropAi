/**
 * @file PropertyStore.ts (postgres adapter)
 * @description Reads per-run property payloads from the `property_snapshots`
 * table. Rows are preserved in `ordinal` order, matching the original
 * properties.json sequence.
 */

import type { PgPool } from './client.js';
import type { IPropertyStore, RawRunProperty } from '../contracts.js';

export class PgPropertyStore implements IPropertyStore {
  constructor(private readonly pool: PgPool) {}

  async listForRun(runDate: string): Promise<RawRunProperty[]> {
    const { rows } = await this.pool.query<{ payload: RawRunProperty }>(
      `select payload from property_snapshots
         where run_date = $1
         order by ordinal asc`,
      [runDate],
    );
    return rows.map((r) => r.payload);
  }

  async getForRun(runDate: string, canonicalId: string): Promise<RawRunProperty | null> {
    const { rows } = await this.pool.query<{ payload: RawRunProperty }>(
      `select payload from property_snapshots
         where run_date = $1 and canonical_id = $2
         limit 1`,
      [runDate, canonicalId],
    );
    return rows[0]?.payload ?? null;
  }
}
