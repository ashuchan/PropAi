/**
 * @file scope.test.ts
 * @description Zod-side coverage for the scope query param:
 *   - missing scope → defaulted to 'today'
 *   - invalid scope → request rejected with 400 + descriptive error
 *   - 'today' / 'all' accepted verbatim
 *
 * We don't spin Express up; we drive the schemas directly. The
 * `validateQuery` middleware is a thin wrapper around `safeParse`, so
 * schema-level guarantees are what matters.
 */

import { describe, expect, test } from 'vitest';
import {
  scopeSchema,
  propertyQuerySchema,
  searchSchema,
  rankedSchema,
  idQuerySchema,
} from '../../src/middleware/validation.js';

describe('scopeSchema', () => {
  test('omitted → defaults to "today"', () => {
    expect(scopeSchema.parse(undefined)).toBe('today');
  });

  test('accepts "today" and "all"', () => {
    expect(scopeSchema.parse('today')).toBe('today');
    expect(scopeSchema.parse('all')).toBe('all');
  });

  test('rejects unknown values', () => {
    const r = scopeSchema.safeParse('yesterday');
    expect(r.success).toBe(false);
  });
});

describe('schemas embed scope correctly', () => {
  test('propertyQuerySchema defaults scope to "today" when absent', () => {
    const r = propertyQuerySchema.parse({});
    expect(r.scope).toBe('today');
  });

  test('searchSchema requires q and defaults scope', () => {
    const r = searchSchema.parse({ q: 'cambridge' });
    expect(r.scope).toBe('today');
    expect(r.limit).toBe(20);
  });

  test('rankedSchema honours an explicit scope=all', () => {
    const r = rankedSchema.parse({ metric: 'avgAskingRent', scope: 'all' });
    expect(r.scope).toBe('all');
  });

  test('idQuerySchema passes through unknown query params (eg. cache busters)', () => {
    // .passthrough() keeps unrelated params so existing callers that
    // append `?_=12345` etc don't 400.
    const r = idQuerySchema.parse({ scope: 'all', _: '999' });
    expect(r.scope).toBe('all');
    expect((r as Record<string, unknown>)._).toBe('999');
  });

  test('propertyQuerySchema rejects bad scope with structured error', () => {
    const r = propertyQuerySchema.safeParse({ scope: 'forever' });
    expect(r.success).toBe(false);
    if (!r.success) {
      const scopeIssue = r.error.issues.find((i) => i.path.includes('scope'));
      expect(scopeIssue).toBeDefined();
    }
  });
});
