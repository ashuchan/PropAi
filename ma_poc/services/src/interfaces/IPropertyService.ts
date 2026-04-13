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
}
