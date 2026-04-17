/**
 * @file dataLoader.ts
 * @description Centralized file I/O with caching for JSON data files.
 * Supports schema-versioned directory layout (data/{v1|v2}/runs/, data/{v1|v2}/state/)
 * with automatic fallback to legacy flat layout (data/runs/, data/state/).
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

/**
 * Get cached data or load from file.
 * @param key - Cache key
 * @param loader - Function to load data
 * @param ttl - Cache TTL in ms
 * @returns Cached or freshly loaded data
 */
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

/**
 * Read and parse a JSON file.
 * @param filePath - Absolute path to JSON file
 * @returns Parsed JSON data or null if file not found
 */
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

/**
 * Read a plain text file (e.g., markdown reports).
 * @param filePath - Absolute path to file
 * @returns File contents or null if not found
 */
export async function readTextFile(filePath: string): Promise<string | null> {
  return cached<string | null>(`text:${filePath}`, async () => {
    try {
      return await readFile(filePath, 'utf-8');
    } catch (err) {
      const error = err as NodeJS.ErrnoException;
      if (error.code === 'ENOENT') {
        logger.warn({ file: filePath }, 'text file not found');
        return null;
      }
      logger.error({ file: filePath, error: error.message }, 'failed to read text');
      return null;
    }
  });
}

/**
 * Read and parse a JSONL file (one JSON object per line).
 * @param filePath - Absolute path to JSONL file
 * @returns Array of parsed objects
 */
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
      if (error.code === 'ENOENT') {
        logger.warn({ file: filePath }, 'JSONL file not found');
        return [];
      }
      logger.error({ file: filePath, error: error.message }, 'failed to read JSONL');
      return [];
    }
  });
}

/**
 * Resolve the effective data root for the current schema version.
 * Checks data/{version}/runs/ first — if it exists, uses the versioned layout.
 * Otherwise falls back to the legacy flat layout (data/runs/).
 * Result is cached per dataDir so the check runs only once per process.
 */
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
    // No versioned directory yet — use legacy flat layout
    root = dataDir;
    logger.info({ root, version, checked: versionedRunsDir }, 'versioned data dir not found, using legacy flat layout');
  }

  resolvedRoots.set(dataDir, root);
  return root;
}

/**
 * Get sorted list of available run dates.
 * @param dataDir - Base data directory
 * @returns Array of date strings sorted descending
 */
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
          if (stats.isDirectory()) {
            dateDirs.push(entry);
          }
        }
      }
      return dateDirs.sort().reverse();
    } catch {
      logger.warn({ dir: runsDir }, 'runs directory not found');
      return [];
    }
  }, 30_000);
}

/**
 * Get the latest run date.
 * @param dataDir - Base data directory
 * @returns Latest date string or null
 */
export async function getLatestRunDate(dataDir: string): Promise<string | null> {
  const dates = await getRunDates(dataDir);
  return dates[0] ?? null;
}

/**
 * Build path to a run file.
 * @param dataDir - Base data directory
 * @param date - Run date
 * @param filename - File name within the run directory
 * @returns Absolute file path
 */
export function runPath(dataDir: string, date: string, filename: string): string {
  const root = resolveDataRoot(dataDir);
  return join(root, 'runs', date, filename);
}

/**
 * Build path to a state file.
 * @param dataDir - Base data directory
 * @param filename - File name within the state directory
 * @returns Absolute file path
 */
export function statePath(dataDir: string, filename: string): string {
  const root = resolveDataRoot(dataDir);
  return join(root, 'state', filename);
}

/** Clear all cached data and resolved roots. Useful for testing. */
export function clearCache(): void {
  cache.clear();
  resolvedRoots.clear();
}
