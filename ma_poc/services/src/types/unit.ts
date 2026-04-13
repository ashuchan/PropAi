/**
 * @file unit.ts
 * @description Unit-specific types for history and grouping.
 */

import type { Unit } from './property.js';

/** Floor plan group with aggregated stats */
export interface FloorPlanGroup {
  floorPlanName: string;
  bedBath: string;
  totalUnits: number;
  availableUnits: number;
  avgRent: number;
  minRent: number;
  maxRent: number;
  avgSqft: number | null;
  units: Unit[];
}

/** Unit history entry for tracking changes over time */
export interface UnitHistoryEntry {
  date: string;
  askingRent: number;
  effectiveRent: number | null;
  availabilityStatus: string;
  concessions: string | null;
  changeType: 'NEW' | 'UPDATED' | 'UNCHANGED' | 'DISAPPEARED';
}
