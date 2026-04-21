/**
 * @file RunStore.ts (postgres adapter)
 * @description Run registry (runs), per-run report (run_reports), per-run
 * ledger (run_ledger). The report shape is preserved verbatim from the
 * filesystem — we reconstruct it from the dedicated columns + `extra` JSON.
 */

import type {
  IRunStore,
  RawLedgerEntry,
  RawRunReport,
} from '../contracts.js';
import type { PgPool } from './client.js';

interface LedgerRow {
  canonical_id: string | null;
  status: string | null;
  units_count: number | null;
  carry_forward_used: boolean | null;
  scrape_failed: boolean | null;
  error_count: number | null;
  warning_count: number | null;
  url: string | null;
  timestamp: string | null;
  extra: Record<string, unknown> | null;
}

export class PgRunStore implements IRunStore {
  constructor(private readonly pool: PgPool) {}

  async listDates(): Promise<string[]> {
    const { rows } = await this.pool.query<{ run_date: string }>(
      `select run_date from runs order by run_date desc`,
    );
    return rows.map((r) => r.run_date);
  }

  async getLatestDate(): Promise<string | null> {
    const dates = await this.listDates();
    return dates[0] ?? null;
  }

  async getReport(runDate: string): Promise<RawRunReport | null> {
    const { rows } = await this.pool.query<{
      run_date: string;
      generated_at: string;
      totals: unknown;
      tier_distribution: unknown;
      cost: unknown;
      slo_violations: unknown;
      extra: Record<string, unknown> | null;
    }>(
      `select run_date, generated_at, totals, tier_distribution, cost,
              slo_violations, extra
         from run_reports where run_date = $1`,
      [runDate],
    );
    const row = rows[0];
    if (!row) return null;
    // Reconstruct a dict that looks like the original report.json, with any
    // extra fields (arbitrary Jugnu/legacy additions) spread in.
    return {
      run_date: row.run_date,
      generated_at: row.generated_at,
      totals: row.totals,
      tier_distribution: row.tier_distribution,
      cost: row.cost,
      slo_violations: row.slo_violations,
      ...(row.extra ?? {}),
    };
  }

  async readLedger(runDate: string): Promise<RawLedgerEntry[]> {
    const { rows } = await this.pool.query<LedgerRow>(
      `select canonical_id, status, units_count, carry_forward_used,
              scrape_failed, error_count, warning_count, url, timestamp, extra
         from run_ledger where run_date = $1 order by seq`,
      [runDate],
    );
    return rows.map((r) => ({
      canonical_id: r.canonical_id ?? '',
      status: r.status ?? '',
      units_count: r.units_count ?? 0,
      carry_forward_used: r.carry_forward_used ?? undefined,
      scrape_failed: r.scrape_failed ?? undefined,
      error_count: r.error_count ?? undefined,
      warning_count: r.warning_count ?? undefined,
      url: r.url ?? undefined,
      timestamp: r.timestamp ?? undefined,
      ...(r.extra ?? {}),
    }));
  }
}
