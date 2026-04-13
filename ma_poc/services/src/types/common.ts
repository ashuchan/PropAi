/**
 * @file common.ts
 * @description Shared types used across all service interfaces.
 */

/** Extraction tier indicating how data was obtained */
export type ExtractionTier = 'TIER_1_API' | 'TIER_2_JSONLD' | 'TIER_3_DOM' | 'TIER_4_LLM' | 'TIER_5_VISION' | 'FAILED';

/** Scrape outcome status */
export type ScrapeStatus = 'SUCCESS' | 'FAILED' | 'CARRIED_FORWARD' | 'SKIPPED' | 'SUCCESS_WITH_ERRORS';

/** Property lifecycle status */
export type PropertyStatus = 'ACTIVE' | 'LEASE_UP' | 'STABILISED' | 'OFFLINE';

/** Paginated result wrapper */
export interface PaginatedResult<T> {
  items: T[];
  total: number;
  page: number;
  pageSize: number;
  totalPages: number;
}

/** Filter options for property queries */
export interface PropertyFilters {
  search?: string;
  cities?: string[];
  tiers?: ExtractionTier[];
  statuses?: ScrapeStatus[];
  propertyStatuses?: PropertyStatus[];
  minRent?: number;
  maxRent?: number;
  hasConcession?: boolean;
}

/** Sort configuration */
export interface SortOptions {
  field: string;
  direction: 'asc' | 'desc';
}
