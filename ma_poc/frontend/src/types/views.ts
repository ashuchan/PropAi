export type ViewMode = 'editorial' | 'terminal' | 'spatial';
export type ExtractionTier = 'TIER_1_API' | 'TIER_2_JSONLD' | 'TIER_3_DOM' | 'TIER_4_LLM' | 'TIER_5_VISION' | 'FAILED';
export type ScrapeStatus = 'SUCCESS' | 'FAILED' | 'CARRIED_FORWARD' | 'SKIPPED' | 'SUCCESS_WITH_ERRORS';
export type PropertyStatus = 'ACTIVE' | 'LEASE_UP' | 'STABILISED' | 'OFFLINE';
