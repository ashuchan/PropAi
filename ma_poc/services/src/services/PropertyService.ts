/**
 * @file PropertyService.ts
 * @description Impl-agnostic property service. All I/O goes through the
 * injected IDataProvider — swap JsonFile for Postgres by changing the
 * DATA_PROVIDER env var. Transform and aggregation logic is lifted
 * verbatim from the legacy JsonFilePropertyService.
 */

import type { IDataProvider } from '../data-provider/contracts.js';
import type { IPropertyService, PropertyReport, PropertyProfile } from '../interfaces/IPropertyService.js';
import type {
  PaginatedResult, PropertyFilters, SortOptions, ExtractionTier, ScrapeStatus,
} from '../types/common.js';
import type {
  PropertySummary, Property, PropertyAggregates, Unit, FloorPlan,
  MarketMetrics, PropertyMedia, FloorPlanImage, SchemaVersion,
} from '../types/property.js';

// ── Raw row shapes — v1 and v2 ───────────────────────────────────────────────

interface RawV1Property {
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
  'Property Image URL'?: string | null;
  'Property Gallery URLs'?: string[];
  units: RawV1Unit[];
}

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

interface RawV2Property {
  apartment_id: number | null;
  proj_name: string;
  address: string;
  city: string;
  state: string;
  zip_code: string | null;
  country: string | null;
  phone: string | null;
  email_address: string | null;
  website: string | null;
  pmc: string | null;
  website_design: string | null;
  concessions: string | null;
  units: RawV2Unit[];
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

interface LedgerIndexEntry {
  status?: string;
  units_count?: number;
  carry_forward_used?: boolean;
  scrape_failed?: boolean;
}

interface LlmCostEntry {
  costUsd: number;
  calls: number;
  tokensTotal: number;
}

function detectSchemaVersion(raw: unknown[]): SchemaVersion {
  if (!raw || raw.length === 0) return 'v1';
  const first = raw[0] as Record<string, unknown>;
  if ('apartment_id' in first || 'proj_name' in first) return 'v2';
  return 'v1';
}

// ─────────────────────────────────────────────────────────────────────────────

export class PropertyService implements IPropertyService {
  constructor(private readonly provider: IDataProvider) {}

  private detectedSchema: SchemaVersion = 'v1';

  private async loadProperties(): Promise<PropertySummary[]> {
    const latestDate = await this.provider.runs.getLatestDate();
    if (!latestDate) return [];
    const raw = await this.provider.properties.listForRun(latestDate);
    if (raw.length === 0) return [];

    this.detectedSchema = detectSchemaVersion(raw);

    const stateList = await this.provider.propertyState.all();
    const stateMap = new Map(stateList.map((s) => [s.canonicalId, s]));
    const ledger = await this.provider.runs.readLedger(latestDate);
    const ledgerMap = new Map<string, LedgerIndexEntry>(
      ledger.map((l) => [l.canonical_id, l as LedgerIndexEntry]),
    );
    const llmMap = await this.loadLlmCosts(latestDate);

    if (this.detectedSchema === 'v2') {
      return (raw as unknown as RawV2Property[]).map((p) => this.toSummaryV2(p, stateMap, ledgerMap, llmMap));
    }
    return (raw as unknown as RawV1Property[]).map((p) => this.toSummaryV1(p, stateMap, ledgerMap, llmMap));
  }

  private async loadLlmCosts(date: string): Promise<Map<string, LlmCostEntry>> {
    const report = await this.provider.artifacts.getLlmReport(date);
    const map = new Map<string, LlmCostEntry>();
    const byProp = (report?.by_property as Array<Record<string, unknown>> | undefined) ?? [];
    for (const entry of byProp) {
      const pid = String(entry.property_id ?? '');
      if (!pid) continue;
      map.set(pid, {
        costUsd: Number(entry.cost_usd ?? 0),
        calls: Number(entry.calls ?? 0),
        tokensTotal: Number(entry.tokens_total ?? 0),
      });
    }
    return map;
  }

  private stateName(state: import('../data-provider/contracts.js').PropertyStateRecord | undefined): string {
    return state?.projName ?? state?.name ?? '';
  }

  private toSummaryV1(
    raw: RawV1Property,
    stateMap: Map<string, import('../data-provider/contracts.js').PropertyStateRecord>,
    ledgerMap: Map<string, LedgerIndexEntry>,
    llmMap: Map<string, LlmCostEntry>,
  ): PropertySummary {
    const id = raw['Unique ID'] || raw['Property ID'];
    const units = raw.units || [];
    const rents = units
      .map((u) => (u.market_rent_low + u.market_rent_high) / 2)
      .filter((r) => r > 0);
    const availableUnits = units.filter(
      (u) => u.available_date && new Date(u.available_date) >= new Date(),
    ).length;
    const state = stateMap.get(id);
    const ledgerEntry = ledgerMap.get(id);
    const sortedRents = [...rents].sort((a, b) => a - b);
    const medianRent = sortedRents.length > 0 ? sortedRents[Math.floor(sortedRents.length / 2)] : 0;
    const avgRent = rents.length > 0 ? rents.reduce((a, b) => a + b, 0) / rents.length : 0;
    const concessions = units.map((u) => u.concessions).filter(Boolean);

    const scrapeStatus: ScrapeStatus =
      units.length === 0
        ? 'FAILED'
        : ledgerEntry?.carry_forward_used
          ? 'CARRIED_FORWARD'
          : ledgerEntry?.scrape_failed
            ? 'FAILED'
            : (state?.lastScrapeStatus as ScrapeStatus) || 'SUCCESS';

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
      extractionTier: this.inferTierV1(units),
      scrapeStatus,
      propertyStatus: this.mapPropertyStatus(raw['Property Status']),
      yearBuilt: raw['Year Built'],
      stories: raw['Stories'],
      activeConcession: concessions[0] || null,
      lastScrapeTimestamp: state?.lastSeenAt || raw['Update Date'] || '',
      carryForwardDays: ledgerEntry?.carry_forward_used ? 1 : 0,
      imageUrl: raw['Property Image URL'] || null,
      galleryUrls: raw['Property Gallery URLs'] || [],
      websiteUrl: raw['Website'] || state?.website || '',
      llmCostUsd: llmMap.get(id)?.costUsd ?? 0,
      llmCallCount: llmMap.get(id)?.calls ?? 0,
      llmTokensTotal: llmMap.get(id)?.tokensTotal ?? 0,
    };
  }

  private toSummaryV2(
    raw: RawV2Property,
    stateMap: Map<string, import('../data-provider/contracts.js').PropertyStateRecord>,
    ledgerMap: Map<string, LedgerIndexEntry>,
    llmMap: Map<string, LlmCostEntry>,
  ): PropertySummary {
    const id = raw.apartment_id != null ? String(raw.apartment_id) : '';
    const units = raw.units || [];
    const rents = units
      .map((u) => {
        const lo = u.rent_low ?? 0;
        const hi = u.rent_high ?? 0;
        return lo > 0 && hi > 0 ? (lo + hi) / 2 : lo > 0 ? lo : hi;
      })
      .filter((r) => r > 0);
    const availableUnits = units.filter((u) => u.available_date != null).length;
    const state = stateMap.get(id);
    const ledgerEntry = ledgerMap.get(id);
    const sortedRents = [...rents].sort((a, b) => a - b);
    const medianRent = sortedRents.length > 0 ? sortedRents[Math.floor(sortedRents.length / 2)] : 0;
    const avgRent = rents.length > 0 ? rents.reduce((a, b) => a + b, 0) / rents.length : 0;

    const scrapeStatus: ScrapeStatus =
      units.length === 0
        ? 'FAILED'
        : ledgerEntry?.carry_forward_used
          ? 'CARRIED_FORWARD'
          : ledgerEntry?.scrape_failed
            ? 'FAILED'
            : (state?.lastScrapeStatus as ScrapeStatus) || 'SUCCESS';

    return {
      id,
      name: raw.proj_name || '',
      address: raw.address || '',
      city: raw.city || '',
      state: raw.state || '',
      zip: raw.zip_code || '',
      latitude: 0,
      longitude: 0,
      managementCompany: raw.pmc || '',
      totalUnits: units.length,
      avgAskingRent: Math.round(avgRent),
      medianAskingRent: Math.round(medianRent),
      availabilityRate: units.length > 0 ? availableUnits / units.length : 0,
      availableUnits,
      extractionTier: this.inferTierV2(units),
      scrapeStatus,
      propertyStatus: 'ACTIVE',
      yearBuilt: null,
      stories: null,
      activeConcession: raw.concessions || null,
      lastScrapeTimestamp: units[0]?.date_captured || state?.lastSeenAt || '',
      carryForwardDays: ledgerEntry?.carry_forward_used ? 1 : 0,
      imageUrl: null,
      galleryUrls: [],
      websiteUrl: raw.website || state?.website || '',
      llmCostUsd: llmMap.get(id)?.costUsd ?? 0,
      llmCallCount: llmMap.get(id)?.calls ?? 0,
      llmTokensTotal: llmMap.get(id)?.tokensTotal ?? 0,
    };
  }

  private inferTierV1(units: RawV1Unit[]): ExtractionTier {
    if (units.length === 0) return 'FAILED';
    const hasRent = units.some((u) => u.market_rent_low > 0 || u.market_rent_high > 0);
    if (!hasRent) return 'TIER_3_DOM';
    return 'TIER_1_API';
  }

  private inferTierV2(units: RawV2Unit[]): ExtractionTier {
    if (units.length === 0) return 'FAILED';
    const hasRent = units.some((u) => (u.rent_low ?? 0) > 0 || (u.rent_high ?? 0) > 0);
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
    page = 1,
    pageSize = 25,
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
    const latestDate = await this.provider.runs.getLatestDate();
    if (!latestDate) return null;
    const rawList = await this.provider.properties.listForRun(latestDate);
    if (rawList.length === 0) return null;
    const schema = detectSchemaVersion(rawList);
    const stateList = await this.provider.propertyState.all();
    const stateMap = new Map(stateList.map((s) => [s.canonicalId, s]));
    const ledger = await this.provider.runs.readLedger(latestDate);
    const ledgerMap = new Map<string, LedgerIndexEntry>(
      ledger.map((l) => [l.canonical_id, l as LedgerIndexEntry]),
    );
    const llmMap = await this.loadLlmCosts(latestDate);

    if (schema === 'v2') {
      const rawProp = (rawList as unknown as RawV2Property[]).find((p) => String(p.apartment_id) === id);
      if (!rawProp) return null;
      return this.buildPropertyV2(rawProp, id, stateMap, ledgerMap, llmMap);
    }
    const rawProp = (rawList as unknown as RawV1Property[]).find(
      (p) => (p['Unique ID'] || p['Property ID']) === id,
    );
    if (!rawProp) return null;
    return this.buildPropertyV1(rawProp, id, stateMap, ledgerMap, llmMap);
  }

  private buildPropertyV1(
    rawProp: RawV1Property,
    id: string,
    stateMap: Map<string, import('../data-provider/contracts.js').PropertyStateRecord>,
    ledgerMap: Map<string, LedgerIndexEntry>,
    llmMap: Map<string, LlmCostEntry>,
  ): Property {
    const summary = this.toSummaryV1(rawProp, stateMap, ledgerMap, llmMap);
    const units = this.transformUnitsV1(rawProp.units || [], id);
    const floorPlans = this.buildFloorPlans(units);
    const metrics = this.computeMetrics(units);
    const media = this.buildMediaV1(rawProp, units);
    return {
      ...summary,
      units,
      floorPlans,
      marketMetrics: metrics,
      scrapeHistory: [],
      screenshotPaths: { pricingPage: null, banner: null },
      media,
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
      schemaVersion: 'v1',
    };
  }

  private buildPropertyV2(
    rawProp: RawV2Property,
    id: string,
    stateMap: Map<string, import('../data-provider/contracts.js').PropertyStateRecord>,
    ledgerMap: Map<string, LedgerIndexEntry>,
    llmMap: Map<string, LlmCostEntry>,
  ): Property {
    const summary = this.toSummaryV2(rawProp, stateMap, ledgerMap, llmMap);
    const units = this.transformUnitsV2(rawProp.units || [], id);
    const floorPlans = this.buildFloorPlans(units);
    const metrics = this.computeMetrics(units);
    return {
      ...summary,
      units,
      floorPlans,
      marketMetrics: metrics,
      scrapeHistory: [],
      screenshotPaths: { pricingPage: null, banner: null },
      media: {
        heroImageUrl: null,
        galleryUrls: [],
        screenshots: { pricingPage: null, banner: null, homepage: null },
        floorPlanImages: [],
      },
      developmentCompany: '',
      propertyOwner: '',
      marketName: '',
      submarketName: '',
      region: '',
      phone: rawProp.phone || '',
      unitMix: this.computeUnitMixV2(rawProp.units || []),
      assetGradeSubmarket: '',
      assetGradeMarket: '',
      averageUnitSizeSf: this.computeAvgAreaV2(rawProp.units || []),
      emailAddress: rawProp.email_address,
      websiteDesign: rawProp.website_design,
      schemaVersion: 'v2',
    };
  }

  async getAggregateStats(filters?: PropertyFilters): Promise<PropertyAggregates> {
    let items = await this.loadProperties();
    if (filters) items = this.applyFilters(items, filters);

    const totalProperties = items.length;
    const totalUnits = items.reduce((sum, p) => sum + p.totalUnits, 0);
    const rents = items.filter((p) => p.avgAskingRent > 0).map((p) => p.avgAskingRent);
    const avgRent = rents.length > 0 ? rents.reduce((a, b) => a + b, 0) / rents.length : 0;
    const sortedRents = [...rents].sort((a, b) => a - b);
    const medianRent = sortedRents.length > 0 ? sortedRents[Math.floor(sortedRents.length / 2)] : 0;
    const availableTotal = items.reduce((sum, p) => sum + p.availableUnits, 0);
    const availabilityRate = totalUnits > 0 ? availableTotal / totalUnits : 0;
    const successCount = items.filter(
      (p) => p.scrapeStatus === 'SUCCESS' || p.scrapeStatus === 'SUCCESS_WITH_ERRORS',
    ).length;
    const successRate = totalProperties > 0 ? successCount / totalProperties : 0;

    const tierDistribution = {} as Record<ExtractionTier, number>;
    for (const p of items) tierDistribution[p.extractionTier] = (tierDistribution[p.extractionTier] || 0) + 1;
    const cityDistribution: Record<string, number> = {};
    for (const p of items) cityDistribution[p.city] = (cityDistribution[p.city] || 0) + 1;

    return {
      totalProperties,
      totalUnits,
      avgRent: Math.round(avgRent),
      medianRent: Math.round(medianRent),
      availabilityRate,
      successRate,
      tierDistribution,
      cityDistribution,
    };
  }

  async searchProperties(query: string, limit = 20): Promise<PropertySummary[]> {
    const items = await this.loadProperties();
    const q = query.toLowerCase();
    return items
      .filter(
        (p) =>
          p.name.toLowerCase().includes(q) ||
          p.address.toLowerCase().includes(q) ||
          p.city.toLowerCase().includes(q) ||
          p.managementCompany.toLowerCase().includes(q),
      )
      .slice(0, limit);
  }

  async getRankedProperties(metric: string, direction: 'asc' | 'desc', limit = 10): Promise<PropertySummary[]> {
    const items = await this.loadProperties();
    return this.applySort(items, { field: metric, direction }).slice(0, limit);
  }

  async getPropertyReport(id: string): Promise<PropertyReport | null> {
    const dates = await this.provider.runs.listDates();
    for (const date of dates) {
      const markdown = await this.provider.artifacts.getPropertyReport(date, id);
      if (markdown != null) {
        return { propertyId: id, runDate: date, filePath: `(${this.provider.name}) ${date}/${id}`, markdown };
      }
    }
    return null;
  }

  async getPropertyProfile(id: string): Promise<PropertyProfile | null> {
    const data = await this.provider.artifacts.getProfile(id);
    if (!data) return null;
    return { canonicalId: id, filePath: `(${this.provider.name}) profile/${id}`, data };
  }

  // ── Transformation helpers (unchanged from JsonFilePropertyService) ─────────

  private transformUnitsV1(rawUnits: RawV1Unit[], propertyId: string): Unit[] {
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

  private transformUnitsV2(rawUnits: RawV2Unit[], propertyId: string): Unit[] {
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

  private computeUnitMixV2(units: RawV2Unit[]): string {
    const counts: Record<string, number> = {};
    for (const u of units) {
      const label = u.beds === 0 ? 'Studio' : `${u.beds}BR`;
      counts[label] = (counts[label] || 0) + 1;
    }
    return Object.entries(counts)
      .sort((a, b) => a[0].localeCompare(b[0]))
      .map(([k, v]) => `${k}: ${v}`)
      .join('; ');
  }

  private computeAvgAreaV2(units: RawV2Unit[]): number | null {
    const areas = units.map((u) => u.area).filter((a) => a > 0);
    if (areas.length === 0) return null;
    return Math.round(areas.reduce((a, b) => a + b, 0) / areas.length);
  }

  private buildMediaV1(rawProp: RawV1Property, units: Unit[]): PropertyMedia {
    const floorPlanImages: FloorPlanImage[] = [];
    const fpGroups = new Map<string, { url: string; unitIds: string[] }>();
    for (const u of units) {
      if (u.floorplanImageUrl) {
        const key = u.floorplanImageUrl;
        const existing = fpGroups.get(key);
        if (existing) existing.unitIds.push(u.unitId);
        else fpGroups.set(key, { url: key, unitIds: [u.unitId] });
      }
    }
    for (const [, val] of fpGroups) {
      floorPlanImages.push({ floorPlanName: val.unitIds[0] || 'Unknown', imageUrl: val.url, unitIds: val.unitIds });
    }
    return {
      heroImageUrl: rawProp['Property Image URL'] || null,
      galleryUrls: rawProp['Property Gallery URLs'] || [],
      screenshots: { pricingPage: null, banner: null, homepage: null },
      floorPlanImages,
    };
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
      const rents = groupUnits.map((u) => u.askingRent).filter((r) => r > 0);
      const available = groupUnits.filter((u) => u.availabilityStatus === 'AVAILABLE');
      return {
        name,
        bedBath: name,
        count: groupUnits.length,
        availableCount: available.length,
        avgRent: rents.length > 0 ? Math.round(rents.reduce((a, b) => a + b, 0) / rents.length) : 0,
        minRent: rents.length > 0 ? Math.min(...rents) : 0,
        maxRent: rents.length > 0 ? Math.max(...rents) : 0,
        avgSqft: null,
        units: groupUnits,
      };
    });
  }

  private computeMetrics(units: Unit[]): MarketMetrics {
    const rents = units.map((u) => u.askingRent).filter((r) => r > 0);
    const sortedRents = [...rents].sort((a, b) => a - b);
    const available = units.filter((u) => u.availabilityStatus === 'AVAILABLE').length;
    return {
      minRent: rents.length > 0 ? Math.min(...rents) : 0,
      maxRent: rents.length > 0 ? Math.max(...rents) : 0,
      medianRent: sortedRents.length > 0 ? sortedRents[Math.floor(sortedRents.length / 2)] : 0,
      avgRent: rents.length > 0 ? Math.round(rents.reduce((a, b) => a + b, 0) / rents.length) : 0,
      avgDaysOnMarket: 0,
      avgSqft: null,
      avgRentPerSqft: null,
      occupancyRate: units.length > 0 ? 1 - available / units.length : 0,
    };
  }

  private applyFilters(items: PropertySummary[], filters: PropertyFilters): PropertySummary[] {
    return items.filter((p) => {
      if (filters.search) {
        const q = filters.search.toLowerCase();
        if (
          !p.name.toLowerCase().includes(q) &&
          !p.address.toLowerCase().includes(q) &&
          !p.city.toLowerCase().includes(q)
        )
          return false;
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
      const aVal = (a as unknown as Record<string, unknown>)[field];
      const bVal = (b as unknown as Record<string, unknown>)[field];
      if (aVal == null && bVal == null) return 0;
      if (aVal == null) return 1;
      if (bVal == null) return -1;
      if (typeof aVal === 'string') return mult * aVal.localeCompare(bVal as string);
      return mult * ((aVal as number) - (bVal as number));
    });
  }
}
