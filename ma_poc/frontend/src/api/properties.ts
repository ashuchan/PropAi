import { apiClient } from './client';

export type SchemaVersion = 'v1' | 'v2';

export interface ApiPropertySummary {
  id: string; name: string; address: string; city: string; state: string; zip: string;
  latitude: number; longitude: number; managementCompany: string;
  totalUnits: number; avgAskingRent: number; medianAskingRent: number;
  availabilityRate: number; availableUnits: number;
  extractionTier: string; scrapeStatus: string; propertyStatus: string;
  yearBuilt: number | null; stories: number | null;
  activeConcession: string | null; lastScrapeTimestamp: string;
  carryForwardDays: number; imageUrl: string | null; galleryUrls?: string[]; websiteUrl: string;
  llmCostUsd: number; llmCallCount: number; llmTokensTotal: number;
}

/** Unit with optional V2 fields */
export interface ApiUnit {
  unitId: string; propertyId: string; floorPlanType: string | null;
  marketRentLow: number; marketRentHigh: number;
  askingRent: number; effectiveRent: number | null;
  sqft: number | null;
  availabilityStatus: 'AVAILABLE' | 'UNAVAILABLE' | 'UNKNOWN';
  availableDate: string | null; leaseLink: string;
  concessions: string | null; amenities: string | null;
  daysOnMarket: number | null; rentPerSqft: number | null;
  floorplanImageUrl: string | null;
  /** V2 fields — present only when schema is v2 */
  beds?: number | null;
  baths?: number | null;
  area?: number | null;
  floorPlanName?: string | null;
  leaseTerm?: number | null;
  moveInDate?: string | null;
  dateCaptured?: string | null;
}

/** Property media model */
export interface ApiPropertyMedia {
  heroImageUrl: string | null;
  galleryUrls: string[];
  screenshots: { pricingPage: string | null; banner: string | null; homepage: string | null };
  floorPlanImages: Array<{ floorPlanName: string; imageUrl: string; unitIds: string[] }>;
}

/** Full property detail (extends summary with units, media, V2 fields) */
export interface ApiPropertyDetail extends ApiPropertySummary {
  units: ApiUnit[];
  floorPlans: Array<{ name: string; bedBath: string; count: number; availableCount: number; avgRent: number; minRent: number; maxRent: number; avgSqft: number | null; units: ApiUnit[] }>;
  marketMetrics: { minRent: number; maxRent: number; medianRent: number; avgRent: number; avgDaysOnMarket: number; avgSqft: number | null; avgRentPerSqft: number | null; occupancyRate: number };
  screenshotPaths: { pricingPage: string | null; banner: string | null };
  media: ApiPropertyMedia;
  phone: string; unitMix: string;
  averageUnitSizeSf: number | null;
  schemaVersion: SchemaVersion;
  /** V2-only */
  emailAddress?: string | null;
  websiteDesign?: string | null;
}

export interface ApiPaginatedResult<T> { items: T[]; total: number; page: number; pageSize: number; totalPages: number; }
export interface ApiPropertyAggregates { totalProperties: number; totalUnits: number; avgRent: number; medianRent: number; availabilityRate: number; successRate: number; tierDistribution: Record<string, number>; cityDistribution: Record<string, number>; }
export interface ApiConfig { schemaVersion: SchemaVersion; }

export async function fetchProperties(params?: Record<string, string | number | undefined>): Promise<ApiPaginatedResult<ApiPropertySummary>> {
  const cleanParams = Object.fromEntries(Object.entries(params || {}).filter(([, v]) => v != null));
  const { data } = await apiClient.get('/properties', { params: cleanParams }); return data;
}
export async function fetchPropertyById(id: string): Promise<ApiPropertyDetail> { const { data } = await apiClient.get(`/properties/${id}`); return data; }
export async function fetchPropertyReport(id: string) { const { data } = await apiClient.get(`/properties/${id}/report`); return data; }
export async function fetchPropertyProfile(id: string) { const { data } = await apiClient.get(`/properties/${id}/profile`); return data; }
export async function fetchPropertyStats(): Promise<ApiPropertyAggregates> { const { data } = await apiClient.get('/properties/stats'); return data; }
export async function searchProperties(q: string, limit = 20): Promise<ApiPropertySummary[]> { const { data } = await apiClient.get('/properties/search', { params: { q, limit } }); return data; }
export async function fetchConfig(): Promise<ApiConfig> { const { data } = await apiClient.get('/config'); return data; }
