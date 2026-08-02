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
  concessions?: unknown;
  bedrooms?: number | null;
  bathrooms?: number | null;
  sqft?: number | null;
  floor_plan_name?: string | null;
  floor_plan_name_provenance?: string | null;
  source_unit_id?: string | null;
  canonical_unit_id?: string | null;
  unit_name?: string | null;
  floor?: string | null;
  building?: string | null;
  building_id?: string | null;
  building_id_source?: string | null;
  area_sqft?: number | null;
  area_is_published?: boolean | null;
  area_low?: number | null;
  area_high?: number | null;
  area_range?: string | null;
  area_range_raw?: string | null;
  area_value_type?: string | null;
  area_provenance?: string | null;
  area_source_url?: string | null;
  rent_range?: string | null;
  rent_range_raw?: string | null;
  rent_is_range?: boolean | null;
  rent_provenance?: string | null;
  available_date_raw?: string | null;
  availability_date_provenance?: string | null;
  availability_status?: string | null;
  extraction_tier?: string | null;
  source_ids?: Record<string, unknown> | null;
  source_response_sha256?: string | null;
  source_response_url?: string | null;
  source_record_locator?: string | null;
  source_parent_record_locator?: string | null;
  source_asset_url?: string | null;
  source_asset_sha256?: string | null;
  identity_quality?: string | null;
  unit_id_aliases?: string[];
  unit_id_alias_sources?: Record<string, unknown>[];
  unit_history_key?: string | null;
  unit_history_key_basis?: string | null;
  unit_history_key_quality?: string | null;
  unit_history_key_version?: string | null;
  data_sha256?: string | null;
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
      market_rent_low, market_rent_high, available_date, concessions,
      bedrooms, bathrooms, sqft, floor_plan_name, floor_plan_name_provenance,
      source_unit_id, canonical_unit_id, unit_name, floor, building,
      building_id, building_id_source, area_sqft, area_is_published,
      area_low, area_high, area_range, area_range_raw, area_value_type,
      area_provenance, area_source_url, rent_range, rent_range_raw, rent_is_range,
      rent_provenance,
      available_date_raw, availability_date_provenance, availability_status,
      extraction_tier, source_ids, unit_history_key, unit_history_key_basis,
      source_response_sha256, source_response_url, source_record_locator,
      source_parent_record_locator, source_asset_url, source_asset_sha256,
      identity_quality, unit_id_aliases, unit_id_alias_sources,
      unit_history_key_quality, unit_history_key_version, data_sha256,
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
      concessions: concessions ?? null,
      bedrooms: bedrooms ?? null,
      bathrooms: bathrooms ?? null,
      sqft: sqft ?? null,
      floorPlanName: floor_plan_name ?? null,
      floorPlanNameProvenance: floor_plan_name_provenance ?? null,
      sourceUnitId: source_unit_id ?? null,
      canonicalUnitId: canonical_unit_id ?? null,
      unitName: unit_name ?? null,
      floor: floor ?? null,
      building: building ?? null,
      buildingId: building_id ?? null,
      buildingIdSource: building_id_source ?? null,
      areaSqft: area_sqft ?? null,
      areaIsPublished: area_is_published ?? null,
      areaLow: area_low ?? null,
      areaHigh: area_high ?? null,
      areaRange: area_range ?? null,
      areaRangeRaw: area_range_raw ?? null,
      areaValueType: area_value_type ?? null,
      areaProvenance: area_provenance ?? null,
      areaSourceUrl: area_source_url ?? null,
      rentRange: rent_range ?? null,
      rentRangeRaw: rent_range_raw ?? null,
      rentIsRange: rent_is_range ?? null,
      rentProvenance: rent_provenance ?? null,
      availableDateRaw: available_date_raw ?? null,
      availabilityDateProvenance: availability_date_provenance ?? null,
      availabilityStatus: availability_status ?? null,
      extractionTier: extraction_tier ?? null,
      sourceIds: source_ids ?? null,
      sourceResponseSha256: source_response_sha256 ?? null,
      sourceResponseUrl: source_response_url ?? null,
      sourceRecordLocator: source_record_locator ?? null,
      sourceParentRecordLocator: source_parent_record_locator ?? null,
      sourceAssetUrl: source_asset_url ?? null,
      sourceAssetSha256: source_asset_sha256 ?? null,
      identityQuality: identity_quality ?? null,
      unitIdAliases: unit_id_aliases ?? [],
      unitIdAliasSources: unit_id_alias_sources ?? [],
      unitHistoryKey: unit_history_key ?? null,
      unitHistoryKeyBasis: unit_history_key_basis ?? null,
      unitHistoryKeyQuality: unit_history_key_quality ?? null,
      unitHistoryKeyVersion: unit_history_key_version ?? null,
      dataSha256: data_sha256 ?? null,
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
