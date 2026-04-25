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
  // run_ledger.extra may carry the original tier label (TIER_1_API, etc.).
  // Without it we have to infer FAILED vs TIER_1_API vs TIER_3_DOM, which
  // is what the legacy code did and why /api/health/tiers used to show
  // a 2-bucket distribution while /api/properties/stats showed 3.
  extra?: Record<string, unknown> | null;
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

    // Authoritative counts come from the catalogue tables, not from the
    // run report — the report only reflects what was scraped today (4482),
    // not what's in the DB (4981 properties / ~20K units). This is what
    // the user noticed was off in the dashboard.
    const stateList = await this.provider.propertyState.all();
    const totalProperties = stateList.length;
    const totalUnits = await this.provider.units.count();

    const report = (await this.provider.runs.getReport(latestDate)) as Record<string, unknown> | null;
    const alerts: HealthAlert[] = [];

    if (report) {
      const totals = (report.totals ?? {}) as Record<string, unknown>;
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
      return {
        lastRunDate: latestDate,
        lastRunStatus: String(report.exit_status ?? 'UNKNOWN'),
        successRate,
        totalProperties,
        totalUnits,
        avgDurationSeconds: Number(report.duration_s ?? 0),
        consecutiveFailureDays: 0,
        alerts,
      };
    }

    return {
      lastRunDate: latestDate, lastRunStatus: 'UNKNOWN', successRate: 0,
      totalProperties, totalUnits, avgDurationSeconds: 0,
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
      let tier: ExtractionTier;
      const rawTier = String(entry.extra?.extraction_tier ?? '').toUpperCase();
      if (rawTier.startsWith('TIER_') || rawTier === 'FAILED') {
        tier = rawTier as ExtractionTier;
      } else if (entry.status === 'FAILED') {
        tier = 'FAILED';
      } else if ((entry.units_count ?? 0) > 0) {
        // Same fallback as PropertyService.fastRowToSummary — agree across
        // endpoints so /api/properties/stats and /api/health/tiers match.
        tier = 'TIER_1_API';
      } else {
        tier = 'TIER_3_DOM';
      }
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
