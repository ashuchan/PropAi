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

/**
 * Data freshness scope applied to every list/aggregate endpoint.
 * `today`  — restrict to rows whose `last_seen_at::date` matches the
 *            most recent `run_date` in the `runs` table.
 * `all`    — no time filter; current-state view across every unit/property
 *            on record.
 */
export type DataScope = 'today' | 'all';

/** Paginated result wrapper */
export interface PaginatedResult<T> {
  items: T[];
  total: number;
  page: number;
  pageSize: number;
  totalPages: number;
}

/** Filter options for property queries. `scope` is intentionally NOT
 *  in here — it's a freshness axis (like pagination), passed as its own
 *  parameter. Keeping it out of PropertyFilters prevents accidental use
 *  by filter-iteration logic. */
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
