/**
 * @file JsonFileRunService.ts
 * @description Reads pipeline run history from data/runs/ directories.
 */

import { readdir } from 'node:fs/promises';
import { join } from 'node:path';
import type { IRunService } from '../../interfaces/IRunService.js';
import type { RunSummary, RunDetail } from '../../types/run.js';
import { readJsonFile, getRunDates, runPath } from './dataLoader.js';

interface RawReport {
  run_date: string;
  retry_mode: string;
  started_at: string;
  finished_at: string;
  duration_s: number;
  exit_status: string;
  csv_path: string;
  data_dir: string;
  totals: {
    csv_rows_total: number;
    rows_eligible: number;
    rows_processed: number;
    rows_succeeded: number;
    rows_failed: number;
    properties_in_output: number;
  };
  ledger_after_retry: Record<string, number>;
  issues: {
    total: number;
    by_severity: Record<string, number>;
    by_code: Record<string, number>;
  };
  state_diff: {
    carry_forward_count: number;
    units_extracted: number;
    units_new: number;
    units_updated: number;
    units_unchanged: number;
    units_disappeared: number;
    units_carried_forward: number;
  };
  failed_properties: Array<{
    row_index: number;
    canonical_id: string;
    reason: string;
  }>;
}

export class JsonFileRunService implements IRunService {
  constructor(private readonly dataDir: string) {}

  async getRunHistory(limit: number = 30): Promise<RunSummary[]> {
    const dates = await getRunDates(this.dataDir);
    const summaries: RunSummary[] = [];
    for (const date of dates.slice(0, limit)) {
      const report = await this.loadReport(date);
      if (report) summaries.push(this.toSummary(report));
    }
    return summaries;
  }

  async getRunByDate(date: string): Promise<RunDetail | null> {
    const report = await this.loadReport(date);
    if (!report) return null;
    return this.toDetail(report);
  }

  async getLatestRun(): Promise<RunDetail> {
    const dates = await getRunDates(this.dataDir);
    if (dates.length === 0) throw new Error('No runs found');
    const report = await this.loadReport(dates[0]);
    if (!report) throw new Error(`Report not found for ${dates[0]}`);
    return this.toDetail(report);
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

  private toSummary(r: RawReport): RunSummary {
    const total = r.totals.rows_processed || r.totals.csv_rows_total;
    const succeeded = r.totals.rows_succeeded;
    return {
      date: r.run_date, startedAt: r.started_at, finishedAt: r.finished_at,
      durationSeconds: r.duration_s, exitStatus: r.exit_status,
      totalProperties: total, succeeded, failed: r.totals.rows_failed,
      successRate: total > 0 ? succeeded / total : 0,
      unitsExtracted: r.state_diff?.units_extracted ?? 0,
    };
  }

  private toDetail(r: RawReport): RunDetail {
    return {
      ...this.toSummary(r),
      retryMode: r.retry_mode, csvPath: r.csv_path,
      totals: {
        csvRowsTotal: r.totals.csv_rows_total, rowsEligible: r.totals.rows_eligible,
        rowsProcessed: r.totals.rows_processed, rowsSucceeded: r.totals.rows_succeeded,
        rowsFailed: r.totals.rows_failed, propertiesInOutput: r.totals.properties_in_output,
      },
      ledgerAfterRetry: r.ledger_after_retry || {},
      issues: {
        total: r.issues?.total ?? 0,
        bySeverity: r.issues?.by_severity ?? {},
        byCode: r.issues?.by_code ?? {},
      },
      stateDiff: {
        carryForwardCount: r.state_diff?.carry_forward_count ?? 0,
        unitsExtracted: r.state_diff?.units_extracted ?? 0,
        unitsNew: r.state_diff?.units_new ?? 0,
        unitsUpdated: r.state_diff?.units_updated ?? 0,
        unitsUnchanged: r.state_diff?.units_unchanged ?? 0,
        unitsDisappeared: r.state_diff?.units_disappeared ?? 0,
        unitsCarriedForward: r.state_diff?.units_carried_forward ?? 0,
      },
      failedProperties: (r.failed_properties || []).map(fp => ({
        rowIndex: fp.row_index, canonicalId: fp.canonical_id, reason: fp.reason,
      })),
    };
  }
}
