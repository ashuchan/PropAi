/**
 * @file diff.ts
 * @description Daily diff types for tracking changes between runs.
 */

/** Summary of changes between two consecutive runs */
export interface DailyDiff {
  date: string;
  previousDate: string;
  summary: DiffSummary;
  rentChanges: RentChange[];
  propertyChanges: PropertyChange[];
  concessionChanges: ConcessionChange[];
}

/** High-level diff metrics */
export interface DiffSummary {
  rentsIncreased: number;
  rentsDecreased: number;
  newAvailable: number;
  becameLeased: number;
  newConcessions: number;
  disappearedUnits: number;
}

/** Rent change detail */
export interface RentChange {
  propertyId: string;
  propertyName: string;
  unitId: string;
  previousRent: number;
  currentRent: number;
  change: number;
  changePercent: number;
  direction: 'up' | 'down';
}

/** Property-level change */
export interface PropertyChange {
  propertyId: string;
  propertyName: string;
  changeType: 'NEW' | 'UPDATED' | 'DISAPPEARED' | 'STATUS_CHANGE';
  details: string;
  timestamp: string;
}

/** Concession change tracking */
export interface ConcessionChange {
  propertyId: string;
  propertyName: string;
  previousConcession: string | null;
  currentConcession: string | null;
  changeType: 'NEW' | 'REMOVED' | 'MODIFIED';
}

/** Changelog entry for property history */
export interface ChangelogEntry {
  date: string;
  changeType: string;
  field: string;
  previousValue: string | number | null;
  currentValue: string | number | null;
  description: string;
}
