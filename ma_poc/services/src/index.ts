/**
 * @file index.ts
 * @description Public API for @ma-poc/services package.
 */

// Types
export type { PropertySummary, Property, Unit, FloorPlan, MarketMetrics, ScrapeEvent, PropertyAggregates } from './types/property.js';
export type { FloorPlanGroup, UnitHistoryEntry } from './types/unit.js';
export type { RunSummary, RunDetail } from './types/run.js';
export type { DailyDiff, DiffSummary, RentChange, PropertyChange, ConcessionChange, ChangelogEntry } from './types/diff.js';
export type { HealthSummary, HealthAlert, TierDistribution, FailureRecord, EntityResolutionStats } from './types/health.js';
export type { PaginatedResult, PropertyFilters, SortOptions, ExtractionTier, ScrapeStatus, PropertyStatus } from './types/common.js';

// Interfaces
export type { IPropertyService, PropertyReport, PropertyProfile } from './interfaces/IPropertyService.js';
export type { IUnitService } from './interfaces/IUnitService.js';
export type { IRunService } from './interfaces/IRunService.js';
export type { IDiffService } from './interfaces/IDiffService.js';
export type { IHealthService } from './interfaces/IHealthService.js';

// Factory
export { createServices } from './factory.js';
export type { ServiceConfig, ServiceImplementation } from './factory.js';

// Logger
export { logger } from './logger.js';
