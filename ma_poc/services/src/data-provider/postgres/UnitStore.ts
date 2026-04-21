/**
 * @file UnitStore.ts (postgres adapter)
 * @description Reads the V2-strict `units` table. Populates V2 aliases on
 * UnitStateRecord.
 */

import type { IUnitStore, UnitStateRecord } from '../contracts.js';
import type { PgPool } from './client.js';

interface Row {
  canonical_id: string;
  unit_id: string;
  beds: number | null;
  baths: number | null;
  floor_plan_name: string | null;
  area: number | null;
  rent_low: number | null;
  rent_high: number | null;
  date_captured: string | null;
  available_date: string | null;
  lease_term: number | null;
  move_in_date: string | null;
  first_seen_date: string | null;
  last_seen_at: string | null;
  carryforward_days: number | null;
  disappeared_since: string | null;
  last_absent_date: string | null;
  concessions: unknown;
  changed_fields: string[] | null;
  extra: Record<string, unknown> | null;
}

const COLS = `
  canonical_id, unit_id, beds, baths, floor_plan_name, area,
  rent_low, rent_high, date_captured, available_date, lease_term, move_in_date,
  first_seen_date, last_seen_at,
  carryforward_days, disappeared_since, last_absent_date,
  concessions, changed_fields, extra
`;

export class PgUnitStore implements IUnitStore {
  constructor(private readonly pool: PgPool) {}

  async listStateForProperty(canonicalId: string): Promise<UnitStateRecord[]> {
    const { rows } = await this.pool.query<Row>(
      `select ${COLS} from units where canonical_id = $1 order by unit_id`,
      [canonicalId],
    );
    return rows.map(this.toRecord);
  }

  private toRecord(row: Row): UnitStateRecord {
    return {
      canonicalId: row.canonical_id,
      unitId: row.unit_id,
      // V2 direct
      beds: row.beds,
      baths: row.baths,
      area: row.area,
      rentLow: row.rent_low,
      rentHigh: row.rent_high,
      dateCaptured: row.date_captured,
      leaseTerm: row.lease_term,
      moveInDate: row.move_in_date,
      // V1 aliases mirrored from v2 so legacy readers still work.
      bedrooms: row.beds,
      bathrooms: row.baths,
      sqft: row.area,
      marketRentLow: row.rent_low,
      marketRentHigh: row.rent_high,
      // Shared
      availableDate: row.available_date,
      concessions: row.concessions,
      floorPlanName: row.floor_plan_name,
      firstSeenDate: row.first_seen_date,
      lastSeenAt: row.last_seen_at,
      carryforwardDays: row.carryforward_days,
      disappearedSince: row.disappeared_since,
      lastAbsentDate: row.last_absent_date,
      changedFields: row.changed_fields ?? [],
      extra: row.extra ?? {},
    };
  }
}
