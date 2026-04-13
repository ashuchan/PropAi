/**
 * @file IRunService.ts
 * @description Interface for pipeline run history access.
 */

import type { RunSummary, RunDetail } from '../types/run.js';

/** Pipeline run history interface */
export interface IRunService {
  getRunHistory(limit?: number): Promise<RunSummary[]>;
  getRunByDate(date: string): Promise<RunDetail | null>;
  getLatestRun(): Promise<RunDetail>;
}
