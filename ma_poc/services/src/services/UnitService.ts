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
  beds: number;
  baths: number;
  floor_plan_name: string | null;
  area: number;
  rent_low: number | null;
  rent_high: number | null;
  date_captured: string;
  available_date: string | null;
  lease_term: number | null;
  move_in_date: string | null;
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
      const sqft = u.area > 0 ? u.area : null;
      return {
        unitId: u.unit_id || '',
        propertyId,
        floorPlanType: u.floor_plan_name || null,
        marketRentLow: lo,
        marketRentHigh: hi,
        askingRent: Math.round(askingRent),
        effectiveRent: null,
        sqft,
        availabilityStatus: u.available_date ? ('AVAILABLE' as const) : ('UNKNOWN' as const),
        availableDate: u.available_date || null,
        leaseLink: '',
        concessions: null,
        amenities: null,
        daysOnMarket: null,
        rentPerSqft: sqft && sqft > 0 && askingRent > 0 ? Math.round((askingRent / sqft) * 100) / 100 : null,
        floorplanImageUrl: null,
        beds: u.beds,
        baths: u.baths,
        area: u.area,
        floorPlanName: u.floor_plan_name,
        leaseTerm: u.lease_term,
        moveInDate: u.move_in_date,
        dateCaptured: u.date_captured,
      };
    });
  }
}
