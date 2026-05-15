/**
 * @file IUnitService.ts
 * @description Interface for unit data access.
 */

import type { Unit } from '../types/property.js';
import type { DataScope } from '../types/common.js';
import type { FloorPlanGroup, UnitHistoryEntry } from '../types/unit.js';

/** Unit data access interface */
export interface IUnitService {
  getUnitsByProperty(propertyId: string, scope?: DataScope): Promise<Unit[]>;
  getUnitsByFloorPlan(propertyId: string, scope?: DataScope): Promise<FloorPlanGroup[]>;
  getUnitHistory(propertyId: string, unitId: string): Promise<UnitHistoryEntry[]>;
}
