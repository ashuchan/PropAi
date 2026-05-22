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
  /** Short scan-friendly summary of ``activeConcession`` produced by
   *  the deterministic enricher (``utils/concession.ts``). Set
   *  when the raw text parsed into a recognised offer shape (free
   *  rent / dollar off / waived fee / etc); ``null`` otherwise.
   *  Frontend prefers this for display; falls back to
   *  ``activeConcession`` when the banner is null. */
  concessionBanner: string | null;
  /** Canonical offer-type taxonomy of the primary atom, e.g.
   *  ``"free_rent"``, ``"dollar_off"``, ``"waived_fee"``. Filterable
   *  in dashboards; null when the raw text didn't parse. */
  concessionOfferType: string | null;
  /** What the discount applies TO — ``"rent"``, ``"app_fee"``,
   *  ``"move_in_cost"``, etc. null when not classified. */
  concessionTarget: string | null;
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

/** Canonical availability statuses surfaced by the API contract.
 *
 * 2026-05-20: widened from the previous 3-value enum
 * (``AVAILABLE | UNAVAILABLE | UNKNOWN``) to include the WAITLIST and
 * COMING_SOON values the Python normaliser emits. Pre-widening, the
 * service layer derived the value from ``available_date`` truthiness
 * and shipped ``UNKNOWN`` whenever the typed date was null — even when
 * the producer explicitly told us the unit was AVAILABLE. The wider
 * enum lets us pass the real signal through to the UI.
 */
export type AvailabilityStatus =
  | 'AVAILABLE'
  | 'UNAVAILABLE'
  | 'WAITLIST'
  | 'COMING_SOON'
  | 'UNKNOWN';

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
  availabilityStatus: AvailabilityStatus;
  availableDate: string | null;
  /** Producer-literal availability string. Populated even when
   *  ``availableDate`` is null because the producer's value couldn't
   *  normalise to ISO (e.g. ``"Available 7/24"`` — year missing,
   *  ``"Late August"``, ``"Available Now"``). UI surfaces this when
   *  the typed column is empty so users see the website's actual text.
   *  Added 2026-05-20; nullable to keep the field optional for V1
   *  schema-version payloads. */
  availableDateRaw?: string | null;
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
