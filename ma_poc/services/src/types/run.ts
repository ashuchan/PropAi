/**
 * @file run.ts
 * @description Run history and report types matching backend report_*.json format.
 */

/** Summary of a pipeline run */
export interface RunSummary {
  date: string;
  startedAt: string;
  finishedAt: string;
  durationSeconds: number;
  exitStatus: string;
  totalProperties: number;
  succeeded: number;
  failed: number;
  successRate: number;
  unitsExtracted: number;
}

/** Detailed run report */
export interface RunDetail extends RunSummary {
  retryMode: string;
  csvPath: string;
  totals: {
    csvRowsTotal: number;
    rowsEligible: number;
    rowsProcessed: number;
    rowsSucceeded: number;
    rowsFailed: number;
    propertiesInOutput: number;
  };
  ledgerAfterRetry: Record<string, number>;
  issues: {
    total: number;
    bySeverity: Record<string, number>;
    byCode: Record<string, number>;
  };
  stateDiff: {
    carryForwardCount: number;
    unitsExtracted: number;
    unitsNew: number;
    unitsUpdated: number;
    unitsUnchanged: number;
    unitsDisappeared: number;
    unitsCarriedForward: number;
  };
  failedProperties: Array<{
    rowIndex: number;
    canonicalId: string;
    reason: string;
  }>;
}
