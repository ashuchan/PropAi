/**
 * @file health.ts
 * @description System health and monitoring types.
 */

import type { ExtractionTier } from './common.js';

/** Overall system health summary */
export interface HealthSummary {
  lastRunDate: string;
  lastRunStatus: string;
  successRate: number;
  totalProperties: number;
  totalUnits: number;
  avgDurationSeconds: number;
  consecutiveFailureDays: number;
  alerts: HealthAlert[];
}

/** Health alert */
export interface HealthAlert {
  severity: 'ERROR' | 'WARNING' | 'INFO';
  message: string;
  code: string;
  timestamp: string;
}

/** Tier distribution stats */
export interface TierDistribution {
  tiers: Array<{
    tier: ExtractionTier;
    count: number;
    percentage: number;
  }>;
  total: number;
}

/** Failure record for analysis */
export interface FailureRecord {
  propertyId: string;
  propertyName: string;
  errorCode: string;
  errorMessage: string;
  consecutiveFailures: number;
  lastFailureDate: string;
}

/** Entity resolution statistics */
export interface EntityResolutionStats {
  totalCanonicalIds: number;
  totalRawIds: number;
  mergedCount: number;
  unresolved: number;
  resolutionRate: number;
}
