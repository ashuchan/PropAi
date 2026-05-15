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
  test('scope="today" emits half-open range, no ::date cast', async () => {
    const { pool, lastSql } = makePool();
    const store = new PgPropertyStore(pool);

    await store.listSummariesFast('today');

    const sql = lastSql();
    // The whole point of the range predicate: never cast the indexed
    // column with `::date`. If a future edit reintroduces it, this fails.
    expect(sql).not.toMatch(/last_seen_at::date/);
    // Unit side: half-open range on units.last_seen_at.
    expect(sql).toMatch(/last_seen_at\s*>=\s*\(select run_date::timestamp/);
    expect(sql).toMatch(/last_seen_at\s*<\s*\(select\s*\(run_date\s*\+\s*interval\s*'1 day'\)/);
    // Property side: inner join that drops properties not touched today.
    expect(sql).toMatch(/inner join latest_run lr\s+on p\.last_seen_at\s*>=/);
  });

  test('scope="all" emits no last_seen_at predicate (legacy behaviour)', async () => {
    const { pool, lastSql } = makePool();
    const store = new PgPropertyStore(pool);

    await store.listSummariesFast('all');

    const sql = lastSql();
    // Outside the per-row mapping, there is no last_seen_at predicate.
    // (We allow the column to appear in the SELECT list; we don't allow
    // it in a WHERE/JOIN that filters rows.)
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
