/**
 * @file PropertyStateStore.ts (json-file adapter)
 * @description Reads data/state/property_index.json. The on-disk file is
 * V1-shaped (`name`, `zip`), so this adapter populates the V1 aliases; the
 * Postgres adapter populates the V2 aliases from v2-named columns.
 */

import type { IPropertyStateStore, PropertyStateRecord } from '../contracts.js';
import { readJsonFile, statePath } from './dataLoader.js';

interface RawEntry {
  canonical_id?: string;
  name?: string | null;
  address?: string | null;
  city?: string | null;
  state?: string | null;
  zip?: string | null;
  website?: string | null;
  last_scrape_status?: string | null;
  last_units_count?: number | null;
  last_seen_at?: string | null;
  first_seen_date?: string | null;
  [key: string]: unknown;
}

type RawIndex = Record<string, RawEntry>;

export class JsonFilePropertyStateStore implements IPropertyStateStore {
  constructor(private readonly dataDir: string) {}

  private async loadIndex(): Promise<RawIndex> {
    return (await readJsonFile<RawIndex>(statePath(this.dataDir, 'property_index.json'))) ?? {};
  }

  async all(): Promise<PropertyStateRecord[]> {
    const index = await this.loadIndex();
    return Object.entries(index).map(([id, raw]) => this.toRecord(id, raw));
  }

  async get(canonicalId: string): Promise<PropertyStateRecord | null> {
    const index = await this.loadIndex();
    const raw = index[canonicalId];
    return raw ? this.toRecord(canonicalId, raw) : null;
  }

  private toRecord(canonicalId: string, raw: RawEntry): PropertyStateRecord {
    const {
      name, address, city, state, zip, website,
      last_scrape_status, last_units_count,
      last_seen_at, first_seen_date,
      canonical_id: _cid,
      // Legacy JSON may still carry last_seen_date; discard — use last_seen_at.
      last_seen_date: _legacyLSD,
      ...rest
    } = raw;
    return {
      canonicalId,
      name: name ?? null,
      address: address ?? null,
      city: city ?? null,
      state: state ?? null,
      zip: zip ?? null,
      website: website ?? null,
      lastScrapeStatus: last_scrape_status ?? null,
      lastUnitsCount: last_units_count ?? null,
      lastSeenAt: last_seen_at ?? null,
      firstSeenDate: first_seen_date ?? null,
      extra: rest,
    };
  }
}
