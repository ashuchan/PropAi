/**
 * @file UnitStore.ts (json-file adapter)
 * @description Reads data/state/unit_index.json (V1-shaped) and produces
 * UnitStateRecord dicts with V1 alias fields populated.
 */

import type { IUnitStore, UnitStateRecord } from '../contracts.js';
import { readJsonFile, statePath } from './dataLoader.js';

interface RawUnit {
  unit_id?: string;
  market_rent_low?: number | null;
  market_rent_high?: number | null;
  available_date?: string | null;
  // 2026-05-20: producer-literal availability string. Optional because
  // pre-2026-05-20 state files don't carry it.
  available_date_raw?: string | null;
  concessions?: unknown;
  bedrooms?: number | null;
  bathrooms?: number | null;
  sqft?: number | null;
  floor_plan_name?: string | null;
  availability_status?: string | null;
  first_seen_date?: string | null;
  last_seen_at?: string | null;
  carryforward_days?: number | null;
  disappeared_since?: string | null;
  last_absent_date?: string | null;
  changed_fields?: string[];
  [key: string]: unknown;
}

type RawUnitIndex = Record<string, Record<string, RawUnit>>;

export class JsonFileUnitStore implements IUnitStore {
  constructor(private readonly dataDir: string) {}

  async listStateForProperty(canonicalId: string): Promise<UnitStateRecord[]> {
    const index = (await readJsonFile<RawUnitIndex>(statePath(this.dataDir, 'unit_index.json'))) ?? {};
    const units = index[canonicalId];
    if (!units) return [];
    return Object.entries(units).map(([uid, raw]) => this.toRecord(canonicalId, uid, raw));
  }

  async count(): Promise<number> {
    const index = (await readJsonFile<RawUnitIndex>(statePath(this.dataDir, 'unit_index.json'))) ?? {};
    let n = 0;
    for (const units of Object.values(index)) n += Object.keys(units).length;
    return n;
  }

  private toRecord(canonicalId: string, unitId: string, raw: RawUnit): UnitStateRecord {
    const {
      unit_id: _uid,
      market_rent_low, market_rent_high, available_date, available_date_raw, concessions,
      bedrooms, bathrooms, sqft, floor_plan_name, availability_status,
      first_seen_date, last_seen_at,
      // Legacy field — no longer on the DTO / DB schema.
      last_seen_date: _legacyLSD,
      carryforward_days, disappeared_since, last_absent_date, changed_fields,
      ...rest
    } = raw;
    return {
      canonicalId,
      unitId,
      marketRentLow: market_rent_low ?? null,
      marketRentHigh: market_rent_high ?? null,
      availableDate: available_date ?? null,
      availableDateRaw: available_date_raw ?? null,
      concessions: concessions ?? null,
      bedrooms: bedrooms ?? null,
      bathrooms: bathrooms ?? null,
      sqft: sqft ?? null,
      floorPlanName: floor_plan_name ?? null,
      availabilityStatus: availability_status ?? null,
      firstSeenDate: first_seen_date ?? null,
      lastSeenAt: last_seen_at ?? null,
      carryforwardDays: carryforward_days ?? null,
      disappearedSince: disappeared_since ?? null,
      lastAbsentDate: last_absent_date ?? null,
      changedFields: changed_fields ?? [],
      extra: rest,
    };
  }
}
