/**
 * @file PropertyStore.ts (json-file adapter)
 * @description Reads per-run property payloads from data/runs/{date}/properties.json.
 */

import type { IPropertyStore, RawRunProperty } from '../contracts.js';
import { readJsonFile, runPath } from './dataLoader.js';

export class JsonFilePropertyStore implements IPropertyStore {
  constructor(private readonly dataDir: string) {}

  async listForRun(runDate: string): Promise<RawRunProperty[]> {
    const raw = await readJsonFile<RawRunProperty[]>(runPath(this.dataDir, runDate, 'properties.json'));
    return raw ?? [];
  }

  async getForRun(runDate: string, canonicalId: string): Promise<RawRunProperty | null> {
    const all = await this.listForRun(runDate);
    // Try both v1 (`Unique ID`, `Property ID`) and v2 (`apartment_id`) + nested `_meta.canonical_id`.
    return (
      all.find((p) => {
        const meta = p._meta as Record<string, unknown> | undefined;
        if (meta?.canonical_id === canonicalId) return true;
        if (p['Unique ID'] === canonicalId) return true;
        if (p['Property ID'] === canonicalId) return true;
        if (String(p.apartment_id ?? '') === canonicalId) return true;
        return false;
      }) ?? null
    );
  }
}
