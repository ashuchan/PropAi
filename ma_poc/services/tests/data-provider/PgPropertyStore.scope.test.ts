/**
 * @file PgPropertyStore.scope.test.ts
 * @description Verify the SQL produced by `listSummariesFast(scope)`:
 *   - scope='today' adds half-open range predicates on `units.last_seen_at`
 *     and joins `properties` against the same range (sargable, no ::date cast)
 *   - scope='all' omits both, recovering the legacy unfiltered behaviour
 *
 * We don't open a real pool — we capture the SQL the store hands to
 * `pool.query` and assert against the string. This catches predicate
 * regressions before they reach Cloud SQL.
 */

import { describe, expect, test, vi } from 'vitest';
import { PgPropertyStore } from '../../src/data-provider/postgres/PropertyStore.js';
import type { PgPool } from '../../src/data-provider/postgres/client.js';

function makePool(): { pool: PgPool; lastSql: () => string } {
  let capturedSql = '';
  const pool = {
    query: vi.fn(async (sql: string) => {
      capturedSql = sql;
      return { rows: [] };
    }),
    end: async () => {},
    on: () => {},
  } as unknown as PgPool;
  return { pool, lastSql: () => capturedSql };
}

describe('PgPropertyStore.listSummariesFast — SQL shape', () => {
  test('scope="today" emits LIKE on date prefix, NEVER a timestamp cast', async () => {
    const { pool, lastSql } = makePool();
    const store = new PgPropertyStore(pool);

    await store.listSummariesFast('today');

    const sql = lastSql();
    // Regression guard: comparing the varchar `last_seen_at` column
    // against a timestamp value 500s at runtime ("operator does not
    // exist: character varying >= timestamp without time zone").
    // The query MUST stay type-safe — no ::timestamp casts on the
    // run_date side of the comparison.
    expect(sql).not.toMatch(/run_date::timestamp/);
    expect(sql).not.toMatch(/interval\s*'1 day'/);
    // LIKE on the ISO-8601 date prefix is the type-safe shape.
    expect(sql).toMatch(/last_seen_at\s+like\s+\(select run_date\s*\|\|\s*'%'/);
    // Property side: inner join that drops properties not touched today,
    // also via LIKE.
    expect(sql).toMatch(/inner join latest_run lr\s+on p\.last_seen_at\s+like/);
  });

  test('scope="all" emits no last_seen_at predicate (legacy behaviour)', async () => {
    const { pool, lastSql } = makePool();
    const store = new PgPropertyStore(pool);

    await store.listSummariesFast('all');

    const sql = lastSql();
    // Outside the per-row mapping, there is no last_seen_at predicate.
    expect(sql).not.toMatch(/where\s+last_seen_at/);
    expect(sql).not.toMatch(/inner join latest_run/);
  });

  test('default scope is "today" (callers that forget the arg get the safer view)', async () => {
    const { pool, lastSql } = makePool();
    const store = new PgPropertyStore(pool);

    await store.listSummariesFast();   // no arg

    expect(lastSql()).toMatch(/inner join latest_run/);
  });
});
