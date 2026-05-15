/**
 * @file IPropertyService.ts
 * @description Interface for property data access.
 */

import type { PaginatedResult, PropertyFilters, SortOptions, DataScope } from '../types/common.js';
import type { PropertySummary, Property, PropertyAggregates } from '../types/property.js';

/** Property data access interface.
 *
 *  Every list/aggregate method takes an optional `scope` arg:
 *    - 'today' (default) — restrict to rows whose `last_seen_at` falls
 *      within the most recent `run_date`. Today's roster.
 *    - 'all' — full current-state view across every recorded unit.
 *
 *  The default is 'today' end-to-end (here, in PropertyService, in the
 *  route layer, and in the frontend store) so a caller that omits scope
 *  never silently lands on the historical view. */
export interface IPropertyService {
  getProperties(filters?: PropertyFilters, sort?: SortOptions, page?: number, pageSize?: number, scope?: DataScope): Promise<PaginatedResult<PropertySummary>>;
  getPropertyById(id: string, scope?: DataScope): Promise<Property | null>;
  getAggregateStats(filters?: PropertyFilters, scope?: DataScope): Promise<PropertyAggregates>;
  searchProperties(query: string, limit?: number, scope?: DataScope): Promise<PropertySummary[]>;
  getRankedProperties(metric: string, direction: 'asc' | 'desc', limit?: number, scope?: DataScope): Promise<PropertySummary[]>;
  getPropertyReport(id: string): Promise<PropertyReport | null>;
  getPropertyProfile(id: string): Promise<PropertyProfile | null>;
}

/** Per-property run report — the full markdown document written by daily_runner. */
export interface PropertyReport {
  propertyId: string;
  runDate: string;
  filePath: string;
  markdown: string;
}

/** Per-property scrape profile from config/profiles/{id}.json. */
export interface PropertyProfile {
  canonicalId: string;
  filePath: string;
  data: Record<string, unknown>;
}
