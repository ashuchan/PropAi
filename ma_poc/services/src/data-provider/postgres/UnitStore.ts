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
  floor_plan_name_provenance: string | null;
  source_unit_id: string | null;
  canonical_unit_id: string | null;
  unit_name: string | null;
  floor: string | null;
  building: string | null;
  building_id: string | null;
  building_id_source: string | null;
  area: number | null;
  area_sqft: number | null;
  area_is_published: boolean | null;
  area_low: number | null;
  area_high: number | null;
  area_range: string | null;
  area_range_raw: string | null;
  area_value_type: string | null;
  area_provenance: string | null;
  area_source_url: string | null;
  rent_low: number | null;
  rent_high: number | null;
  rent_range: string | null;
  rent_range_raw: string | null;
  rent_is_range: boolean | null;
  rent_provenance: string | null;
  date_captured: string | null;
  available_date: string | null;
  available_date_raw: string | null;
  availability_date_provenance: string | null;
  availability_status: string | null;
  lease_term: number | null;
  move_in_date: string | null;
  extraction_tier: string | null;
  source_ids: Record<string, unknown> | null;
  source_response_sha256: string | null;
  source_response_url: string | null;
  source_record_locator: string | null;
  source_parent_record_locator: string | null;
  source_asset_url: string | null;
  source_asset_sha256: string | null;
  identity_quality: string | null;
  unit_id_aliases: string[] | null;
  unit_id_alias_sources: Record<string, unknown>[] | null;
  unit_history_key: string | null;
  unit_history_key_basis: string | null;
  unit_history_key_quality: string | null;
  unit_history_key_version: string | null;
  first_seen_date: string | null;
  last_seen_at: string | null;
  carryforward_days: number | null;
  disappeared_since: string | null;
  last_absent_date: string | null;
  concessions: unknown;
  amenities: unknown;
  changed_fields: string[] | null;
  data_sha256: string | null;
  extra: Record<string, unknown> | null;
}

const COLS = `
  canonical_id, unit_id, beds, baths, floor_plan_name, floor_plan_name_provenance,
  source_unit_id, canonical_unit_id, unit_name, floor, building, building_id,
  building_id_source, area, area_sqft, area_is_published, area_low, area_high,
  area_range, area_range_raw, area_value_type, area_provenance, area_source_url,
  rent_low, rent_high, rent_range, rent_range_raw, rent_is_range, rent_provenance,
  date_captured, available_date, available_date_raw,
  availability_date_provenance, availability_status, lease_term, move_in_date,
  extraction_tier, source_ids, source_response_sha256, source_response_url,
  source_record_locator, source_parent_record_locator, source_asset_url,
  source_asset_sha256, identity_quality, unit_id_aliases, unit_id_alias_sources,
  unit_history_key, unit_history_key_basis,
  unit_history_key_quality, unit_history_key_version,
  first_seen_date, last_seen_at,
  carryforward_days, disappeared_since, last_absent_date,
  concessions, amenities, changed_fields, data_sha256, extra
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

  async count(): Promise<number> {
    const { rows } = await this.pool.query<{ n: string }>('select count(*)::text as n from units');
    return Number(rows[0]?.n ?? 0);
  }

  private toRecord(row: Row): UnitStateRecord {
    return {
      canonicalId: row.canonical_id,
      unitId: row.unit_id,
      // V2 direct
      beds: row.beds,
      baths: row.baths,
      area: row.area,
      areaSqft: row.area_sqft,
      areaIsPublished: row.area_is_published,
      areaLow: row.area_low,
      areaHigh: row.area_high,
      areaRange: row.area_range,
      areaRangeRaw: row.area_range_raw,
      areaValueType: row.area_value_type,
      areaProvenance: row.area_provenance,
      areaSourceUrl: row.area_source_url,
      rentLow: row.rent_low,
      rentHigh: row.rent_high,
      rentRange: row.rent_range,
      rentRangeRaw: row.rent_range_raw,
      rentIsRange: row.rent_is_range,
      rentProvenance: row.rent_provenance,
      dateCaptured: row.date_captured,
      leaseTerm: row.lease_term,
      moveInDate: row.move_in_date,
      sourceUnitId: row.source_unit_id,
      canonicalUnitId: row.canonical_unit_id,
      unitName: row.unit_name,
      floor: row.floor,
      building: row.building,
      buildingId: row.building_id,
      buildingIdSource: row.building_id_source,
      floorPlanNameProvenance: row.floor_plan_name_provenance,
      availableDateRaw: row.available_date_raw,
      availabilityDateProvenance: row.availability_date_provenance,
      extractionTier: row.extraction_tier,
      sourceIds: row.source_ids,
      sourceResponseSha256: row.source_response_sha256,
      sourceResponseUrl: row.source_response_url,
      sourceRecordLocator: row.source_record_locator,
      sourceParentRecordLocator: row.source_parent_record_locator,
      sourceAssetUrl: row.source_asset_url,
      sourceAssetSha256: row.source_asset_sha256,
      identityQuality: row.identity_quality,
      unitIdAliases: row.unit_id_aliases ?? [],
      unitIdAliasSources: row.unit_id_alias_sources ?? [],
      unitHistoryKey: row.unit_history_key,
      unitHistoryKeyBasis: row.unit_history_key_basis,
      unitHistoryKeyQuality: row.unit_history_key_quality,
      unitHistoryKeyVersion: row.unit_history_key_version,
      // V1 aliases mirrored from v2 so legacy readers still work.
      bedrooms: row.beds,
      bathrooms: row.baths,
      sqft: row.area,
      marketRentLow: row.rent_low,
      marketRentHigh: row.rent_high,
      // Shared
      availableDate: row.available_date,
      availabilityStatus: row.availability_status,
      concessions: row.concessions,
      amenities: row.amenities,
      floorPlanName: row.floor_plan_name,
      firstSeenDate: row.first_seen_date,
      lastSeenAt: row.last_seen_at,
      carryforwardDays: row.carryforward_days,
      disappearedSince: row.disappeared_since,
      lastAbsentDate: row.last_absent_date,
      changedFields: row.changed_fields ?? [],
      dataSha256: row.data_sha256,
      extra: row.extra ?? {},
    };
  }
}
