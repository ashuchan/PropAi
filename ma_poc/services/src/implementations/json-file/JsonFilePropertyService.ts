/**
 * @file JsonFilePropertyService.ts
 * @description Reads property data from JSON files in data/runs/ and data/state/.
 * Implements IPropertyService. Caches parsed data with 60s TTL.
 */

import type { IPropertyService } from '../../interfaces/IPropertyService.js';
import type { PaginatedResult, PropertyFilters, SortOptions, ExtractionTier, ScrapeStatus } from '../../types/common.js';
import type { PropertySummary, Property, PropertyAggregates, Unit, FloorPlan, MarketMetrics } from '../../types/property.js';
import { readJsonFile, readJsonlFile, getLatestRunDate, runPath, statePath } from './dataLoader.js';

/** Raw property format from backend properties.json */
interface RawProperty {
  'Property Name': string;
  'Unique ID': string;
  'Property ID': string;
  'City': string;
  'State': string;
  'ZIP Code': string;
  'Property Address': string;
  'Latitude': number;
  'Longitude': number;
  'Management Company': string;
  'Development Company': string;
  'Property Owner': string;
  'Total Units': number;
  'Year Built': number | null;
  'Stories': number | null;
  'Property Status': string;
  'Property Type': string;
  'Property Style': string;
  'Market Name': string;
  'Submarket Name': string;
  'Region': string;
  'Website': string;
  'Phone': string;
  'Average Unit Size (SF)': number | null;
  'Unit Mix': string;
  'Asset Grade in Submarket': string;
  'Asset Grade in Market': string;
  'Update Date': string;
  units: RawUnit[];
}

interface RawUnit {
  unit_id: string;
  market_rent_low: number;
  market_rent_high: number;
  available_date: string;
  lease_link: string;
  concessions: string | null;
  amenities: string | null;
}

interface RawPropertyIndex {
  [key: string]: {
    canonical_id: string;
    name: string;
    address: string;
    city: string;
    state: string;
    zip: string;
    website: string;
    last_scrape_status: string;
    last_units_count: number;
    last_seen_date: string;
    last_seen_at: string;
    first_seen_date: string;
  };
}

interface RawLedgerEntry {
  canonical_id: string;
  status: string;
  units_count: number;
  carry_forward_used: boolean;
  scrape_failed: boolean;
  error_count: number;
  warning_count: number;
}

export class JsonFilePropertyService implements IPropertyService {
  constructor(private readonly dataDir: string) {}

  private async loadProperties(): Promise<PropertySummary[]> {
    const latestDate = await getLatestRunDate(this.dataDir);
    if (!latestDate) return [];

    const raw = await readJsonFile<RawProperty[]>(runPath(this.dataDir, latestDate, 'properties.json'));
    if (!raw) return [];

    const index = await readJsonFile<RawPropertyIndex>(statePath(this.dataDir, 'property_index.json'));
    const ledger = await readJsonlFile<RawLedgerEntry>(runPath(this.dataDir, latestDate, 'ledger.jsonl'));
    const ledgerMap = new Map(ledger.map(l => [l.canonical_id, l]));

    return raw.map(p => this.toPropertySummary(p, index, ledgerMap));
  }

  private toPropertySummary(
    raw: RawProperty,
    index: RawPropertyIndex | null,
    ledgerMap: Map<string, RawLedgerEntry>
  ): PropertySummary {
    const id = raw['Unique ID'] || raw['Property ID'];
    const units = raw.units || [];
    const rents = units.map(u => (u.market_rent_low + u.market_rent_high) / 2).filter(r => r > 0);
    const availableUnits = units.filter(u => u.available_date && new Date(u.available_date) >= new Date()).length;
    const indexEntry = index?.[id];
    const ledgerEntry = ledgerMap.get(id);
    const sortedRents = [...rents].sort((a, b) => a - b);
    const medianRent = sortedRents.length > 0 ? sortedRents[Math.floor(sortedRents.length / 2)] : 0;
    const avgRent = rents.length > 0 ? rents.reduce((a, b) => a + b, 0) / rents.length : 0;
    const concessions = units.map(u => u.concessions).filter(Boolean);

    const scrapeStatus: ScrapeStatus = ledgerEntry?.carry_forward_used
      ? 'CARRIED_FORWARD'
      : ledgerEntry?.scrape_failed
        ? 'FAILED'
        : (indexEntry?.last_scrape_status as ScrapeStatus) || 'SUCCESS';

    return {
      id,
      name: raw['Property Name'],
      address: raw['Property Address'],
      city: raw['City'],
      state: raw['State'],
      zip: raw['ZIP Code'],
      latitude: raw['Latitude'] || 0,
      longitude: raw['Longitude'] || 0,
      managementCompany: raw['Management Company'] || '',
      totalUnits: raw['Total Units'] || units.length,
      avgAskingRent: Math.round(avgRent),
      medianAskingRent: Math.round(medianRent),
      availabilityRate: units.length > 0 ? availableUnits / units.length : 0,
      availableUnits,
      extractionTier: this.inferTier(units),
      scrapeStatus,
      propertyStatus: this.mapPropertyStatus(raw['Property Status']),
      yearBuilt: raw['Year Built'],
      stories: raw['Stories'],
      activeConcession: concessions[0] || null,
      lastScrapeTimestamp: indexEntry?.last_seen_at || raw['Update Date'] || '',
      carryForwardDays: ledgerEntry?.carry_forward_used ? 1 : 0,
      imageUrl: null,
      websiteUrl: raw['Website'] || indexEntry?.website || '',
    };
  }

  private inferTier(units: RawUnit[]): ExtractionTier {
    if (units.length === 0) return 'FAILED';
    const hasRent = units.some(u => u.market_rent_low > 0 || u.market_rent_high > 0);
    if (!hasRent) return 'TIER_3_DOM';
    return 'TIER_1_API';
  }

  private mapPropertyStatus(status: string): 'ACTIVE' | 'LEASE_UP' | 'STABILISED' | 'OFFLINE' {
    const s = (status || '').toUpperCase();
    if (s.includes('LEASE')) return 'LEASE_UP';
    if (s.includes('STAB')) return 'STABILISED';
    if (s.includes('OFFLINE') || s.includes('CLOSED')) return 'OFFLINE';
    return 'ACTIVE';
  }

  async getProperties(
    filters?: PropertyFilters,
    sort?: SortOptions,
    page: number = 1,
    pageSize: number = 25
  ): Promise<PaginatedResult<PropertySummary>> {
    let items = await this.loadProperties();
    if (filters) items = this.applyFilters(items, filters);
    if (sort) items = this.applySort(items, sort);

    const total = items.length;
    const totalPages = Math.ceil(total / pageSize);
    const start = (page - 1) * pageSize;
    const paged = items.slice(start, start + pageSize);

    return { items: paged, total, page, pageSize, totalPages };
  }

  async getPropertyById(id: string): Promise<Property | null> {
    const latestDate = await getLatestRunDate(this.dataDir);
    if (!latestDate) return null;

    const raw = await readJsonFile<RawProperty[]>(runPath(this.dataDir, latestDate, 'properties.json'));
    if (!raw) return null;

    const rawProp = raw.find(p => (p['Unique ID'] || p['Property ID']) === id);
    if (!rawProp) return null;

    const index = await readJsonFile<RawPropertyIndex>(statePath(this.dataDir, 'property_index.json'));
    const ledger = await readJsonlFile<RawLedgerEntry>(runPath(this.dataDir, latestDate, 'ledger.jsonl'));
    const ledgerMap = new Map(ledger.map(l => [l.canonical_id, l]));

    const summary = this.toPropertySummary(rawProp, index, ledgerMap);
    const units = this.transformUnits(rawProp.units || [], id);
    const floorPlans = this.buildFloorPlans(units);
    const metrics = this.computeMetrics(units);

    return {
      ...summary,
      units,
      floorPlans,
      marketMetrics: metrics,
      scrapeHistory: [],
      screenshotPaths: { pricingPage: null, banner: null },
      developmentCompany: rawProp['Development Company'] || '',
      propertyOwner: rawProp['Property Owner'] || '',
      marketName: rawProp['Market Name'] || '',
      submarketName: rawProp['Submarket Name'] || '',
      region: rawProp['Region'] || '',
      phone: rawProp['Phone'] || '',
      unitMix: rawProp['Unit Mix'] || '',
      assetGradeSubmarket: rawProp['Asset Grade in Submarket'] || '',
      assetGradeMarket: rawProp['Asset Grade in Market'] || '',
      averageUnitSizeSf: rawProp['Average Unit Size (SF)'],
    };
  }

  async getAggregateStats(filters?: PropertyFilters): Promise<PropertyAggregates> {
    let items = await this.loadProperties();
    if (filters) items = this.applyFilters(items, filters);

    const totalProperties = items.length;
    const totalUnits = items.reduce((sum, p) => sum + p.totalUnits, 0);
    const rents = items.filter(p => p.avgAskingRent > 0).map(p => p.avgAskingRent);
    const avgRent = rents.length > 0 ? rents.reduce((a, b) => a + b, 0) / rents.length : 0;
    const sortedRents = [...rents].sort((a, b) => a - b);
    const medianRent = sortedRents.length > 0 ? sortedRents[Math.floor(sortedRents.length / 2)] : 0;
    const availableTotal = items.reduce((sum, p) => sum + p.availableUnits, 0);
    const availabilityRate = totalUnits > 0 ? availableTotal / totalUnits : 0;
    const successCount = items.filter(p => p.scrapeStatus === 'SUCCESS' || p.scrapeStatus === 'SUCCESS_WITH_ERRORS').length;
    const successRate = totalProperties > 0 ? successCount / totalProperties : 0;

    const tierDistribution = {} as Record<ExtractionTier, number>;
    for (const p of items) {
      tierDistribution[p.extractionTier] = (tierDistribution[p.extractionTier] || 0) + 1;
    }

    const cityDistribution: Record<string, number> = {};
    for (const p of items) {
      cityDistribution[p.city] = (cityDistribution[p.city] || 0) + 1;
    }

    return { totalProperties, totalUnits, avgRent: Math.round(avgRent), medianRent: Math.round(medianRent), availabilityRate, successRate, tierDistribution, cityDistribution };
  }

  async searchProperties(query: string, limit: number = 20): Promise<PropertySummary[]> {
    const items = await this.loadProperties();
    const q = query.toLowerCase();
    return items
      .filter(p => p.name.toLowerCase().includes(q) || p.address.toLowerCase().includes(q) || p.city.toLowerCase().includes(q) || p.managementCompany.toLowerCase().includes(q))
      .slice(0, limit);
  }

  async getRankedProperties(metric: string, direction: 'asc' | 'desc', limit: number = 10): Promise<PropertySummary[]> {
    const items = await this.loadProperties();
    const sorted = this.applySort(items, { field: metric, direction });
    return sorted.slice(0, limit);
  }

  private transformUnits(rawUnits: RawUnit[], propertyId: string): Unit[] {
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
      };
    });
  }

  private buildFloorPlans(units: Unit[]): FloorPlan[] {
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
        name, bedBath: name, count: groupUnits.length, availableCount: available.length,
        avgRent: rents.length > 0 ? Math.round(rents.reduce((a, b) => a + b, 0) / rents.length) : 0,
        minRent: rents.length > 0 ? Math.min(...rents) : 0,
        maxRent: rents.length > 0 ? Math.max(...rents) : 0,
        avgSqft: null, units: groupUnits,
      };
    });
  }

  private computeMetrics(units: Unit[]): MarketMetrics {
    const rents = units.map(u => u.askingRent).filter(r => r > 0);
    const sortedRents = [...rents].sort((a, b) => a - b);
    const available = units.filter(u => u.availabilityStatus === 'AVAILABLE').length;
    return {
      minRent: rents.length > 0 ? Math.min(...rents) : 0,
      maxRent: rents.length > 0 ? Math.max(...rents) : 0,
      medianRent: sortedRents.length > 0 ? sortedRents[Math.floor(sortedRents.length / 2)] : 0,
      avgRent: rents.length > 0 ? Math.round(rents.reduce((a, b) => a + b, 0) / rents.length) : 0,
      avgDaysOnMarket: 0, avgSqft: null, avgRentPerSqft: null,
      occupancyRate: units.length > 0 ? 1 - (available / units.length) : 0,
    };
  }

  private applyFilters(items: PropertySummary[], filters: PropertyFilters): PropertySummary[] {
    return items.filter(p => {
      if (filters.search) {
        const q = filters.search.toLowerCase();
        if (!p.name.toLowerCase().includes(q) && !p.address.toLowerCase().includes(q) && !p.city.toLowerCase().includes(q)) return false;
      }
      if (filters.cities?.length && !filters.cities.includes(p.city)) return false;
      if (filters.tiers?.length && !filters.tiers.includes(p.extractionTier)) return false;
      if (filters.statuses?.length && !filters.statuses.includes(p.scrapeStatus)) return false;
      if (filters.propertyStatuses?.length && !filters.propertyStatuses.includes(p.propertyStatus)) return false;
      if (filters.minRent != null && p.avgAskingRent < filters.minRent) return false;
      if (filters.maxRent != null && p.avgAskingRent > filters.maxRent) return false;
      if (filters.hasConcession != null && (p.activeConcession !== null) !== filters.hasConcession) return false;
      return true;
    });
  }

  private applySort(items: PropertySummary[], sort: SortOptions): PropertySummary[] {
    const { field, direction } = sort;
    const mult = direction === 'asc' ? 1 : -1;
    return [...items].sort((a, b) => {
      const aVal = (a as Record<string, unknown>)[field];
      const bVal = (b as Record<string, unknown>)[field];
      if (aVal == null && bVal == null) return 0;
      if (aVal == null) return 1;
      if (bVal == null) return -1;
      if (typeof aVal === 'string') return mult * aVal.localeCompare(bVal as string);
      return mult * ((aVal as number) - (bVal as number));
    });
  }
}
