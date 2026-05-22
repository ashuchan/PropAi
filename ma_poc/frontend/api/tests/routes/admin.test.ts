/**
 * @file admin.test.ts
 * @description Integration tests for the admin job-runner route.
 *
 * We spawn the Express app on a random localhost port and hit it with
 * Node's built-in fetch — no supertest dep required. The Python
 * subprocess is stubbed by overriding ``PYTHON_BIN=node`` so the spawn
 * exits immediately (node rejects the script's argv, but the
 * spawn / pipe / close machinery still runs end-to-end, which is what
 * we're validating).
 */

import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, test } from 'vitest';
import express from 'express';
import type { Server } from 'node:http';
import type { AddressInfo } from 'node:net';

import { createAdminRoutes } from '../../src/routes/admin.js';

let server: Server;
let baseUrl: string;

beforeAll(async () => {
  const app = express();
  app.use(express.json());
  app.use('/api/admin', createAdminRoutes());
  await new Promise<void>((resolve) => {
    server = app.listen(0, () => resolve());
  });
  const port = (server.address() as AddressInfo).port;
  baseUrl = `http://127.0.0.1:${port}`;
});

afterAll(async () => {
  await new Promise<void>((resolve) => server.close(() => resolve()));
});

const ORIG_PYTHON_BIN = process.env.PYTHON_BIN;
const ORIG_GMAIL_DELEGATED = process.env.GMAIL_DELEGATED_USER;
const ORIG_REPORT_RECIPIENTS = process.env.REPORT_RECIPIENTS;
afterEach(() => {
  if (ORIG_PYTHON_BIN === undefined) delete process.env.PYTHON_BIN;
  else process.env.PYTHON_BIN = ORIG_PYTHON_BIN;
  if (ORIG_GMAIL_DELEGATED === undefined) delete process.env.GMAIL_DELEGATED_USER;
  else process.env.GMAIL_DELEGATED_USER = ORIG_GMAIL_DELEGATED;
  if (ORIG_REPORT_RECIPIENTS === undefined) delete process.env.REPORT_RECIPIENTS;
  else process.env.REPORT_RECIPIENTS = ORIG_REPORT_RECIPIENTS;
});

/** Stub the env vars the preflight requires so a default 'happy path'
 *  spawn isn't blocked by missing creds in CI. Tests that exercise
 *  the preflight failure path override these. */
function stubPreflightPassing(): void {
  process.env.PYTHON_BIN = 'node';
  process.env.GMAIL_DELEGATED_USER = 'test@example.com';
  process.env.REPORT_RECIPIENTS = 'test@example.com';
}

/** The job registry is shared across the Express app, so a job from
 *  one test can return 409 in the next. Drain the registry before
 *  each test by polling /jobs and waiting for everything to settle. */
beforeEach(async () => {
  const list = await fetch(`${baseUrl}/api/admin/jobs?kind=email-daily-failures`);
  const data = await list.json().catch(() => ({ items: [] })) as { items: Array<{ jobId: string; status: string }> };
  for (const item of data.items) {
    if (item.status === 'running') {
      await waitFor(async () => {
        const j = await getJob(item.jobId);
        return j.body.status !== 'running';
      });
    }
  }
});

async function postEmail(body: Record<string, unknown>) {
  const r = await fetch(`${baseUrl}/api/admin/email-daily-failures`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  });
  const json = await r.json().catch(() => ({}));
  return { status: r.status, body: json as Record<string, any> };
}

async function getJob(id: string) {
  const r = await fetch(`${baseUrl}/api/admin/jobs/${id}`);
  const json = await r.json().catch(() => ({}));
  return { status: r.status, body: json as Record<string, any> };
}

async function getJobs(kind?: string) {
  const q = kind ? `?kind=${encodeURIComponent(kind)}` : '';
  const r = await fetch(`${baseUrl}/api/admin/jobs${q}`);
  const json = await r.json().catch(() => ({}));
  return { status: r.status, body: json as Record<string, any> };
}

async function waitFor(predicate: () => Promise<boolean>, timeoutMs = 5000): Promise<void> {
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    if (await predicate()) return;
    await new Promise((r) => setTimeout(r, 25));
  }
  throw new Error(`waitFor timed out after ${timeoutMs}ms`);
}

describe('POST /api/admin/email-daily-failures', () => {
  test('rejects body with malformed runDate', async () => {
    const { status, body } = await postEmail({ runDate: 'not-a-date' });
    expect(status).toBe(400);
    expect(body.error).toBe('invalid_body');
  });

  test('rejects body with non-string recipients', async () => {
    const { status } = await postEmail({ recipients: ['a@x.com'] });
    expect(status).toBe(400);
  });

  test('spawns subprocess, job transitions to terminal state', async () => {
    stubPreflightPassing();
    const { status, body } = await postEmail({ dryRun: true });
    expect([202, 409]).toContain(status);
    expect(body.jobId).toBeDefined();
    expect(body.argv?.[0]).toBe('node');

    await waitFor(async () => {
      const r = await getJob(body.jobId);
      return r.body.status !== 'running';
    });
    const final = await getJob(body.jobId);
    expect(['succeeded', 'failed']).toContain(final.body.status);
    expect(final.body.finishedAt).toBeDefined();
    expect(typeof final.body.exitCode).toBe('number');
  }, 10_000);

  test('409 when a job of the same kind is already running', async () => {
    stubPreflightPassing();
    const first = await postEmail({});
    expect(first.status).toBe(202);

    // Immediate second POST. Race: if the stub exited too fast,
    // accept 202 (still correct behaviour); otherwise assert 409.
    const second = await postEmail({});
    if (second.status === 409) {
      expect(second.body.jobId).toBe(first.body.jobId);
    } else {
      expect(second.status).toBe(202);
    }

    // Drain so the next test doesn't see leftover running jobs.
    await waitFor(async () => {
      const r = await getJob(first.body.jobId);
      return r.body.status !== 'running';
    });
  }, 10_000);
});

describe('GET /api/admin/jobs/:jobId', () => {
  test('404 for unknown jobId', async () => {
    const { status, body } = await getJob('does-not-exist');
    expect(status).toBe(404);
    expect(body.error).toBe('job_not_found');
  });

  test('exposes stdoutTail / stderrTail / argv on a finished job', async () => {
    stubPreflightPassing();
    const { status, body } = await postEmail({ dryRun: true });
    expect(status).toBe(202);

    await waitFor(async () => (await getJob(body.jobId)).body.status !== 'running');
    const final = await getJob(body.jobId);
    expect(Array.isArray(final.body.stdoutTail)).toBe(true);
    expect(Array.isArray(final.body.stderrTail)).toBe(true);
    expect(Array.isArray(final.body.argv)).toBe(true);
    expect(final.body.argv).toContain('-m');
  }, 10_000);
});

describe('GET /api/admin/jobs (list)', () => {
  test('filters by kind', async () => {
    stubPreflightPassing();
    await postEmail({});
    const r = await getJobs('email-daily-failures');
    expect(r.body.items.length).toBeGreaterThan(0);
    expect(r.body.items[0].kind).toBe('email-daily-failures');

    const other = await getJobs('nonexistent-kind');
    expect(other.body.items).toEqual([]);
  });
});

/* ────────────────────────────────────────────────────────────────────
 * Preflight + error mapping
 * ──────────────────────────────────────────────────────────────── */

describe('GET /api/admin/email-daily-failures/preflight', () => {
  test('returns shape with python + recipients + checks', async () => {
    stubPreflightPassing();  // 'node' is always present on test machine.
    const r = await fetch(`${baseUrl}/api/admin/email-daily-failures/preflight`);
    expect(r.status).toBe(200);
    const body = await r.json() as any;
    expect(typeof body.ok).toBe('boolean');
    expect(Array.isArray(body.checks)).toBe(true);
    expect(body.python.bin).toBe('node');
    expect(body.python.found).toBe(true);   // node --version always works
    expect(typeof body.python.version).toBe('string');
    expect(body.scriptModule).toBe('ma_poc.scripts.email.daily_failures');
    expect(Array.isArray(body.recipients)).toBe(true);
  });

  test('python_interpreter check fails when PYTHON_BIN missing', async () => {
    process.env.PYTHON_BIN = 'definitely-not-a-real-binary-xyzqq';
    const r = await fetch(`${baseUrl}/api/admin/email-daily-failures/preflight`);
    const body = await r.json() as any;
    expect(body.ok).toBe(false);
    const pyCheck = body.checks.find((c: any) => c.name === 'python_interpreter');
    expect(pyCheck.ok).toBe(false);
    expect(pyCheck.detail).toContain('not found');
  });
});

describe('POST /api/admin/email-daily-failures preflight gating', () => {
  test('returns 412 with failures when PYTHON_BIN missing', async () => {
    process.env.PYTHON_BIN = 'definitely-not-a-real-binary-xyzqq';
    const { status, body } = await postEmail({});
    expect(status).toBe(412);
    expect(body.error).toBe('preflight_failed');
    expect(Array.isArray(body.failures)).toBe(true);
    expect(body.failures.some((f: any) => f.name === 'python_interpreter')).toBe(true);
  });

  test('dryRun bypasses preflight default-recipients failure', async () => {
    stubPreflightPassing();
    const origRecipients = process.env.REPORT_RECIPIENTS;
    delete process.env.REPORT_RECIPIENTS;
    try {
      const { status } = await postEmail({ dryRun: true });
      // dryRun skips ALL preflight checks → spawn allowed even with no recipients.
      expect(status).toBe(202);
    } finally {
      if (origRecipients !== undefined) process.env.REPORT_RECIPIENTS = origRecipients;
    }
  });

  test('explicit recipients body field skips default-recipients gate', async () => {
    stubPreflightPassing();
    const origRecipients = process.env.REPORT_RECIPIENTS;
    delete process.env.REPORT_RECIPIENTS;
    try {
      const { status } = await postEmail({ recipients: 'test@example.com' });
      expect(status).toBe(202);
    } finally {
      if (origRecipients !== undefined) process.env.REPORT_RECIPIENTS = origRecipients;
    }
  });
});

describe('spawn error mapping', () => {
  test('ENOENT for missing interpreter → friendly error message', async () => {
    // Use dryRun to skip preflight (which would 412 first), forcing
    // the spawn to actually attempt + fail with ENOENT.
    process.env.PYTHON_BIN = 'definitely-not-a-real-binary-xyzqq';
    const { status, body } = await postEmail({ dryRun: true });
    expect(status).toBe(202);
    await waitFor(async () => (await getJob(body.jobId)).body.status !== 'running');
    const final = await getJob(body.jobId);
    expect(final.body.status).toBe('failed');
    expect(final.body.error).toContain('Python interpreter');
    expect(final.body.error).toContain('PYTHON_BIN');
  }, 10_000);
});
