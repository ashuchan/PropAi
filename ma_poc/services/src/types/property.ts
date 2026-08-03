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
  galleryUrls?: string[];
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
  media: PropertyMedia;
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
  /** V2-only fields */
  emailAddress?: string | null;
  websiteDesign?: string | null;
  schemaVersion: SchemaVersion;
}

/** Individual rental unit */
export interface Unit {
  unitId: string;
  sourceUnitId?: string | null;
  canonicalUnitId?: string | null;
  unitHistoryKey?: string | null;
  unitHistoryKeyBasis?: string | null;
  unitHistoryKeyQuality?: string | null;
  unitHistoryKeyVersion?: string | null;
  propertyId: string;
  floorPlanType: string | null;
  marketRentLow: number;
  marketRentHigh: number;
  askingRent: number;
  effectiveRent: number | null;
  sqft: number | null;
  availabilityStatus: 'AVAILABLE' | 'UNAVAILABLE' | 'UNKNOWN';
  availableDate: string | null;
  availableDateRaw?: string | null;
  availabilityDateProvenance?: string | null;
  leaseLink: string;
  concessions: string | null;
  amenities: string | null;
  daysOnMarket: number | null;
  rentPerSqft: number | null;
  floorplanImageUrl: string | null;
  /** V2 fields — present only when data source is V2 schema */
  beds?: number | null;
  baths?: number | null;
  area?: number | null;
  floorPlanName?: string | null;
  floorPlanId?: string | null;
  floorPlanNameProvenance?: string | null;
  unitName?: string | null;
  floor?: string | null;
  building?: string | null;
  buildingId?: string | null;
  buildingIdSource?: string | null;
  areaSqft?: number | null;
  areaIsPublished?: boolean | null;
  areaLow?: number | null;
  areaHigh?: number | null;
  areaRange?: string | null;
  areaRangeRaw?: string | null;
  areaValueType?: string | null;
  areaProvenance?: string | null;
  areaSourceUrl?: string | null;
  rentRange?: string | null;
  rentRangeRaw?: string | null;
  rentIsRange?: boolean | null;
  rentProvenance?: string | null;
  extractionTier?: string | null;
  sourceIds?: Record<string, unknown> | null;
  sourceResponseSha256?: string | null;
  sourceResponseUrl?: string | null;
  sourceRecordLocator?: string | null;
  sourceParentRecordLocator?: string | null;
  sourceAssetUrl?: string | null;
  sourceAssetSha256?: string | null;
  identityQuality?: string | null;
  unitIdAliases?: string[];
  unitIdAliasSources?: Record<string, unknown>[];
  leaseTerm?: number | null;
  moveInDate?: string | null;
  dateCaptured?: string | null;
}

/** Schema version indicator */
export type SchemaVersion = 'v1' | 'v2';

/** Property media — screenshots and images for property and floor plans */
export interface PropertyMedia {
  heroImageUrl: string | null;
  galleryUrls: string[];
  screenshots: {
    pricingPage: string | null;
    banner: string | null;
    homepage: string | null;
  };
  floorPlanImages: FloorPlanImage[];
}

/** Floor plan image reference */
export interface FloorPlanImage {
  floorPlanName: string;
  imageUrl: string;
  unitIds: string[];
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
