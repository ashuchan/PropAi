/**
 * @file property.ts
 * @description Property and unit type definitions matching backend data schema.
 */

import type { ExtractionTier, ScrapeStatus, PropertyStatus } from './common.js';

/** Summary property for list views */
export interface PropertySummary {
  id: string;
  name: string;
  address: string;
  city: string;
  state: string;
  zip: string;
  latitude: number;
  longitude: number;
  managementCompany: string;
  totalUnits: number;
  avgAskingRent: number;
  medianAskingRent: number;
  availabilityRate: number;
  availableUnits: number;
  extractionTier: ExtractionTier;
  scrapeStatus: ScrapeStatus;
  propertyStatus: PropertyStatus;
  yearBuilt: number | null;
  stories: number | null;
  activeConcession: string | null;
  lastScrapeTimestamp: string;
  carryForwardDays: number;
  imageUrl: string | null;
  websiteUrl: string;
  llmCostUsd: number;
  llmCallCount: number;
  llmTokensTotal: number;
}

/** Full property with units and metrics */
export interface Property extends PropertySummary {
  units: Unit[];
  floorPlans: FloorPlan[];
  marketMetrics: MarketMetrics;
  scrapeHistory: ScrapeEvent[];
  screenshotPaths: { pricingPage: string | null; banner: string | null };
  developmentCompany: string;
  propertyOwner: string;
  marketName: string;
  submarketName: string;
  region: string;
  phone: string;
  unitMix: string;
  assetGradeSubmarket: string;
  assetGradeMarket: string;
  averageUnitSizeSf: number | null;
}

/** Individual rental unit */
export interface Unit {
  unitId: string;
  propertyId: string;
  floorPlanType: string | null;
  marketRentLow: number;
  marketRentHigh: number;
  askingRent: number;
  effectiveRent: number | null;
  sqft: number | null;
  availabilityStatus: 'AVAILABLE' | 'UNAVAILABLE' | 'UNKNOWN';
  availableDate: string | null;
  leaseLink: string;
  concessions: string | null;
  amenities: string | null;
  daysOnMarket: number | null;
  rentPerSqft: number | null;
}

/** Floor plan grouping */
export interface FloorPlan {
  name: string;
  bedBath: string;
  count: number;
  availableCount: number;
  avgRent: number;
  minRent: number;
  maxRent: number;
  avgSqft: number | null;
  units: Unit[];
}

/** Market-level metrics for a property */
export interface MarketMetrics {
  minRent: number;
  maxRent: number;
  medianRent: number;
  avgRent: number;
  avgDaysOnMarket: number;
  avgSqft: number | null;
  avgRentPerSqft: number | null;
  occupancyRate: number;
}

/** Scrape event history entry */
export interface ScrapeEvent {
  timestamp: string;
  status: ScrapeStatus;
  tier: ExtractionTier | null;
  unitsCount: number;
  errorCount: number;
  warningCount: number;
}

/** Aggregate statistics for property collection */
export interface PropertyAggregates {
  totalProperties: number;
  totalUnits: number;
  avgRent: number;
  medianRent: number;
  availabilityRate: number;
  successRate: number;
  tierDistribution: Record<ExtractionTier, number>;
  cityDistribution: Record<string, number>;
}
