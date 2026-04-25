/**
 * @file contracts.ts
 * @description Data-provider interfaces for the API services layer.
 *
 * Mirrors ma_poc/data_provider/contracts.py: thin storage adapters that
 * return raw records. All business logic (filtering, pagination, ranking,
 * aggregation, v1↔v2 transformation) lives in the service layer above.
 *
 * Swap between `json-file` and `postgres` adapters via `DATA_PROVIDER` env var
 * — consumers never know which backend is answering.
 */

/** Raw per-run property payload. Shape depends on SCHEMA_VERSION of the run
 *  (v1 uses human-readable keys like 'Property Name'; v2 uses `apartment_id`,
 *  `proj_name`, etc.). Services detect and transform. */
export type RawRunProperty = Record<string, unknown>;

/** Raw run report — shape varies by pipeline (legacy vs Jugnu). */
export type RawRunReport = Record<string, unknown>;

/** One row in the run ledger (`runs/{date}/ledger.jsonl`). */
export interface RawLedgerEntry {
  canonical_id: string;
  status: string;
  units_count: number;
  carry_forward_used?: boolean;
  scrape_failed?: boolean;
  error_count?: number;
  warning_count?: number;
  url?: string;
  timestamp?: string;
  [key: string]: unknown;
}

/** Current-state entry for a property (`data/state/property_index.json` /
 *  `properties` table). All fields optional because the state file may be
 *  v1-shaped (`name`, `zip`) while the DB is v2-shaped (`proj_name`,
 *  `zip_code`). Adapters normalise via the alias fields below. */
export interface PropertyStateRecord {
  canonicalId: string;
  // V1 aliases (set when source is v1-shaped)
  name?: string | null;
  zip?: string | null;
  // V2 aliases (set when source is v2-shaped)
  projName?: string | null;
  apartmentId?: number | null;
  zipCode?: string | null;
  country?: string | null;
  phone?: string | null;
  emailAddress?: string | null;
  pmc?: string | null;
  websiteDesign?: string | null;
  concessions?: string | null;
  // Shared
  address?: string | null;
  city?: string | null;
  state?: string | null;
  website?: string | null;
  lastScrapeStatus?: string | null;
  lastUnitsCount?: number | null;
  // Full ISO timestamp of the most recent upsert. Slice `[:10]` for a date.
  lastSeenAt?: string | null;
  firstSeenDate?: string | null;
  /** Any fields the adapter didn't promote to first-class columns. */
  extra?: Record<string, unknown>;
}

/** Current-state unit entry (`data/state/unit_index.json` / `units` table). */
export interface UnitStateRecord {
  canonicalId: string;
  unitId: string;
  // V1 aliases
  marketRentLow?: number | null;
  marketRentHigh?: number | null;
  bedrooms?: number | null;
  bathrooms?: number | null;
  sqft?: number | null;
  // V2 aliases
  rentLow?: number | null;
  rentHigh?: number | null;
  beds?: number | null;
  baths?: number | null;
  area?: number | null;
  dateCaptured?: string | null;
  leaseTerm?: number | null;
  moveInDate?: string | null;
  // Shared
  availableDate?: string | null;
  concessions?: unknown;
  floorPlanName?: string | null;
  availabilityStatus?: string | null;
  firstSeenDate?: string | null;
  lastSeenAt?: string | null;
  carryforwardDays?: number | null;
  disappearedSince?: string | null;
  lastAbsentDate?: string | null;
  changedFields?: string[];
  extra?: Record<string, unknown>;
}

// ── Store interfaces ─────────────────────────────────────────────────────────

/**
 * Denormalised per-property row for list/aggregate endpoints. Built from
 * `properties` ⨝ aggregated `units` ⨝ latest `run_ledger` so we can render
 * /api/properties* without ever loading the per-run `property_snapshots`
 * JSONB payloads. Counts are authoritative because they come from the
 * `units` table, not from per-snapshot `len(payload.units)` (which
 * inflates totals by including duplicates / lease-term variants).
 */
export interface PropertySummaryRow {
  canonicalId: string;
  apartmentId: number | null;
  name: string;            // proj_name
  address: string;
  city: string;
  state: string;
  zip: string;             // zip_code
  pmc: string;             // management company
  phone: string;
  website: string;
  totalUnits: number;
  availableUnits: number;
  avgRent: number;
  medianRent: number;
  lastSeenAt: string | null;
  lastScrapeStatus: string | null;
  ledgerStatus: string | null;
  extractionTier: string | null;  // from ledger.extra->>'extraction_tier'
  concessions: string | null;
}

/** Per-run property payloads (properties.json / `property_snapshots` table). */
export interface IPropertyStore {
  listForRun(runDate: string): Promise<RawRunProperty[]>;
  getForRun(runDate: string, canonicalId: string): Promise<RawRunProperty | null>;
  /**
   * Fast path: one SQL round-trip returning lightweight summary rows
   * for ALL canonical properties (not just the latest-run subset).
   * Optional — adapters that can't compute it (json-file) leave it
   * undefined and PropertyService falls back to the JSONB-snapshot path.
   */
  listSummariesFast?(): Promise<PropertySummaryRow[]>;
}

/** Per-property cumulative state. Mirrors IPropertyStateStore on the Py side. */
export interface IPropertyStateStore {
  all(): Promise<PropertyStateRecord[]>;
  get(canonicalId: string): Promise<PropertyStateRecord | null>;
}

/** Per-property state-tracked units. */
export interface IUnitStore {
  listStateForProperty(canonicalId: string): Promise<UnitStateRecord[]>;
  /** Total unit count across all properties (`select count(*) from units`). */
  count(): Promise<number>;
}

/** Run registry + per-run report + ledger. */
export interface IRunStore {
  /** All known run_dates, descending (most recent first). */
  listDates(): Promise<string[]>;
  getLatestDate(): Promise<string | null>;
  getReport(runDate: string): Promise<RawRunReport | null>;
  readLedger(runDate: string): Promise<RawLedgerEntry[]>;
}

/** Run artifacts: per-property markdown, LLM JSON, profiles. */
export interface IArtifactStore {
  getPropertyReport(runDate: string, canonicalId: string): Promise<string | null>;
  getLlmReport(runDate: string): Promise<Record<string, unknown> | null>;
  getLlmPropertyDetail(
    runDate: string,
    propertyId: string,
  ): Promise<Record<string, unknown> | null>;
  getLlmDiagnostic(
    runDate: string,
    propertyId: string,
    kind: string,
  ): Promise<Record<string, unknown> | null>;
  getProfile(canonicalId: string): Promise<Record<string, unknown> | null>;
}

/** Composite data-provider facade. The API factory holds exactly one. */
export interface IDataProvider {
  /** Short identifier for logs: 'json-file' | 'postgres'. */
  readonly name: string;
  readonly properties: IPropertyStore;
  readonly propertyState: IPropertyStateStore;
  readonly units: IUnitStore;
  readonly runs: IRunStore;
  readonly artifacts: IArtifactStore;
  /** Release resources. Called on server shutdown. */
  close(): Promise<void>;
}
