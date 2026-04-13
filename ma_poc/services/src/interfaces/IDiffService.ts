/**
 * @file IDiffService.ts
 * @description Interface for daily diff access.
 */

import type { DailyDiff, ChangelogEntry } from '../types/diff.js';

/** Daily diff data access interface */
export interface IDiffService {
  getDailyDiff(date: string): Promise<DailyDiff>;
  getLatestDiff(): Promise<DailyDiff>;
  getPropertyChangelog(propertyId: string, days?: number): Promise<ChangelogEntry[]>;
}
