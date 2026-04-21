/**
 * @file HealthService.ts
 * @description Impl-agnostic health service. Reads the latest run report,
 * ledger, and property state through the provider.
 */

import type { IDataProvider } from '../data-provider/contracts.js';
import type { IHealthService } from '../interfaces/IHealthService.js';
import type {
  HealthSummary, HealthAlert, TierDistribution, FailureRecord, EntityResolutionStats,
} from '../types/health.js';
import type { ExtractionTier } from '../types/common.js';

interface LedgerEntry {
  canonical_id: string;
  status: string;
  units_count: number;
}

export class HealthService implements IHealthService {
  constructor(private readonly provider: IDataProvider) {}

  async getHealthSummary(): Promise<HealthSummary> {
    const latestDate = await this.provider.runs.getLatestDate();
    if (!latestDate) {
      return {
        lastRunDate: '', lastRunStatus: 'UNKNOWN', successRate: 0,
        totalProperties: 0, totalUnits: 0, avgDurationSeconds: 0,
        consecutiveFailureDays: 0, alerts: [],
      };
    }

    const report = (await this.provider.runs.getReport(latestDate)) as Record<string, unknown> | null;
    const alerts: HealthAlert[] = [];

    if (report) {
      const totals = (report.totals ?? {}) as Record<string, unknown>;
      // Support both new (`properties`/`succeeded`) and legacy
      // (`rows_processed`/`rows_succeeded`) shapes.
      const total = Number(totals.properties ?? totals.rows_processed ?? 0);
      const succeeded = Number(totals.succeeded ?? totals.rows_succeeded ?? 0);
      const successRate = total > 0 ? succeeded / total : 0;
      const issues = (report.issues ?? {}) as Record<string, unknown>;
      const bySeverity = (issues.by_severity ?? {}) as Record<string, unknown>;
      const errorCount = Number(bySeverity.ERROR ?? 0);
      if (successRate < 0.95) {
        alerts.push({
          severity: 'WARNING',
          message: `Success rate ${(successRate * 100).toFixed(1)}% is below 95% target`,
          code: 'LOW_SUCCESS_RATE',
          timestamp: latestDate,
        });
      }
      if (errorCount > 10) {
        alerts.push({
          severity: 'ERROR',
          message: `${errorCount} errors in latest run`,
          code: 'HIGH_ERROR_COUNT',
          timestamp: latestDate,
        });
      }
      const stateDiff = (report.state_diff ?? {}) as Record<string, unknown>;
      return {
        lastRunDate: latestDate,
        lastRunStatus: String(report.exit_status ?? 'UNKNOWN'),
        successRate,
        totalProperties: total,
        totalUnits: Number(stateDiff.units_extracted ?? 0),
        avgDurationSeconds: Number(report.duration_s ?? 0),
        consecutiveFailureDays: 0,
        alerts,
      };
    }

    return {
      lastRunDate: latestDate, lastRunStatus: 'UNKNOWN', successRate: 0,
      totalProperties: 0, totalUnits: 0, avgDurationSeconds: 0,
      consecutiveFailureDays: 0, alerts,
    };
  }

  async getTierDistribution(): Promise<TierDistribution> {
    const latest = await this.provider.runs.getLatestDate();
    if (!latest) return { tiers: [], total: 0 };

    const ledger = (await this.provider.runs.readLedger(latest)) as LedgerEntry[];
    const counts: Record<string, number> = {};
    let total = 0;
    for (const entry of ledger) {
      const tier = entry.status === 'FAILED' ? 'FAILED' : entry.units_count > 0 ? 'TIER_1_API' : 'FAILED';
      counts[tier] = (counts[tier] || 0) + 1;
      total++;
    }

    const tiers = Object.entries(counts).map(([tier, count]) => ({
      tier: tier as ExtractionTier,
      count,
      percentage: total > 0 ? count / total : 0,
    }));
    return { tiers, total };
  }

  async getTopFailures(limit = 20): Promise<FailureRecord[]> {
    const latest = await this.provider.runs.getLatestDate();
    if (!latest) return [];
    const report = (await this.provider.runs.getReport(latest)) as Record<string, unknown> | null;
    if (!report) return [];
    const failed = (report.failed_properties as Array<Record<string, unknown>> | undefined) ?? [];
    const stateList = await this.provider.propertyState.all();
    const stateByCid = new Map(stateList.map((s) => [s.canonicalId, s]));
    return failed.slice(0, limit).map((fp) => {
      const cid = String(fp.canonical_id ?? '');
      const state = stateByCid.get(cid);
      const reason = String(fp.reason ?? '');
      return {
        propertyId: cid,
        propertyName: state?.projName ?? state?.name ?? cid,
        errorCode: reason,
        errorMessage: reason,
        consecutiveFailures: 1,
        lastFailureDate: latest,
      };
    });
  }

  async getEntityResolutionStats(): Promise<EntityResolutionStats> {
    const list = await this.provider.propertyState.all();
    const totalCanonicalIds = list.length;
    return {
      totalCanonicalIds,
      totalRawIds: totalCanonicalIds,
      mergedCount: 0,
      unresolved: 0,
      resolutionRate: 1.0,
    };
  }
}
