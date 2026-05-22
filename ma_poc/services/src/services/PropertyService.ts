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
  PaginatedResult, PropertyFilters, SortOptions, ExtractionTier, ScrapeStatus, DataScope,
} from '../types/common.js';
import { isSuccessVerdict } from '../types/common.js';
import type {
  AvailabilityStatus, PropertySummary, Property, PropertyAggregates, Unit, FloorPlan,
  MarketMetrics, PropertyMedia, FloorPlanImage, SchemaVersion,
} from '../types/property.js';
import { resolveAvailabilityStatus } from '../utils/availability.js';
import { buildConcessionFields } from '../utils/concession.js';

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
  // 2026-05-20: producer-literal availability string + explicit status.
  // Both are optional to preserve forward-compat with pre-2026-05-20
  // payloads — the resolver helpers treat ``undefined`` as ``null``.
  available_date_raw?: string | null;
  availability_status?: string | null;
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

/** Project a cumulative `units` table row (UnitStateRecord) into the
 *  Unit shape the API contract expects. Shared between PropertyService
 *  (detail page, scope='all' override) and UnitService (units endpoint,
 *  scope='all'). Exported for tests. */
export function recordToUnit(
  r: import('../data-provider/contracts.js').UnitStateRecord,
  propertyId: string,
): Unit {
  const lo = r.rentLow ?? r.marketRentLow ?? 0;
  const hi = r.rentHigh ?? r.marketRentHigh ?? 0;
  const askingRent = lo > 0 && hi > 0 ? (lo + hi) / 2 : lo > 0 ? lo : hi;
  const sqft = r.area ?? r.sqft ?? null;
  const concessions = typeof r.concessions === 'string' ? r.concessions : null;
  return {
    unitId: r.unitId,
    propertyId,
    floorPlanType: r.floorPlanName ?? null,
    marketRentLow: lo,
    marketRentHigh: hi,
    askingRent: Math.round(askingRent),
    effectiveRent: null,
    sqft,
    // 2026-05-20: read the explicit status field (populated by the Python
    // normaliser); fall back to date-based inference only when no
    // producer status was captured.
    availabilityStatus: resolveAvailabilityStatus(r.availabilityStatus, r.availableDate),
    availableDate: r.availableDate ?? null,
    availableDateRaw: r.availableDateRaw ?? null,
    leaseLink: '',
    concessions,
    amenities: null,
    daysOnMarket: null,
    rentPerSqft: sqft && sqft > 0 && askingRent > 0 ? Math.round((askingRent / sqft) * 100) / 100 : null,
    floorplanImageUrl: null,
    beds: r.beds ?? r.bedrooms ?? null,
    baths: r.baths ?? r.bathrooms ?? null,
    area: sqft,
    floorPlanName: r.floorPlanName ?? null,
    leaseTerm: r.leaseTerm ?? null,
    moveInDate: r.moveInDate ?? null,
    dateCaptured: r.dateCaptured ?? null,
  };
}

// ─────────────────────────────────────────────────────────────────────────────

export class PropertyService implements IPropertyService {
  constructor(private readonly provider: IDataProvider) {}

  private detectedSchema: SchemaVersion = 'v1';

  // Memoize the materialized PropertySummary[] for each scope. Without
  // this, each call to /api/properties, /api/properties/stats,
  // /api/properties/search, and /api/properties/ranked re-issues 5 Cloud
  // SQL round-trips (listForRun, propertyState.all, runs.readLedger,
  // getLlmReport) — ~15s each over the public Cloud SQL Connector for the
  // 4482-row prod dataset. Parallel requests caused the pg pool to back
  // up, the API process to hit per-request timeouts, and Vite to log
  // ECONNRESET / ECONNREFUSED. The single-flight promise also dedupes
  // concurrent loads so the "page-load fires N queries at once" pattern
  // costs one Cloud SQL trip, not N. Keyed by scope so today/all don't
  // evict each other.
  private propertyCache: Map<DataScope, { expires: number; items: PropertySummary[] }> = new Map();
  private propertyCacheInflight: Map<DataScope, Promise<PropertySummary[]>> = new Map();
  private static readonly PROPERTY_CACHE_TTL_MS = 60_000;

  private async loadProperties(scope: DataScope = 'today'): Promise<PropertySummary[]> {
    const now = Date.now();
    const cached = this.propertyCache.get(scope);
    if (cached && cached.expires > now) {
      return cached.items;
    }
    const inflight = this.propertyCacheInflight.get(scope);
    if (inflight) {
      return inflight;
    }
    const promise = this.loadPropertiesUncached(scope).finally(() => {
      this.propertyCacheInflight.delete(scope);
    });
    this.propertyCacheInflight.set(scope, promise);
    return promise;
  }

  private async loadPropertiesUncached(scope: DataScope): Promise<PropertySummary[]> {
    // Fast path: providers that can compute summaries via SQL (postgres)
    // skip the per-snapshot JSONB load entirely. This is what makes the
    // /api/properties* endpoints sub-second AND correct — counts come from
    // the `units` table (~20K) rather than from per-snapshot len(units)
    // (which inflates to ~39K because JSONB payloads include duplicates,
    // lease-term variants, and carry-forward entries). It also surfaces
    // ALL canonical properties (4981) instead of just today's scrape
    // subset (4482).
    if (this.provider.properties.listSummariesFast) {
      const fast = await this.provider.properties.listSummariesFast(scope);
      const items: PropertySummary[] = fast.map((r) => this.fastRowToSummary(r));
      this.propertyCache.set(scope, {
        expires: Date.now() + PropertyService.PROPERTY_CACHE_TTL_MS, items,
      });
      return items;
    }

    // Slow path: json-file (and any future adapter without the fast hook)
    // — load the per-run JSONB payload list and project it in JS. Note
    // that for json-file scope is moot: the snapshot list IS the latest
    // run, so today and all return the same set. The toggle is a no-op
    // for that backend.
    const latestDate = await this.provider.runs.getLatestDate();
    if (!latestDate) {
      this.propertyCache.set(scope, { expires: Date.now() + PropertyService.PROPERTY_CACHE_TTL_MS, items: [] });
      return [];
    }
    const raw = await this.provider.properties.listForRun(latestDate);
    if (raw.length === 0) {
      this.propertyCache.set(scope, { expires: Date.now() + PropertyService.PROPERTY_CACHE_TTL_MS, items: [] });
      return [];
    }

    this.detectedSchema = detectSchemaVersion(raw);

    const stateList = await this.provider.propertyState.all();
    const stateMap = new Map(stateList.map((s) => [s.canonicalId, s]));
    const ledger = await this.provider.runs.readLedger(latestDate);
    const ledgerMap = new Map<string, LedgerIndexEntry>(
      ledger.map((l) => [l.canonical_id, l as LedgerIndexEntry]),
    );
    const llmMap = await this.loadLlmCosts(latestDate);

    const items: PropertySummary[] = this.detectedSchema === 'v2'
      ? (raw as unknown as RawV2Property[]).map((p) => this.toSummaryV2(p, stateMap, ledgerMap, llmMap))
      : (raw as unknown as RawV1Property[]).map((p) => this.toSummaryV1(p, stateMap, ledgerMap, llmMap));

    this.propertyCache.set(scope, {
      expires: Date.now() + PropertyService.PROPERTY_CACHE_TTL_MS,
      items,
    });
    return items;
  }

  /**
   * Translate a denormalised SQL row into the PropertySummary the API
   * contract expects. Field names map 1:1 except for tier — when the
   * ledger doesn't carry an explicit extraction_tier we infer FAILED
   * vs TIER_3_DOM vs TIER_1_API the same way `inferTierFromUnitsV2` does.
   */
  private fastRowToSummary(r: import('../data-provider/contracts.js').PropertySummaryRow): PropertySummary {
    const ledgerStatus = (r.ledgerStatus ?? '').toUpperCase();
    const stateStatus = (r.lastScrapeStatus ?? '').toUpperCase();
    const status = ledgerStatus || stateStatus || 'UNKNOWN';
    const isSuccess = isSuccessVerdict(status);

    let tier: ExtractionTier;
    const tierStr = (r.extractionTier ?? '').toUpperCase();
    if (tierStr.startsWith('TIER_') || tierStr === 'FAILED') {
      tier = tierStr as ExtractionTier;
    } else if (!isSuccess) {
      tier = 'FAILED';
    } else if (r.totalUnits === 0 || r.avgRent === 0) {
      tier = 'TIER_3_DOM';
    } else {
      tier = 'TIER_1_API';
    }

    const scrapeStatus: ScrapeStatus = isSuccess
      ? 'SUCCESS'
      : status === 'CARRIED_FORWARD' ? 'CARRIED_FORWARD'
      : status === 'SKIPPED' ? 'SKIPPED'
      : 'FAILED';

    return {
      id: r.apartmentId != null ? String(r.apartmentId) : r.canonicalId,
      name: r.name,
      address: r.address,
      city: r.city,
      state: r.state,
      zip: r.zip,
      latitude: 0,
      longitude: 0,
      managementCompany: r.pmc,
      totalUnits: r.totalUnits,
      avgAskingRent: Math.round(r.avgRent),
      medianAskingRent: Math.round(r.medianRent),
      availabilityRate: r.totalUnits > 0 ? r.availableUnits / r.totalUnits : 0,
      availableUnits: r.availableUnits,
      extractionTier: tier,
      scrapeStatus,
      propertyStatus: 'ACTIVE',
      yearBuilt: null,
      stories: null,
      ...buildConcessionFields(r.concessions ?? null),
      lastScrapeTimestamp: r.lastSeenAt ?? '',
      carryForwardDays: 0,
      imageUrl: null,
      galleryUrls: [],
      websiteUrl: r.website,
      llmCostUsd: 0,
      llmCallCount: 0,
      llmTokensTotal: 0,
    };
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
      ...buildConcessionFields(concessions[0] || null),
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
      ...buildConcessionFields(raw.concessions || null),
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
    scope: DataScope = 'today',
  ): Promise<PaginatedResult<PropertySummary>> {
    let items = await this.loadProperties(scope);
    if (filters) items = this.applyFilters(items, filters);
    if (sort) items = this.applySort(items, sort);

    const total = items.length;
    const totalPages = Math.ceil(total / pageSize);
    const start = (page - 1) * pageSize;
    const paged = items.slice(start, start + pageSize);
    return { items: paged, total, page, pageSize, totalPages };
  }

  async getPropertyById(id: string, scope: DataScope = 'today'): Promise<Property | null> {
    const latestDate = await this.provider.runs.getLatestDate();
    if (!latestDate) {
      // No run on record. For scope='all' we can still synthesise a
      // property from the propertyState + units tables. For 'today'
      // there's literally no "today" to read from.
      return scope === 'all' ? this.buildPropertyFromState(id) : null;
    }
    const rawList = await this.provider.properties.listForRun(latestDate);
    const schema = rawList.length > 0 ? detectSchemaVersion(rawList) : 'v2';
    const stateList = await this.provider.propertyState.all();
    const stateMap = new Map(stateList.map((s) => [s.canonicalId, s]));
    const ledger = await this.provider.runs.readLedger(latestDate);
    const ledgerMap = new Map<string, LedgerIndexEntry>(
      ledger.map((l) => [l.canonical_id, l as LedgerIndexEntry]),
    );
    const llmMap = await this.loadLlmCosts(latestDate);

    const rawProp = schema === 'v2'
      ? (rawList as unknown as RawV2Property[]).find((p) => String(p.apartment_id) === id)
      : (rawList as unknown as RawV1Property[]).find(
          (p) => (p['Unique ID'] || p['Property ID']) === id,
        );

    // Missing from today's snapshot:
    //   - scope='today' → 404 (not in today's roster)
    //   - scope='all'   → fall back to propertyState + units tables so
    //                     properties not scraped today still resolve.
    if (!rawProp) {
      return scope === 'all' ? this.buildPropertyFromState(id) : null;
    }

    const built = schema === 'v2'
      ? this.buildPropertyV2(rawProp as RawV2Property, id, stateMap, ledgerMap, llmMap)
      : this.buildPropertyV1(rawProp as RawV1Property, id, stateMap, ledgerMap, llmMap);

    // scope='all' overrides the snapshot unit array with the cumulative
    // current-state roster from the `units` table. Loaded LAZILY here —
    // only after we know the property exists — to avoid a wasted Cloud
    // SQL round-trip on 404s.
    if (scope === 'all') {
      const overrideUnits = await this.provider.units.listStateForProperty(id);
      return this.overrideUnitArray(built, overrideUnits);
    }
    return built;
  }

  /** Synthesise a Property from `properties` ⨝ `units` state tables when
   *  the property has no entry in the latest run snapshot. Used by
   *  scope='all' to keep historical properties addressable. */
  private async buildPropertyFromState(id: string): Promise<Property | null> {
    const state = await this.provider.propertyState.get(id);
    if (!state) return null;
    const units = await this.provider.units.listStateForProperty(id);
    if (units.length === 0 && !state.projName && !state.name) return null;

    const transformed: Unit[] = units.map((r) => recordToUnit(r, id));
    return {
      id,
      name: state.projName ?? state.name ?? '',
      address: state.address ?? '',
      city: state.city ?? '',
      state: state.state ?? '',
      zip: state.zipCode ?? state.zip ?? '',
      latitude: 0,
      longitude: 0,
      managementCompany: state.pmc ?? '',
      totalUnits: transformed.length,
      avgAskingRent: 0,
      medianAskingRent: 0,
      availabilityRate: 0,
      availableUnits: transformed.filter((u) => u.availabilityStatus === 'AVAILABLE').length,
      extractionTier: 'TIER_1_API',
      scrapeStatus: (state.lastScrapeStatus as ScrapeStatus | null) ?? 'SUCCESS',
      propertyStatus: 'ACTIVE',
      yearBuilt: null,
      stories: null,
      ...buildConcessionFields(typeof state.concessions === 'string' ? state.concessions : null),
      lastScrapeTimestamp: state.lastSeenAt ?? '',
      carryForwardDays: 0,
      imageUrl: null,
      galleryUrls: [],
      websiteUrl: state.website ?? '',
      llmCostUsd: 0,
      llmCallCount: 0,
      llmTokensTotal: 0,
      units: transformed,
      floorPlans: this.buildFloorPlans(transformed),
      marketMetrics: this.computeMetrics(transformed),
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
      phone: state.phone ?? '',
      unitMix: '',
      assetGradeSubmarket: '',
      assetGradeMarket: '',
      averageUnitSizeSf: null,
      emailAddress: state.emailAddress ?? null,
      websiteDesign: state.websiteDesign ?? null,
      schemaVersion: 'v2',
    };
  }

  /** Replace `units` and recompute `floorPlans` + `marketMetrics` from the
   *  cumulative `UnitStateRecord[]` view. Used for scope='all' on the
   *  detail endpoint. */
  private overrideUnitArray(prop: Property, records: import('../data-provider/contracts.js').UnitStateRecord[]): Property {
    const units: Unit[] = records.map((r) => recordToUnit(r, prop.id));
    return {
      ...prop,
      units,
      floorPlans: this.buildFloorPlans(units),
      marketMetrics: this.computeMetrics(units),
      totalUnits: units.length,
      availableUnits: units.filter((u) => u.availabilityStatus === 'AVAILABLE').length,
    };
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

  async getAggregateStats(filters?: PropertyFilters, scope: DataScope = 'today'): Promise<PropertyAggregates> {
    let items = await this.loadProperties(scope);
    if (filters) items = this.applyFilters(items, filters);

    const totalProperties = items.length;
    const totalUnits = items.reduce((sum, p) => sum + p.totalUnits, 0);
    const rents = items.filter((p) => p.avgAskingRent > 0).map((p) => p.avgAskingRent);
    const avgRent = rents.length > 0 ? rents.reduce((a, b) => a + b, 0) / rents.length : 0;
    const sortedRents = [...rents].sort((a, b) => a - b);
    const medianRent = sortedRents.length > 0 ? sortedRents[Math.floor(sortedRents.length / 2)] : 0;
    const availableTotal = items.reduce((sum, p) => sum + p.availableUnits, 0);
    const availabilityRate = totalUnits > 0 ? availableTotal / totalUnits : 0;
    const successCount = items.filter((p) => isSuccessVerdict(p.scrapeStatus)).length;
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

  async searchProperties(query: string, limit = 20, scope: DataScope = 'today'): Promise<PropertySummary[]> {
    const items = await this.loadProperties(scope);
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

  async getRankedProperties(metric: string, direction: 'asc' | 'desc', limit = 10, scope: DataScope = 'today'): Promise<PropertySummary[]> {
    const items = await this.loadProperties(scope);
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
        // V1 payloads predate the explicit status field; fall back to
        // date-based inference (the historical behaviour). The helper
        // keeps this branch consistent with V2 / postgres readers.
        availabilityStatus: resolveAvailabilityStatus(null, u.available_date),
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
        // 2026-05-20: prefer the explicit producer status — the Python
        // pipeline emits AVAILABLE / WAITLIST / COMING_SOON / UNAVAILABLE
        // long before the typed date column is set. Pre-fix the API
        // shipped UNKNOWN for 77 % of the units that had an explicit
        // ``AVAILABLE`` status because the typed date was null.
        availabilityStatus: resolveAvailabilityStatus(u.availability_status, u.available_date),
        availableDate: u.available_date || null,
        availableDateRaw: u.available_date_raw ?? null,
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
