/**
 * @file JsonFileUnitService.ts
 * @description Reads unit data from property JSON files.
 */

import type { IUnitService } from '../../interfaces/IUnitService.js';
import type { Unit } from '../../types/property.js';
import type { FloorPlanGroup, UnitHistoryEntry } from '../../types/unit.js';
import { readJsonFile, getLatestRunDate, runPath } from './dataLoader.js';

interface RawProperty {
  'Unique ID': string;
  'Property ID': string;
  units: Array<{
    unit_id: string;
    market_rent_low: number;
    market_rent_high: number;
    available_date: string;
    lease_link: string;
    concessions: string | null;
    amenities: string | null;
    floorplan_image_url?: string | null;
  }>;
}

export class JsonFileUnitService implements IUnitService {
  constructor(private readonly dataDir: string) {}

  async getUnitsByProperty(propertyId: string): Promise<Unit[]> {
    const rawProp = await this.findProperty(propertyId);
    if (!rawProp) return [];
    return this.transformUnits(rawProp.units || [], propertyId);
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
      const rents = groupUnits.map(u => u.askingRent).filter(r => r > 0);
      const available = groupUnits.filter(u => u.availabilityStatus === 'AVAILABLE');
      return {
        floorPlanName: name, bedBath: name,
        totalUnits: groupUnits.length, availableUnits: available.length,
        avgRent: rents.length > 0 ? Math.round(rents.reduce((a, b) => a + b, 0) / rents.length) : 0,
        minRent: rents.length > 0 ? Math.min(...rents) : 0,
        maxRent: rents.length > 0 ? Math.max(...rents) : 0,
        avgSqft: null, units: groupUnits,
      };
    });
  }

  async getUnitHistory(_propertyId: string, _unitId: string): Promise<UnitHistoryEntry[]> {
    return [];
  }

  private async findProperty(propertyId: string): Promise<RawProperty | null> {
    const latestDate = await getLatestRunDate(this.dataDir);
    if (!latestDate) return null;
    const raw = await readJsonFile<RawProperty[]>(runPath(this.dataDir, latestDate, 'properties.json'));
    if (!raw) return null;
    return raw.find(p => (p['Unique ID'] || p['Property ID']) === propertyId) ?? null;
  }

  private transformUnits(rawUnits: RawProperty['units'], propertyId: string): Unit[] {
    return rawUnits.map(u => {
      const askingRent = (u.market_rent_low + u.market_rent_high) / 2;
      return {
        unitId: u.unit_id, propertyId, floorPlanType: null,
        marketRentLow: u.market_rent_low, marketRentHigh: u.market_rent_high,
        askingRent: Math.round(askingRent), effectiveRent: null, sqft: null,
        availabilityStatus: u.available_date ? 'AVAILABLE' as const : 'UNKNOWN' as const,
        availableDate: u.available_date || null, leaseLink: u.lease_link || '',
        concessions: u.concessions, amenities: u.amenities,
        daysOnMarket: null, rentPerSqft: null,
        floorplanImageUrl: u.floorplan_image_url || null,
      };
    });
  }
}
