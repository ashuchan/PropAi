/**
 * @file IHealthService.ts
 * @description Interface for system health monitoring.
 */

import type { HealthSummary, TierDistribution, FailureRecord, EntityResolutionStats } from '../types/health.js';

/** System health monitoring interface */
export interface IHealthService {
  getHealthSummary(): Promise<HealthSummary>;
  getTierDistribution(): Promise<TierDistribution>;
  getTopFailures(limit?: number): Promise<FailureRecord[]>;
  getEntityResolutionStats(): Promise<EntityResolutionStats>;
}
