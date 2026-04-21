/**
 * @file IRunService.ts
 * @description Interface for pipeline run history access.
 */

import type {
  RunSummary,
  RunDetail,
  LlmRunReport,
  LlmPropertyDetail,
} from '../types/run.js';

/** Pipeline run history interface */
export interface IRunService {
  getRunHistory(limit?: number): Promise<RunSummary[]>;
  getRunByDate(date: string): Promise<RunDetail | null>;
  getLatestRun(): Promise<RunDetail>;
  /**
   * Load the run-wide LLM aggregate report (`data/runs/{date}/llm_report.json`).
   * Returns null when the file does not exist (e.g. no LLM calls in that run).
   */
  getLlmReport(date: string): Promise<LlmRunReport | null>;
  /**
   * Load the per-property LLM detail file
   * (`data/runs/{date}/llm_report/{property_id}.json`).
   * Returns null when the file does not exist.
   */
  getLlmPropertyDetail(date: string, propertyId: string): Promise<LlmPropertyDetail | null>;
}
