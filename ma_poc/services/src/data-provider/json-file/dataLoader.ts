/**
 * @file dataLoader.ts
 * @description Centralized file I/O with 60s cache for JSON / text / JSONL.
 *
 * Handles SCHEMA_VERSION-aware data root resolution: tries
 * `data/{version}/runs/` first, falls back to the flat `data/runs/` layout.
 *
 * This is the json-file adapter's shared I/O layer — stores built on top
 * (`PropertyStore`, `RunStore`, …) call these helpers rather than touching
 * the filesystem directly.
 */

import { readFile, readdir, stat } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { join } from 'node:path';
import { logger } from '../../logger.js';

interface CacheEntry<T> {
  data: T;
  timestamp: number;
  ttl: number;
}

const cache = new Map<string, CacheEntry<unknown>>();
const DEFAULT_TTL_MS = 60_000;

async function cached<T>(key: string, loader: () => Promise<T>, ttl: number = DEFAULT_TTL_MS): Promise<T> {
  const existing = cache.get(key) as CacheEntry<T> | undefined;
  if (existing && Date.now() - existing.timestamp < existing.ttl) {
    logger.debug({ key }, 'cache hit');
    return existing.data;
  }
  const start = Date.now();
  const data = await loader();
  cache.set(key, { data, timestamp: Date.now(), ttl });
  logger.info({ key, duration_ms: Date.now() - start, cached: false }, 'loaded data');
  return data;
}

export async function readJsonFile<T>(filePath: string): Promise<T | null> {
  return cached<T | null>(`json:${filePath}`, async () => {
    try {
      const content = await readFile(filePath, 'utf-8');
      return JSON.parse(content) as T;
    } catch (err) {
      const error = err as NodeJS.ErrnoException;
      if (error.code === 'ENOENT') {
        logger.warn({ file: filePath }, 'file not found');
        return null;
      }
      logger.error({ file: filePath, error: error.message }, 'failed to read JSON');
      return null;
    }
  });
}

export async function readTextFile(filePath: string): Promise<string | null> {
  return cached<string | null>(`text:${filePath}`, async () => {
    try {
      return await readFile(filePath, 'utf-8');
    } catch (err) {
      const error = err as NodeJS.ErrnoException;
      if (error.code === 'ENOENT') return null;
      logger.error({ file: filePath, error: error.message }, 'failed to read text');
      return null;
    }
  });
}

export async function readJsonlFile<T>(filePath: string): Promise<T[]> {
  return cached<T[]>(`jsonl:${filePath}`, async () => {
    try {
      const content = await readFile(filePath, 'utf-8');
      const lines = content.trim().split('\n').filter(Boolean);
      const results: T[] = [];
      for (const line of lines) {
        try {
          results.push(JSON.parse(line) as T);
        } catch {
          logger.warn({ file: filePath, line: line.substring(0, 100) }, 'skipped malformed JSONL line');
        }
      }
      return results;
    } catch (err) {
      const error = err as NodeJS.ErrnoException;
      if (error.code === 'ENOENT') return [];
      logger.error({ file: filePath, error: error.message }, 'failed to read JSONL');
      return [];
    }
  });
}

/** SCHEMA_VERSION-aware data root. `data/{v1|v2}/runs/` if it exists, else flat layout. */
const resolvedRoots = new Map<string, string>();

function resolveDataRoot(dataDir: string): string {
  const existing = resolvedRoots.get(dataDir);
  if (existing) return existing;

  const version = process.env.SCHEMA_VERSION || 'v1';
  const versioned = join(dataDir, version);
  const versionedRunsDir = join(versioned, 'runs');

  let root: string;
  if (existsSync(versionedRunsDir)) {
    root = versioned;
    logger.info({ root, version }, 'using schema-versioned data directory');
  } else {
    root = dataDir;
    logger.info({ root, version, checked: versionedRunsDir }, 'versioned data dir not found, using legacy flat layout');
  }

  resolvedRoots.set(dataDir, root);
  return root;
}

export async function getRunDates(dataDir: string): Promise<string[]> {
  const root = resolveDataRoot(dataDir);
  return cached<string[]>(`runs:${root}`, async () => {
    const runsDir = join(root, 'runs');
    try {
      const entries = await readdir(runsDir);
      const dateDirs: string[] = [];
      for (const entry of entries) {
        if (/^\d{4}-\d{2}-\d{2}$/.test(entry)) {
          const stats = await stat(join(runsDir, entry));
          if (stats.isDirectory()) dateDirs.push(entry);
        }
      }
      return dateDirs.sort().reverse();
    } catch {
      logger.warn({ dir: runsDir }, 'runs directory not found');
      return [];
    }
  }, 30_000);
}

export async function getLatestRunDate(dataDir: string): Promise<string | null> {
  const dates = await getRunDates(dataDir);
  return dates[0] ?? null;
}

export function getRunDir(dataDir: string, date: string): string {
  return join(resolveDataRoot(dataDir), 'runs', date);
}

export function runPath(dataDir: string, date: string, filename: string): string {
  return join(resolveDataRoot(dataDir), 'runs', date, filename);
}

/** State files live under the non-versioned data root (shared across schemas). */
export function statePath(dataDir: string, filename: string): string {
  return join(dataDir, 'state', filename);
}

/** Config directory (profiles live here), sibling of the data dir. */
export function configPath(dataDir: string, ...segments: string[]): string {
  // data dir is typically `{ma_poc}/data`; config is `{ma_poc}/config`.
  const maPocRoot = join(dataDir, '..');
  return join(maPocRoot, 'config', ...segments);
}

export function clearCache(): void {
  cache.clear();
  resolvedRoots.clear();
}
