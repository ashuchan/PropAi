/**
 * @file postgres.client.test.ts
 * @description Unit tests for the postgres `client.ts` dispatch.
 *
 * Mirrors `ma_poc/tests/data_provider/test_cloud_sql_engine.py` — we
 * don't open a real Cloud SQL connection; we stub both `pg.Pool` and
 * `@google-cloud/cloud-sql-connector` and assert:
 *   - CLOUD_SQL_INSTANCE unset → direct TCP path, pool built from the URL
 *   - CLOUD_SQL_INSTANCE set   → connector path, IAM auth, user+db taken
 *     from DATABASE_URL and host/port ignored
 *   - CLOUD_SQL_IP_TYPE=PUBLIC is honoured
 *   - Missing user/db under Cloud SQL fails loudly (no silent empty-user
 *     connects, which is how we'd land zero rows in prod)
 *   - Provider `close()` ends both the pool and the connector
 */

import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';

// ── Stubs ────────────────────────────────────────────────────────────────────

const poolCalls: Array<{ config: Record<string, unknown> }> = [];
const poolEndCalls: number[] = [];

class FakePool {
  constructor(public readonly config: Record<string, unknown>) {
    poolCalls.push({ config });
  }
  on(_event: string, _cb: (err: Error) => void): void {}
  async end(): Promise<void> {
    poolEndCalls.push(Date.now());
  }
}

vi.mock('pg', () => ({
  default: { Pool: FakePool },
  Pool: FakePool,
}));

const connectorCalls: {
  getOptions: Array<Record<string, unknown>>;
  closes: number;
} = { getOptions: [], closes: 0 };

class FakeConnector {
  async getOptions(opts: Record<string, unknown>): Promise<Record<string, unknown>> {
    connectorCalls.getOptions.push(opts);
    return {
      // Cloud SQL Connector returns a `stream` factory the pg Pool
      // calls per-connection. We only need to prove it got forwarded.
      stream: () => ({ __fake: true }),
      ssl: false,
    };
  }
  close(): void {
    connectorCalls.closes += 1;
  }
}

vi.mock('@google-cloud/cloud-sql-connector', () => ({
  Connector: FakeConnector,
  IpAddressTypes: { PUBLIC: 'PUBLIC', PRIVATE: 'PRIVATE' },
  AuthTypes: { IAM: 'IAM', PASSWORD: 'PASSWORD' },
}));

// ── Helpers ──────────────────────────────────────────────────────────────────

beforeEach(() => {
  poolCalls.length = 0;
  poolEndCalls.length = 0;
  connectorCalls.getOptions.length = 0;
  connectorCalls.closes = 0;
});

afterEach(() => {
  vi.resetModules();
});

// ── Tests ────────────────────────────────────────────────────────────────────

describe('buildPool — direct TCP path', () => {
  test('CLOUD_SQL_INSTANCE unset → connection string passed to pg.Pool unchanged', async () => {
    const { buildPool } = await import('../../src/data-provider/postgres/client.js');
    const { pool, close } = await buildPool(
      'postgresql://app:pw@10.0.0.3:5432/jugnu',
      {} as NodeJS.ProcessEnv,
    );
    expect(pool).toBeInstanceOf(FakePool);
    expect(poolCalls).toHaveLength(1);
    expect(poolCalls[0].config).toMatchObject({
      connectionString: 'postgresql://app:pw@10.0.0.3:5432/jugnu',
    });
    expect(connectorCalls.getOptions).toHaveLength(0);
    await close();
    expect(poolEndCalls).toHaveLength(1);
    expect(connectorCalls.closes).toBe(0);
  });

  test('SQLAlchemy-style +pg8000 prefix is stripped before pg.Pool sees it', async () => {
    const { buildPool } = await import('../../src/data-provider/postgres/client.js');
    await buildPool(
      'postgresql+pg8000://postgres:pw@localhost:5432/proppy',
      {} as NodeJS.ProcessEnv,
    );
    expect(poolCalls[0].config).toMatchObject({
      connectionString: 'postgresql://postgres:pw@localhost:5432/proppy',
    });
  });
});

describe('buildPool — Cloud SQL Connector path', () => {
  test('CLOUD_SQL_INSTANCE set → connector options forwarded, IAM auth, user/db from URL', async () => {
    const { buildPool } = await import('../../src/data-provider/postgres/client.js');
    const { pool, close } = await buildPool(
      'postgresql+psycopg://jugnu-worker-production%40jugnu-494013.iam@/jugnu',
      {
        CLOUD_SQL_INSTANCE: 'jugnu-494013:us-central1:jugnu-db-production',
      } as NodeJS.ProcessEnv,
    );
    expect(pool).toBeInstanceOf(FakePool);

    // Connector got the instance name, IAM auth, and PRIVATE default.
    expect(connectorCalls.getOptions).toHaveLength(1);
    expect(connectorCalls.getOptions[0]).toMatchObject({
      instanceConnectionName: 'jugnu-494013:us-central1:jugnu-db-production',
      authType: 'IAM',
      ipType: 'PRIVATE',
    });

    // Pool got the connector stream + user/db from the URL.
    // The IAM username is percent-encoded on the wire (`%40` = `@`);
    // the client must decode it before handing to pg.Pool or Cloud SQL
    // rejects the auth handshake.
    expect(poolCalls[0].config).toMatchObject({
      user: 'jugnu-worker-production@jugnu-494013.iam',
      database: 'jugnu',
    });
    // Host/port in the URL must be ignored — the stream overrides
    // transport. pg.Pool config shouldn't carry a connectionString.
    expect(poolCalls[0].config).not.toHaveProperty('connectionString');

    await close();
    // Both pool and connector must be torn down on shutdown — leaking
    // the connector keeps a token-refresh loop alive.
    expect(poolEndCalls).toHaveLength(1);
    expect(connectorCalls.closes).toBe(1);
  });

  test('CLOUD_SQL_IP_TYPE=PUBLIC is honoured (staging smoke from outside VPC)', async () => {
    const { buildPool } = await import('../../src/data-provider/postgres/client.js');
    await buildPool(
      'postgresql://u@/d',
      {
        CLOUD_SQL_INSTANCE: 'p:us-central1:db',
        CLOUD_SQL_IP_TYPE: 'PUBLIC',
      } as NodeJS.ProcessEnv,
    );
    expect(connectorCalls.getOptions[0].ipType).toBe('PUBLIC');
  });

  test('missing user or db raises — silent empty-user connects are the zero-rows-in-prod regression', async () => {
    const { buildPool } = await import('../../src/data-provider/postgres/client.js');
    await expect(
      buildPool('postgresql://@h/', {
        CLOUD_SQL_INSTANCE: 'p:us-central1:db',
      } as NodeJS.ProcessEnv),
    ).rejects.toThrow(/DATABASE_URL must supply/);
  });
});
