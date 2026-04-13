/**
 * @file IUnitService.ts
 * @description Interface for unit data access.
 */

import type { Unit } from '../types/property.js';
import type { FloorPlanGroup, UnitHistoryEntry } from '../types/unit.js';

/** Unit data access interface */
export interface IUnitService {
  getUnitsByProperty(propertyId: string): Promise<Unit[]>;
  getUnitsByFloorPlan(propertyId: string): Promise<FloorPlanGroup[]>;
  getUnitHistory(propertyId: string, unitId: string): Promise<UnitHistoryEntry[]>;
}
