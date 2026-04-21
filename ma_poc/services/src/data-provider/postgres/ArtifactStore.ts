/**
 * @file ArtifactStore.ts (postgres adapter)
 * @description Reads run artifacts from the four dedicated tables:
 *   property_reports, llm_reports, llm_property_details, llm_diagnostics,
 * plus profiles from `scrape_profiles`.
 */

import type { IArtifactStore } from '../contracts.js';
import type { PgPool } from './client.js';

export class PgArtifactStore implements IArtifactStore {
  constructor(private readonly pool: PgPool) {}

  async getPropertyReport(runDate: string, canonicalId: string): Promise<string | null> {
    const { rows } = await this.pool.query<{ markdown: string }>(
      `select markdown from property_reports
         where run_date = $1 and canonical_id = $2`,
      [runDate, canonicalId],
    );
    return rows[0]?.markdown ?? null;
  }

  async getLlmReport(runDate: string): Promise<Record<string, unknown> | null> {
    const { rows } = await this.pool.query<{ payload: Record<string, unknown> }>(
      `select payload from llm_reports where run_date = $1`,
      [runDate],
    );
    return rows[0]?.payload ?? null;
  }

  async getLlmPropertyDetail(
    runDate: string,
    propertyId: string,
  ): Promise<Record<string, unknown> | null> {
    const { rows } = await this.pool.query<{ payload: Record<string, unknown> }>(
      `select payload from llm_property_details
         where run_date = $1 and property_id = $2`,
      [runDate, propertyId],
    );
    return rows[0]?.payload ?? null;
  }

  async getLlmDiagnostic(
    runDate: string,
    propertyId: string,
    kind: string,
  ): Promise<Record<string, unknown> | null> {
    const { rows } = await this.pool.query<{ payload: Record<string, unknown> }>(
      `select payload from llm_diagnostics
         where run_date = $1 and property_id = $2 and kind = $3`,
      [runDate, propertyId, kind],
    );
    return rows[0]?.payload ?? null;
  }

  async getProfile(canonicalId: string): Promise<Record<string, unknown> | null> {
    const { rows } = await this.pool.query<{ payload: Record<string, unknown> }>(
      `select payload from scrape_profiles where canonical_id = $1`,
      [canonicalId],
    );
    return rows[0]?.payload ?? null;
  }
}
