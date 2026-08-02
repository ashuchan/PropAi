/**
 * @file UnitService.ts
 * @description Impl-agnostic unit service. Finds a property in the latest
 * run payload via the provider, then transforms the nested units. Handles
 * both V1 and V2 unit shapes.
 */

import type { IDataProvider } from '../data-provider/contracts.js';
import type { IUnitService } from '../interfaces/IUnitService.js';
import type { Unit } from '../types/property.js';
import type { FloorPlanGroup, UnitHistoryEntry } from '../types/unit.js';

interface RawV1Unit {
  unit_id: string;
  market_rent_low: number;
  market_rent_high: number;
  available_date: string;
  lease_link: string;
  concessions: string | null;
  amenities: string | null;
  floorplan_image_url?: string | null;
}

interface RawV2Unit {
  unit_id: string | null;
  source_unit_id?: string | null;
  canonical_unit_id?: string | null;
  unit_history_key?: string | null;
  unit_history_key_basis?: string | null;
  unit_history_key_quality?: string | null;
  unit_history_key_version?: string | null;
  beds: number;
  baths: number;
  floor_plan_name: string | null;
  floor_plan_id?: string | null;
  floor_plan_name_provenance?: string | null;
  unit_name?: string | null;
  floor?: string | null;
  building?: string | null;
  building_id?: string | null;
  building_id_source?: string | null;
  area: number;
  area_sqft?: number | null;
  area_is_published?: boolean | null;
  area_low?: number | null;
  area_high?: number | null;
  area_range?: string | null;
  area_range_raw?: string | null;
  area_value_type?: string | null;
  area_provenance?: string | null;
  area_source_url?: string | null;
  rent_low: number | null;
  rent_high: number | null;
  rent_range?: string | null;
  rent_range_raw?: string | null;
  rent_is_range?: boolean | null;
  rent_provenance?: string | null;
  date_captured: string;
  available_date: string | null;
  available_date_raw?: string | null;
  _available_date_raw?: string | null;
  availability_date_provenance?: string | null;
  availability_status?: string | null;
  lease_term: number | null;
  move_in_date: string | null;
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
}

export class UnitService implements IUnitService {
  constructor(private readonly provider: IDataProvider) {}

  async getUnitsByProperty(propertyId: string): Promise<Unit[]> {
    const latest = await this.provider.runs.getLatestDate();
    if (!latest) return [];
    const rawProp = await this.provider.properties.getForRun(latest, propertyId);
    if (!rawProp) return [];
    const units = (rawProp.units ?? []) as unknown[];
    return this.isV2Shape(rawProp)
      ? this.transformV2(units as RawV2Unit[], propertyId)
      : this.transformV1(units as RawV1Unit[], propertyId);
  }

  async getUnitsByFloorPlan(propertyId: string): Promise<FloorPlanGroup[]> {
    const units = await this.getUnitsByProperty(propertyId);
    const groups = new Map<string, Unit[]>();
    for (const unit of units) {
      const key = unit.floorPlanType || 'Unknown';
      const existing = groups.get(key) || [];
      existing.push(unit);
      groups.set(key, existing);
    }
    return Array.from(groups.entries()).map(([name, groupUnits]) => {
      const rents = groupUnits.map((u) => u.askingRent).filter((r) => r > 0);
      const available = groupUnits.filter((u) => u.availabilityStatus === 'AVAILABLE');
      return {
        floorPlanName: name,
        bedBath: name,
        totalUnits: groupUnits.length,
        availableUnits: available.length,
        avgRent: rents.length > 0 ? Math.round(rents.reduce((a, b) => a + b, 0) / rents.length) : 0,
        minRent: rents.length > 0 ? Math.min(...rents) : 0,
        maxRent: rents.length > 0 ? Math.max(...rents) : 0,
        avgSqft: null,
        units: groupUnits,
      };
    });
  }

  async getUnitHistory(_propertyId: string, _unitId: string): Promise<UnitHistoryEntry[]> {
    return [];
  }

  private isV2Shape(raw: Record<string, unknown>): boolean {
    return 'apartment_id' in raw || 'proj_name' in raw;
  }

  private transformV1(rawUnits: RawV1Unit[], propertyId: string): Unit[] {
    return rawUnits.map((u) => {
      const askingRent = (u.market_rent_low + u.market_rent_high) / 2;
      return {
        unitId: u.unit_id,
        propertyId,
        floorPlanType: null,
        marketRentLow: u.market_rent_low,
        marketRentHigh: u.market_rent_high,
        askingRent: Math.round(askingRent),
        effectiveRent: null,
        sqft: null,
        availabilityStatus: u.available_date ? ('AVAILABLE' as const) : ('UNKNOWN' as const),
        availableDate: u.available_date || null,
        leaseLink: u.lease_link || '',
        concessions: u.concessions,
        amenities: u.amenities,
        daysOnMarket: null,
        rentPerSqft: null,
        floorplanImageUrl: u.floorplan_image_url || null,
      };
    });
  }

  private transformV2(rawUnits: RawV2Unit[], propertyId: string): Unit[] {
    return rawUnits.map((u) => {
      const lo = u.rent_low ?? 0;
      const hi = u.rent_high ?? 0;
      const askingRent = lo > 0 && hi > 0 ? (lo + hi) / 2 : lo > 0 ? lo : hi;
      const sqft = u.area_sqft ?? (u.area > 0 ? u.area : null);
      const publishedStatus = u.availability_status?.toUpperCase();
      const availabilityStatus =
        publishedStatus === 'AVAILABLE' ||
        publishedStatus === 'UNAVAILABLE' ||
        publishedStatus === 'UNKNOWN'
          ? publishedStatus
          : u.available_date
            ? ('AVAILABLE' as const)
            : ('UNKNOWN' as const);
      return {
        unitId: u.unit_id || '',
        sourceUnitId: u.source_unit_id ?? null,
        canonicalUnitId: u.canonical_unit_id ?? u.unit_id ?? null,
        unitHistoryKey: u.unit_history_key ?? null,
        unitHistoryKeyBasis: u.unit_history_key_basis ?? null,
        unitHistoryKeyQuality: u.unit_history_key_quality ?? null,
        unitHistoryKeyVersion: u.unit_history_key_version ?? null,
        propertyId,
        floorPlanType: u.floor_plan_name || null,
        marketRentLow: lo,
        marketRentHigh: hi,
        askingRent: Math.round(askingRent),
        effectiveRent: null,
        sqft,
        availabilityStatus,
        availableDate: u.available_date || null,
        availableDateRaw: u.available_date_raw ?? u._available_date_raw ?? null,
        availabilityDateProvenance: u.availability_date_provenance ?? null,
        leaseLink: '',
        concessions: null,
        amenities: null,
        daysOnMarket: null,
        rentPerSqft: sqft && sqft > 0 && askingRent > 0 ? Math.round((askingRent / sqft) * 100) / 100 : null,
        floorplanImageUrl: null,
        beds: u.beds,
        baths: u.baths,
        area: u.area,
        areaSqft: u.area_sqft ?? null,
        areaIsPublished: u.area_is_published ?? null,
        areaLow: u.area_low ?? null,
        areaHigh: u.area_high ?? null,
        areaRange: u.area_range ?? null,
        areaRangeRaw: u.area_range_raw ?? null,
        areaValueType: u.area_value_type ?? null,
        areaProvenance: u.area_provenance ?? null,
        areaSourceUrl: u.area_source_url ?? null,
        rentRange: u.rent_range ?? null,
        rentRangeRaw: u.rent_range_raw ?? null,
        rentIsRange: u.rent_is_range ?? null,
        rentProvenance: u.rent_provenance ?? null,
        floorPlanName: u.floor_plan_name,
        floorPlanId: u.floor_plan_id ?? null,
        floorPlanNameProvenance: u.floor_plan_name_provenance ?? null,
        unitName: u.unit_name ?? null,
        floor: u.floor ?? null,
        building: u.building ?? null,
        buildingId: u.building_id ?? null,
        buildingIdSource: u.building_id_source ?? null,
        extractionTier: u.extraction_tier ?? null,
        sourceIds: u.source_ids ?? null,
        sourceResponseSha256: u.source_response_sha256 ?? null,
        sourceResponseUrl: u.source_response_url ?? null,
        sourceRecordLocator: u.source_record_locator ?? null,
        sourceParentRecordLocator: u.source_parent_record_locator ?? null,
        sourceAssetUrl: u.source_asset_url ?? null,
        sourceAssetSha256: u.source_asset_sha256 ?? null,
        identityQuality: u.identity_quality ?? null,
        unitIdAliases: u.unit_id_aliases ?? [],
        unitIdAliasSources: u.unit_id_alias_sources ?? [],
        leaseTerm: u.lease_term,
        moveInDate: u.move_in_date,
        dateCaptured: u.date_captured,
      };
    });
  }
}
