/**
 * @file retention.ts
 * @description Helpers for telling apart "this run was purged by the
 * retention sweep" from "this run never existed".
 *
 * Background: the Postgres sync (`ma_poc/scripts/sync_run_to_pg.py
 * :: _apply_retention`) deletes rows older than 3 days from every
 * per-run table (`property_snapshots`, `run_issues`, `run_ledger`,
 * `llm_reports`, `llm_property_details`, `llm_diagnostics`,
 * `scrape_events`, `dlq_entries`). When the API gets a request for a
 * run older than that window, the underlying store returns null —
 * indistinguishable from "we never had that run". This helper turns
 * that distinction into an explicit 410 Gone with a clear message
 * instead of a generic 404.
 *
 * The cutoff is calendar-based and identical to the cutoff
 * `_apply_retention` uses, so the two stay in sync without a DB probe.
 */

import type { Response } from 'express';

/** Must match the cap enforced by `_apply_retention()` in sync_run_to_pg.py. */
export const RETENTION_DAYS = 3;

/**
 * True when the given ISO date is older than the retention window —
 * meaning a null fetch from a per-run table is overwhelmingly likely
 * to be a retention purge rather than a missing run.
 *
 * Both inputs are interpreted in the server's local date — the
 * retention sweep also uses `date.today()` server-side, so the two
 * comparisons agree even on the boundary day.
 */
export function isPotentiallyPurged(runDate: string, today: Date = new Date()): boolean {
  const cutoff = new Date(today);
  cutoff.setDate(cutoff.getDate() - RETENTION_DAYS);
  // Compare as ISO date strings (lexicographic == chronological for YYYY-MM-DD).
  const cutoffIso = cutoff.toISOString().slice(0, 10);
  return runDate < cutoffIso;
}

/**
 * Standard 410 Gone response when a per-run artifact is absent and the
 * date is outside the retention window. Centralised so every route
 * uses the same payload shape — UI code can branch on `status`.
 */
export function sendPurged(res: Response, runDate: string, resource: string): void {
  res.status(410).json({
    status: 'purged',
    runDate,
    resource,
    retentionDays: RETENTION_DAYS,
    message:
      `This run is older than the ${RETENTION_DAYS}-day retention window and ` +
      `has been purged from durable storage. Only the last ${RETENTION_DAYS} days of run data are retained.`,
  });
}
