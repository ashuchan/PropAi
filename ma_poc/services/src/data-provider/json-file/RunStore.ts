/**
 * @file RunStore.ts (json-file adapter)
 * @description Run registry + per-run report + ledger from data/runs/{date}/.
 */

import { readdir } from 'node:fs/promises';
import type {
  IRunStore,
  RawLedgerEntry,
  RawRunReport,
} from '../contracts.js';
import {
  getLatestRunDate,
  getRunDates,
  getRunDir,
  readJsonFile,
  readJsonlFile,
  runPath,
} from './dataLoader.js';

export class JsonFileRunStore implements IRunStore {
  constructor(private readonly dataDir: string) {}

  async listDates(): Promise<string[]> {
    return getRunDates(this.dataDir);
  }

  async getLatestDate(): Promise<string | null> {
    return getLatestRunDate(this.dataDir);
  }

  async getReport(runDate: string): Promise<RawRunReport | null> {
    // Some pipelines write `report.json`, some write `report_{date}.json`.
    // Prefer the canonical name; fall back to any `report*.json`.
    const direct = await readJsonFile<RawRunReport>(runPath(this.dataDir, runDate, 'report.json'));
    if (direct) return direct;
    try {
      const entries = await readdir(getRunDir(this.dataDir, runDate));
      for (const f of entries) {
        if (f.startsWith('report') && f.endsWith('.json')) {
          const loaded = await readJsonFile<RawRunReport>(
            runPath(this.dataDir, runDate, f),
          );
          if (loaded) return loaded;
        }
      }
    } catch {
      // dir missing — fall through
    }
    return null;
  }

  async readLedger(runDate: string): Promise<RawLedgerEntry[]> {
    return readJsonlFile<RawLedgerEntry>(runPath(this.dataDir, runDate, 'ledger.jsonl'));
  }
}
