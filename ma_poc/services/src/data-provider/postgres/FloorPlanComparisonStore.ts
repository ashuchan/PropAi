/**
 * @file FloorPlanComparisonStore.ts (postgres adapter)
 * @description Reads CSV-vs-DB floor plan comparison results written by
 * `ma_poc/scripts/floor_plans/compare.py`. Two backing tables:
 * `floor_plan_comparison_runs` (per-property summary) and
 * `floor_plan_comparison_rows` (per-CSV-row detail). Re-running the util
 * with the same run_id replaces both tables for that run.
 *
 * The "missing in CSV" view is computed live by anti-joining the property's
 * `units` against the matched DB plans for the run — no separate table.
 */

import type { PgPool } from './client.js';
import type {
  FloorPlanComparisonExtraDbPlan,
  FloorPlanComparisonRowRow,
  FloorPlanComparisonRunRow,
  IFloorPlanComparisonStore,
} from '../contracts.js';

interface RawRunRow {
  run_id: string;
  canonical_id: string;
  apartment_id: number | null;
  proj_name: string | null;
  csv_rows_total: number;
  db_floor_plans_total: number;
  matched_total: number;
  name_match_count: number;
  bed_bath_sqft_match_count: number;
  bed_bath_only_match_count: number;
  unmatched_count: number;
  sqft_within_buffer_count: number;
  missing_in_csv_count: number;
  created_at: string | Date;
}

interface RawRowRow {
  id: number;
  run_id: string;
  canonical_id: string | null;
  csv_apartment_id: number | null;
  csv_floorplan_number: string | null;
  csv_description: string | null;
  csv_bed: number | null;
  csv_bath: number | null;
  csv_area: number | null;
  match_method: string;
  match_score: number | null;
  sqft_within_buffer: boolean | null;
  sqft_delta: number | null;
  db_floor_plan_name: string | null;
  db_beds: number | null;
  db_baths: number | null;
  db_area: number | null;
  db_unit_count: number | null;
}

export class PgFloorPlanComparisonStore implements IFloorPlanComparisonStore {
  constructor(private readonly pool: PgPool) {}

  async getLatestRunId(): Promise<string | null> {
    const { rows } = await this.pool.query<{ run_id: string }>(
      `select run_id from floor_plan_comparison_runs
         order by created_at desc limit 1`,
    );
    return rows[0]?.run_id ?? null;
  }

  async listRunIds(): Promise<string[]> {
    const { rows } = await this.pool.query<{ run_id: string }>(
      `select run_id, max(created_at) as created_at
         from floor_plan_comparison_runs
         group by run_id
         order by created_at desc`,
    );
    return rows.map((r) => r.run_id);
  }

  async getRunSummaries(runId: string): Promise<FloorPlanComparisonRunRow[]> {
    const { rows } = await this.pool.query<RawRunRow>(
      `select run_id, canonical_id, apartment_id, proj_name,
              csv_rows_total, db_floor_plans_total, matched_total,
              name_match_count, bed_bath_sqft_match_count,
              bed_bath_only_match_count, unmatched_count,
              sqft_within_buffer_count, missing_in_csv_count, created_at
         from floor_plan_comparison_runs
         where run_id = $1
         order by proj_name asc nulls last`,
      [runId],
    );
    return rows.map((r) => ({
      runId: r.run_id,
      canonicalId: r.canonical_id,
      apartmentId: r.apartment_id,
      projName: r.proj_name,
      csvRowsTotal: r.csv_rows_total,
      dbFloorPlansTotal: r.db_floor_plans_total,
      matchedTotal: r.matched_total,
      nameMatchCount: r.name_match_count,
      bedBathSqftMatchCount: r.bed_bath_sqft_match_count,
      bedBathOnlyMatchCount: r.bed_bath_only_match_count,
      unmatchedCount: r.unmatched_count,
      sqftWithinBufferCount: r.sqft_within_buffer_count,
      missingInCsvCount: r.missing_in_csv_count,
      createdAt: typeof r.created_at === 'string' ? r.created_at : r.created_at.toISOString(),
    }));
  }

  async getRowsForProperty(
    runId: string,
    canonicalId: string,
  ): Promise<FloorPlanComparisonRowRow[]> {
    const { rows } = await this.pool.query<RawRowRow>(
      `select id, run_id, canonical_id, csv_apartment_id, csv_floorplan_number,
              csv_description, csv_bed, csv_bath, csv_area,
              match_method, match_score, sqft_within_buffer, sqft_delta,
              db_floor_plan_name, db_beds, db_baths, db_area, db_unit_count
         from floor_plan_comparison_rows
         where run_id = $1 and canonical_id = $2
         order by csv_floorplan_number nulls last, id asc`,
      [runId, canonicalId],
    );
    return rows.map((r) => ({
      id: r.id,
      runId: r.run_id,
      canonicalId: r.canonical_id,
      csvApartmentId: r.csv_apartment_id,
      csvFloorplanNumber: r.csv_floorplan_number,
      csvDescription: r.csv_description,
      csvBed: r.csv_bed,
      csvBath: r.csv_bath,
      csvArea: r.csv_area,
      matchMethod: r.match_method,
      matchScore: r.match_score,
      sqftWithinBuffer: r.sqft_within_buffer,
      sqftDelta: r.sqft_delta,
      dbFloorPlanName: r.db_floor_plan_name,
      dbBeds: r.db_beds,
      dbBaths: r.db_baths,
      dbArea: r.db_area,
      dbUnitCount: r.db_unit_count,
    }));
  }

  /**
   * Anti-join: every distinct floor plan in `units` for this property minus
   * the ones already matched by a CSV row in this run. Lets the deep-dive
   * page show the inverse — what plans the CSV is missing.
   */
  async getMissingInCsv(
    runId: string,
    canonicalId: string,
  ): Promise<FloorPlanComparisonExtraDbPlan[]> {
    const { rows } = await this.pool.query<{
      floor_plan_name: string | null;
      beds: number | null;
      baths: number | null;
      area: number | null;
      unit_count: string | number;
    }>(
      `with db_groups as (
         select floor_plan_name, beds, baths, area, count(*) as unit_count
           from units
          where canonical_id = $2
          group by floor_plan_name, beds, baths, area
       ),
       matched as (
         select distinct lower(coalesce(db_floor_plan_name, '')) as fpn,
                db_beds, db_baths, db_area
           from floor_plan_comparison_rows
          where run_id = $1
            and canonical_id = $2
            and db_floor_plan_name is not null OR db_beds is not null
              OR db_baths is not null OR db_area is not null
       )
       select g.floor_plan_name, g.beds, g.baths, g.area, g.unit_count
         from db_groups g
        where not exists (
          select 1 from matched m
           where lower(coalesce(g.floor_plan_name, '')) = m.fpn
             and g.beds is not distinct from m.db_beds
             and g.baths is not distinct from m.db_baths
             and g.area is not distinct from m.db_area
        )
        order by g.beds nulls last, g.baths nulls last, g.area nulls last`,
      [runId, canonicalId],
    );
    return rows.map((r) => ({
      floorPlanName: r.floor_plan_name,
      beds: r.beds,
      baths: r.baths,
      area: r.area,
      unitCount: typeof r.unit_count === 'string' ? parseInt(r.unit_count, 10) : r.unit_count,
    }));
  }
}
