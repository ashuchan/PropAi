/**
 * @file IPropertyService.ts
 * @description Interface for property data access.
 */

import type { PaginatedResult, PropertyFilters, SortOptions } from '../types/common.js';
import type { PropertySummary, Property, PropertyAggregates } from '../types/property.js';

/** Property data access interface */
export interface IPropertyService {
  getProperties(filters?: PropertyFilters, sort?: SortOptions, page?: number, pageSize?: number): Promise<PaginatedResult<PropertySummary>>;
  getPropertyById(id: string): Promise<Property | null>;
  getAggregateStats(filters?: PropertyFilters): Promise<PropertyAggregates>;
  searchProperties(query: string, limit?: number): Promise<PropertySummary[]>;
  getRankedProperties(metric: string, direction: 'asc' | 'desc', limit?: number): Promise<PropertySummary[]>;
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
