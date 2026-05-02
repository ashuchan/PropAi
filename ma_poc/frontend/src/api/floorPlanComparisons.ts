/**
 * @file floorPlanComparisons.ts
 * @description Frontend API client for /api/floor-plan-comparisons.
 * Mirrors the contracts in ma_poc/services/src/interfaces/IFloorPlanComparisonService.ts.
 */

import { apiClient } from './client';

export interface FloorPlanComparisonTotals {
  propertiesCompared: number;
  propertiesWithAnyMatch: number;
  csvRowsTotal: number;
  matchedTotal: number;
  nameMatchCount: number;
  bedBathSqftMatchCount: number;
  bedBathOnlyMatchCount: number;
  unmatchedCount: number;
  sqftWithinBufferCount: number;
  floorPlansMissingInCsv: number;
  dbFloorPlansTotal: number;
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

export interface FloorPlanComparisonOverview {
  runId: string | null;
  availableRunIds: string[];
  totals: FloorPlanComparisonTotals;
  properties: FloorPlanComparisonPropertyRow[];
}

export interface FloorPlanComparisonRowDetail {
  id: number;
  runId: string;
  canonicalId: string | null;
  csvApartmentId: number | null;
  csvFloorplanNumber: string | null;
  csvDescription: string | null;
  csvBed: number | null;
  csvBath: number | null;
  csvArea: number | null;
  matchMethod: string;
  matchScore: number | null;
  sqftWithinBuffer: boolean | null;
  sqftDelta: number | null;
  dbFloorPlanName: string | null;
  dbBeds: number | null;
  dbBaths: number | null;
  dbArea: number | null;
  dbUnitCount: number | null;
}

export interface FloorPlanComparisonExtraDbPlan {
  floorPlanName: string | null;
  beds: number | null;
  baths: number | null;
  area: number | null;
  unitCount: number;
}

export interface FloorPlanComparisonDeepDive {
  runId: string;
  canonicalId: string;
  summary: FloorPlanComparisonPropertyRow;
  rows: FloorPlanComparisonRowDetail[];
  missingInCsv: FloorPlanComparisonExtraDbPlan[];
}

export interface FloorPlanComparisonMeta {
  supported: boolean;
  latestRunId: string | null;
}

export async function fetchFloorPlanComparisonMeta(): Promise<FloorPlanComparisonMeta> {
  const res = await apiClient.get<FloorPlanComparisonMeta>('/floor-plan-comparisons/_meta');
  return res.data;
}

export async function fetchFloorPlanComparisonOverview(
  runId?: string,
): Promise<FloorPlanComparisonOverview> {
  const res = await apiClient.get<FloorPlanComparisonOverview>('/floor-plan-comparisons', {
    params: runId ? { runId } : {},
  });
  return res.data;
}

export async function fetchFloorPlanComparisonDeepDive(
  canonicalId: string,
  runId?: string,
): Promise<FloorPlanComparisonDeepDive> {
  const res = await apiClient.get<FloorPlanComparisonDeepDive>(
    `/floor-plan-comparisons/${encodeURIComponent(canonicalId)}`,
    { params: runId ? { runId } : {} },
  );
  return res.data;
}
