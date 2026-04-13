import { apiClient } from './client';

export interface ApiPropertySummary {
  id: string; name: string; address: string; city: string; state: string; zip: string;
  latitude: number; longitude: number; managementCompany: string;
  totalUnits: number; avgAskingRent: number; medianAskingRent: number;
  availabilityRate: number; availableUnits: number;
  extractionTier: string; scrapeStatus: string; propertyStatus: string;
  yearBuilt: number | null; stories: number | null;
  activeConcession: string | null; lastScrapeTimestamp: string;
  carryForwardDays: number; imageUrl: string | null; websiteUrl: string;
}

export interface ApiPaginatedResult<T> { items: T[]; total: number; page: number; pageSize: number; totalPages: number; }
export interface ApiPropertyAggregates { totalProperties: number; totalUnits: number; avgRent: number; medianRent: number; availabilityRate: number; successRate: number; tierDistribution: Record<string, number>; cityDistribution: Record<string, number>; }

export async function fetchProperties(params?: Record<string, string | number | undefined>): Promise<ApiPaginatedResult<ApiPropertySummary>> {
  const cleanParams = Object.fromEntries(Object.entries(params || {}).filter(([, v]) => v != null));
  const { data } = await apiClient.get('/properties', { params: cleanParams }); return data;
}
export async function fetchPropertyById(id: string) { const { data } = await apiClient.get(`/properties/${id}`); return data; }
export async function fetchPropertyStats(): Promise<ApiPropertyAggregates> { const { data } = await apiClient.get('/properties/stats'); return data; }
export async function searchProperties(q: string, limit = 20): Promise<ApiPropertySummary[]> { const { data } = await apiClient.get('/properties/search', { params: { q, limit } }); return data; }
