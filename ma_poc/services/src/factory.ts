/**
 * @file factory.ts
 * @description Top-level factory. Builds an IDataProvider from the
 * DATA_PROVIDER env var (via data-provider/factory.ts), then composes the
 * five high-level services on top of it. All services are impl-agnostic —
 * they only touch the provider's stores.
 */

import type { IPropertyService } from './interfaces/IPropertyService.js';
import type { IUnitService } from './interfaces/IUnitService.js';
import type { IRunService } from './interfaces/IRunService.js';
import type { IDiffService } from './interfaces/IDiffService.js';
import type { IHealthService } from './interfaces/IHealthService.js';
import { PropertyService } from './services/PropertyService.js';
import { UnitService } from './services/UnitService.js';
import { RunService } from './services/RunService.js';
import { DiffService } from './services/DiffService.js';
import { HealthService } from './services/HealthService.js';
import {
  createDataProvider,
  resolveProviderConfig,
  type ProviderConfig,
  type ProviderName,
} from './data-provider/factory.js';
import type { IDataProvider } from './data-provider/contracts.js';

export interface Services {
  properties: IPropertyService;
  units: IUnitService;
  runs: IRunService;
  diff: IDiffService;
  health: IHealthService;
  /** Backing provider — expose so the host can close the pool on shutdown. */
  dataProvider: IDataProvider;
}

export interface ServiceFactoryConfig {
  /** Absolute path to the filesystem data root (needed by the json-file adapter
   *  and used as a fallback for resolving the provider kind). */
  dataDir: string;
  /** Optional override for env (defaults to process.env). */
  env?: NodeJS.ProcessEnv;
  /** Optional pre-built provider (bypass env resolution) — useful in tests. */
  provider?: IDataProvider;
}

export function createServices(cfg: ServiceFactoryConfig): Services {
  const provider =
    cfg.provider ??
    createDataProvider(resolveProviderConfig(cfg.dataDir, cfg.env ?? process.env));
  return {
    properties: new PropertyService(provider),
    units: new UnitService(provider),
    runs: new RunService(provider),
    diff: new DiffService(provider),
    health: new HealthService(provider),
    dataProvider: provider,
  };
}

export { resolveProviderConfig, createDataProvider };
export type { ProviderConfig, ProviderName };
