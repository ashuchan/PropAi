/**
 * @file provider.ts (json-file adapter)
 * @description Composite DataProvider that bundles the five json-file stores.
 */

import type { IDataProvider } from '../contracts.js';
import { JsonFileArtifactStore } from './ArtifactStore.js';
import { JsonFilePropertyStateStore } from './PropertyStateStore.js';
import { JsonFilePropertyStore } from './PropertyStore.js';
import { JsonFileRunStore } from './RunStore.js';
import { JsonFileUnitStore } from './UnitStore.js';

export class JsonFileDataProvider implements IDataProvider {
  readonly name = 'json-file';
  readonly properties: JsonFilePropertyStore;
  readonly propertyState: JsonFilePropertyStateStore;
  readonly units: JsonFileUnitStore;
  readonly runs: JsonFileRunStore;
  readonly artifacts: JsonFileArtifactStore;

  constructor(dataDir: string) {
    this.properties = new JsonFilePropertyStore(dataDir);
    this.propertyState = new JsonFilePropertyStateStore(dataDir);
    this.units = new JsonFileUnitStore(dataDir);
    this.runs = new JsonFileRunStore(dataDir);
    this.artifacts = new JsonFileArtifactStore(dataDir);
  }

  async close(): Promise<void> {
    // No pooled resources; cache is process-global and self-expires.
  }
}
