/**
 * @file JsonFileHealthService.ts
 * @description Reads health and monitoring data from run reports and ledgers.
 */

import { readdir } from 'node:fs/promises';
import { join } from 'node:path';
import type { IHealthService } from '../../interfaces/IHealthService.js';
import type { HealthSummary, HealthAlert, TierDistribution, FailureRecord, EntityResolutionStats } from '../../types/health.js';
import type { ExtractionTier } from '../../types/common.js';
import { readJsonFile, readJsonlFile, getLatestRunDate, runPath, statePath } from './dataLoader.js';

interface RawReport {
  run_date: string;
  exit_status: string;
  duration_s: number;
  totals: { rows_processed: number; rows_succeeded: number; rows_failed: number; properties_in_output: number };
  issues: { total: number; by_severity: Record<string, number>; by_code: Record<string, number> };
  state_diff: { units_extracted: number };
  failed_properties: Array<{ canonical_id: string; reason: string }>;
}

interface RawLedgerEntry {
  canonical_id: string;
  status: string;
  units_count: number;
}

interface RawPropertyIndex {
  [key: string]: { canonical_id: string; name: string; last_scrape_status: string };
}

export class JsonFileHealthService implements IHealthService {
  constructor(private readonly dataDir: string) {}

  async getHealthSummary(): Promise<HealthSummary> {
    const latestDate = await getLatestRunDate(this.dataDir);
    if (!latestDate) {
      return { lastRunDate: '', lastRunStatus: 'UNKNOWN', successRate: 0, totalProperties: 0, totalUnits: 0, avgDurationSeconds: 0, consecutiveFailureDays: 0, alerts: [] };
    }

    const report = await this.loadReport(latestDate);
    const alerts: HealthAlert[] = [];

    if (report) {
      const successRate = report.totals.rows_processed > 0 ? report.totals.rows_succeeded / report.totals.rows_processed : 0;
      if (successRate < 0.95) {
        alerts.push({ severity: 'WARNING', message: `Success rate ${(successRate * 100).toFixed(1)}% is below 95% target`, code: 'LOW_SUCCESS_RATE', timestamp: latestDate });
      }
      if ((report.issues?.by_severity?.ERROR ?? 0) > 10) {
        alerts.push({ severity: 'ERROR', message: `${report.issues.by_severity.ERROR} errors in latest run`, code: 'HIGH_ERROR_COUNT', timestamp: latestDate });
      }
      return { lastRunDate: latestDate, lastRunStatus: report.exit_status, successRate, totalProperties: report.totals.rows_processed, totalUnits: report.state_diff?.units_extracted ?? 0, avgDurationSeconds: report.duration_s, consecutiveFailureDays: 0, alerts };
    }

    return { lastRunDate: latestDate, lastRunStatus: 'UNKNOWN', successRate: 0, totalProperties: 0, totalUnits: 0, avgDurationSeconds: 0, consecutiveFailureDays: 0, alerts };
  }

  async getTierDistribution(): Promise<TierDistribution> {
    const latestDate = await getLatestRunDate(this.dataDir);
    if (!latestDate) return { tiers: [], total: 0 };

    const ledger = await readJsonlFile<RawLedgerEntry>(runPath(this.dataDir, latestDate, 'ledger.jsonl'));
    const counts: Record<string, number> = {};
    let total = 0;

    for (const entry of ledger) {
      const tier = entry.status === 'FAILED' ? 'FAILED' : entry.units_count > 0 ? 'TIER_1_API' : 'FAILED';
      counts[tier] = (counts[tier] || 0) + 1;
      total++;
    }

    const tiers = Object.entries(counts).map(([tier, count]) => ({
      tier: tier as ExtractionTier, count, percentage: total > 0 ? count / total : 0,
    }));

    return { tiers, total };
  }

  async getTopFailures(limit: number = 20): Promise<FailureRecord[]> {
    const latestDate = await getLatestRunDate(this.dataDir);
    if (!latestDate) return [];

    const report = await this.loadReport(latestDate);
    if (!report) return [];

    const index = await readJsonFile<RawPropertyIndex>(statePath(this.dataDir, 'property_index.json'));

    return (report.failed_properties || []).slice(0, limit).map(fp => ({
      propertyId: fp.canonical_id,
      propertyName: index?.[fp.canonical_id]?.name || fp.canonical_id,
      errorCode: fp.reason, errorMessage: fp.reason,
      consecutiveFailures: 1, lastFailureDate: latestDate,
    }));
  }

  async getEntityResolutionStats(): Promise<EntityResolutionStats> {
    const index = await readJsonFile<RawPropertyIndex>(statePath(this.dataDir, 'property_index.json'));
    const totalCanonicalIds = index ? Object.keys(index).length : 0;
    return { totalCanonicalIds, totalRawIds: totalCanonicalIds, mergedCount: 0, unresolved: 0, resolutionRate: 1.0 };
  }

  private async loadReport(date: string): Promise<RawReport | null> {
    const dir = join(this.dataDir, 'runs', date);
    try {
      const entries = await readdir(dir);
      const reportFiles = entries.filter(e => e.startsWith('report') && e.endsWith('.json'));
      for (const file of reportFiles) {
        const report = await readJsonFile<RawReport>(join(dir, file));
        if (report) return report;
      }
    } catch {
      // directory not found
    }
    return null;
  }
}
