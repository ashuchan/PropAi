/**
 * @file IFloorPlanComparisonService.ts
 * @description Interface for the CSV-vs-DB floor-plan comparison service.
 * Backed by the floor_plan_comparison_runs / floor_plan_comparison_rows
 * tables; populated by `ma_poc/scripts/compare_floor_plans_csv.py`.
 */

import type {
  FloorPlanComparisonExtraDbPlan,
  FloorPlanComparisonRowRow,
} from '../data-provider/contracts.js';

export interface FloorPlanComparisonOverview {
  /** The run_id this overview is computed for. Null if no comparisons exist. */
  runId: string | null;
  /** All run_ids in descending recency order — for the run picker. */
  availableRunIds: string[];
  totals: {
    propertiesCompared: number;
    propertiesWithAnyMatch: number;
    csvRowsTotal: number;
    matchedTotal: number;
    nameMatchCount: number;
    bedBathSqftMatchCount: number;
    bedBathOnlyMatchCount: number;
    unmatchedCount: number;
    sqftWithinBufferCount: number;
    /** Sum across properties of "DB plans not matched to any CSV row". */
    floorPlansMissingInCsv: number;
    /** Sum across properties of all distinct DB floor plans. */
    dbFloorPlansTotal: number;
  };
  properties: FloorPlanComparisonPropertyRow[];
}

export interface FloorPlanComparisonPropertyRow {
  canonicalId: string;
  apartmentId: number | null;
  projName: string | null;
  csvRowsTotal: number;
  dbFloorPlansTotal: number;
  matchedTotal: number;
  matchRatePct: number;
  nameMatchCount: number;
  bedBathSqftMatchCount: number;
  bedBathOnlyMatchCount: number;
  unmatchedCount: number;
  sqftWithinBufferCount: number;
  missingInCsvCount: number;
}

export interface FloorPlanComparisonDeepDive {
  runId: string;
  canonicalId: string;
  summary: FloorPlanComparisonPropertyRow;
  rows: FloorPlanComparisonRowRow[];
  missingInCsv: FloorPlanComparisonExtraDbPlan[];
}

export interface IFloorPlanComparisonService {
  /** True when the underlying data provider supports comparisons. */
  isSupported(): boolean;
  /** Latest run_id, or null. */
  getLatestRunId(): Promise<string | null>;
  /** Top-level overview — totals + per-property scores. */
  getOverview(runId?: string): Promise<FloorPlanComparisonOverview>;
  /** Per-property deep-dive — every CSV row + missing DB plans. */
  getPropertyDeepDive(
    runId: string,
    canonicalId: string,
  ): Promise<FloorPlanComparisonDeepDive | null>;
}
