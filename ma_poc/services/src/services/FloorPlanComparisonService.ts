/**
 * @file FloorPlanComparisonService.ts
 * @description Service-layer wrapper over IFloorPlanComparisonStore. Provides
 * the overview (totals + per-property table) and per-property deep dive.
 *
 * Only the postgres provider exposes `floorPlanComparisons`. When backed by
 * json-file the service short-circuits with an empty overview so the API
 * route can return 200 with `{ runId: null, ... }` instead of crashing.
 */

import type { IDataProvider } from '../data-provider/contracts.js';
import type {
  FloorPlanComparisonDeepDive,
  FloorPlanComparisonOverview,
  FloorPlanComparisonPropertyRow,
  IFloorPlanComparisonService,
} from '../interfaces/IFloorPlanComparisonService.js';

const EMPTY_TOTALS: FloorPlanComparisonOverview['totals'] = {
  propertiesCompared: 0,
  propertiesWithAnyMatch: 0,
  csvRowsTotal: 0,
  matchedTotal: 0,
  nameMatchCount: 0,
  bedBathSqftMatchCount: 0,
  bedBathOnlyMatchCount: 0,
  unmatchedCount: 0,
  sqftWithinBufferCount: 0,
  floorPlansMissingInCsv: 0,
  dbFloorPlansTotal: 0,
};

export class FloorPlanComparisonService implements IFloorPlanComparisonService {
  constructor(private readonly provider: IDataProvider) {}

  isSupported(): boolean {
    return Boolean(this.provider.floorPlanComparisons);
  }

  async getLatestRunId(): Promise<string | null> {
    if (!this.provider.floorPlanComparisons) return null;
    return this.provider.floorPlanComparisons.getLatestRunId();
  }

  async getOverview(runId?: string): Promise<FloorPlanComparisonOverview> {
    if (!this.provider.floorPlanComparisons) {
      return { runId: null, availableRunIds: [], totals: EMPTY_TOTALS, properties: [] };
    }
    const store = this.provider.floorPlanComparisons;

    const availableRunIds = await store.listRunIds();
    const resolvedRunId = runId ?? availableRunIds[0] ?? null;
    if (!resolvedRunId) {
      return { runId: null, availableRunIds, totals: EMPTY_TOTALS, properties: [] };
    }

    const summaries = await store.getRunSummaries(resolvedRunId);
    const properties: FloorPlanComparisonPropertyRow[] = summaries.map((s) => ({
      canonicalId: s.canonicalId,
      apartmentId: s.apartmentId,
      projName: s.projName,
      csvRowsTotal: s.csvRowsTotal,
      dbFloorPlansTotal: s.dbFloorPlansTotal,
      matchedTotal: s.matchedTotal,
      matchRatePct: s.csvRowsTotal > 0 ? (s.matchedTotal * 100) / s.csvRowsTotal : 0,
      nameMatchCount: s.nameMatchCount,
      bedBathSqftMatchCount: s.bedBathSqftMatchCount,
      bedBathOnlyMatchCount: s.bedBathOnlyMatchCount,
      unmatchedCount: s.unmatchedCount,
      sqftWithinBufferCount: s.sqftWithinBufferCount,
      missingInCsvCount: s.missingInCsvCount,
    }));

    const totals = properties.reduce<FloorPlanComparisonOverview['totals']>(
      (acc, p) => ({
        propertiesCompared: acc.propertiesCompared + 1,
        propertiesWithAnyMatch: acc.propertiesWithAnyMatch + (p.matchedTotal > 0 ? 1 : 0),
        csvRowsTotal: acc.csvRowsTotal + p.csvRowsTotal,
        matchedTotal: acc.matchedTotal + p.matchedTotal,
        nameMatchCount: acc.nameMatchCount + p.nameMatchCount,
        bedBathSqftMatchCount: acc.bedBathSqftMatchCount + p.bedBathSqftMatchCount,
        bedBathOnlyMatchCount: acc.bedBathOnlyMatchCount + p.bedBathOnlyMatchCount,
        unmatchedCount: acc.unmatchedCount + p.unmatchedCount,
        sqftWithinBufferCount: acc.sqftWithinBufferCount + p.sqftWithinBufferCount,
        floorPlansMissingInCsv: acc.floorPlansMissingInCsv + p.missingInCsvCount,
        dbFloorPlansTotal: acc.dbFloorPlansTotal + p.dbFloorPlansTotal,
      }),
      { ...EMPTY_TOTALS },
    );

    return {
      runId: resolvedRunId,
      availableRunIds,
      totals,
      properties,
    };
  }

  async getPropertyDeepDive(
    runId: string,
    canonicalId: string,
  ): Promise<FloorPlanComparisonDeepDive | null> {
    if (!this.provider.floorPlanComparisons) return null;
    const store = this.provider.floorPlanComparisons;

    const summaries = await store.getRunSummaries(runId);
    const summary = summaries.find((s) => s.canonicalId === canonicalId);
    if (!summary) return null;

    const [rows, missingInCsv] = await Promise.all([
      store.getRowsForProperty(runId, canonicalId),
      store.getMissingInCsv(runId, canonicalId),
    ]);

    return {
      runId,
      canonicalId,
      summary: {
        canonicalId: summary.canonicalId,
        apartmentId: summary.apartmentId,
        projName: summary.projName,
        csvRowsTotal: summary.csvRowsTotal,
        dbFloorPlansTotal: summary.dbFloorPlansTotal,
        matchedTotal: summary.matchedTotal,
        matchRatePct: summary.csvRowsTotal > 0
          ? (summary.matchedTotal * 100) / summary.csvRowsTotal
          : 0,
        nameMatchCount: summary.nameMatchCount,
        bedBathSqftMatchCount: summary.bedBathSqftMatchCount,
        bedBathOnlyMatchCount: summary.bedBathOnlyMatchCount,
        unmatchedCount: summary.unmatchedCount,
        sqftWithinBufferCount: summary.sqftWithinBufferCount,
        missingInCsvCount: summary.missingInCsvCount,
      },
      rows,
      missingInCsv,
    };
  }
}
