/**
 * @file factory.ts
 * @description Service factory — creates service instances based on configuration.
 */

import type { IPropertyService } from './interfaces/IPropertyService.js';
import type { IUnitService } from './interfaces/IUnitService.js';
import type { IRunService } from './interfaces/IRunService.js';
import type { IDiffService } from './interfaces/IDiffService.js';
import type { IHealthService } from './interfaces/IHealthService.js';
import { JsonFilePropertyService } from './implementations/json-file/JsonFilePropertyService.js';
import { JsonFileUnitService } from './implementations/json-file/JsonFileUnitService.js';
import { JsonFileRunService } from './implementations/json-file/JsonFileRunService.js';
import { JsonFileDiffService } from './implementations/json-file/JsonFileDiffService.js';
import { JsonFileHealthService } from './implementations/json-file/JsonFileHealthService.js';

/** Available service implementations */
export type ServiceImplementation = 'json-file' | 'database';

/** Configuration for service creation */
export interface ServiceConfig {
  implementation: ServiceImplementation;
  dataDir?: string;
  connectionString?: string;
}

/** Service container with all service instances */
export interface Services {
  properties: IPropertyService;
  units: IUnitService;
  runs: IRunService;
  diff: IDiffService;
  health: IHealthService;
}

/**
 * Create service instances based on configuration.
 * @param config - Service configuration
 * @returns Service container
 * @throws Error if implementation is unknown or required config is missing
 */
export function createServices(config: ServiceConfig): Services {
  switch (config.implementation) {
    case 'json-file': {
      if (!config.dataDir) throw new Error('dataDir required for json-file implementation');
      return {
        properties: new JsonFilePropertyService(config.dataDir),
        units: new JsonFileUnitService(config.dataDir),
        runs: new JsonFileRunService(config.dataDir),
        diff: new JsonFileDiffService(config.dataDir),
        health: new JsonFileHealthService(config.dataDir),
      };
    }
    default:
      throw new Error(`Unknown service implementation: ${config.implementation}`);
  }
}
