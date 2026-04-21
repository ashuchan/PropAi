/**
 * @file ArtifactStore.ts (json-file adapter)
 * @description Reads run artifacts from disk:
 *   - runs/{date}/property_reports/{cid}.md
 *   - runs/{date}/llm_report.json
 *   - runs/{date}/llm_report/{cid}.json
 *   - runs/{date}/llm_diagnostics/{cid}_{kind}.json
 *   - config/profiles/{cid}.json
 */

import { join } from 'node:path';
import type { IArtifactStore } from '../contracts.js';
import {
  configPath,
  getRunDir,
  readJsonFile,
  readTextFile,
} from './dataLoader.js';

export class JsonFileArtifactStore implements IArtifactStore {
  constructor(private readonly dataDir: string) {}

  async getPropertyReport(runDate: string, canonicalId: string): Promise<string | null> {
    const path = join(getRunDir(this.dataDir, runDate), 'property_reports', `${canonicalId}.md`);
    return readTextFile(path);
  }

  async getLlmReport(runDate: string): Promise<Record<string, unknown> | null> {
    const path = join(getRunDir(this.dataDir, runDate), 'llm_report.json');
    return readJsonFile<Record<string, unknown>>(path);
  }

  async getLlmPropertyDetail(
    runDate: string,
    propertyId: string,
  ): Promise<Record<string, unknown> | null> {
    const path = join(getRunDir(this.dataDir, runDate), 'llm_report', `${propertyId}.json`);
    return readJsonFile<Record<string, unknown>>(path);
  }

  async getLlmDiagnostic(
    runDate: string,
    propertyId: string,
    kind: string,
  ): Promise<Record<string, unknown> | null> {
    const path = join(
      getRunDir(this.dataDir, runDate),
      'llm_diagnostics',
      `${propertyId}_${kind}.json`,
    );
    return readJsonFile<Record<string, unknown>>(path);
  }

  async getProfile(canonicalId: string): Promise<Record<string, unknown> | null> {
    const path = configPath(this.dataDir, 'profiles', `${canonicalId}.json`);
    return readJsonFile<Record<string, unknown>>(path);
  }
}
