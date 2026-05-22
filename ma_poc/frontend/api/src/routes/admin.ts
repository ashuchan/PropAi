/**
 * @file admin.ts
 * @description Admin endpoints — operator-triggered scripts that don't
 * fit the read-only services contracts.
 *
 * Currently exposes:
 *
 *   POST /api/admin/email-daily-failures
 *     Body: { runDate?: string, recipients?: string,
 *             useGcs?: boolean, localDir?: string, dryRun?: boolean }
 *     Spawns `scripts.email.daily_failures` as a Python subprocess and
 *     returns ``{ jobId, status: "running", startedAt }`` immediately.
 *     The subprocess takes ~3 min for a 5K-property run — too long for
 *     a sync HTTP response — so the call is fire-and-forget and the UI
 *     polls the job endpoint below.
 *
 *   GET /api/admin/jobs/:jobId
 *     Returns ``{ jobId, status: "running"|"succeeded"|"failed",
 *                 startedAt, finishedAt?, exitCode?, stdoutTail,
 *                 stderrTail, error? }``.
 *
 * Design notes
 * ------------
 *
 * The job registry is **in-memory**, owned by this Express process.
 * Process restart → all in-flight jobs forgotten (the subprocess
 * keeps running until completion; just no way to query status). This
 * is intentional for a single-instance internal tool — adding a
 * persistent queue (DB, Redis) would be over-engineering for a
 * once-a-day email button.
 *
 * Concurrency: only ONE job at a time is allowed for the
 * ``email-daily-failures`` kind. A second POST while a job is running
 * returns 409 Conflict with the running ``jobId`` so the UI can latch
 * onto it instead of double-spawning the script.
 *
 * Security: NO auth. This is an internal-tool API — gate it at the
 * deployment / VPC layer, not here. The script runs whatever Python
 * interpreter is in PATH; do not expose this server to the public
 * internet without an auth layer.
 */

import { spawn, spawnSync, type ChildProcess } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { resolve } from 'node:path';
import { existsSync } from 'node:fs';
import { Router } from 'express';
import { z } from 'zod';

type JobStatus = 'running' | 'succeeded' | 'failed';

interface JobRecord {
  jobId: string;
  kind: 'email-daily-failures';
  status: JobStatus;
  startedAt: string;
  finishedAt?: string;
  exitCode?: number | null;
  stdoutTail: string[];
  stderrTail: string[];
  error?: string;
  /** Reference to the live ChildProcess. Cleared on completion so the
   *  GC can collect; the registry then holds just the result. */
  child?: ChildProcess;
  /** Command argv that was actually spawned — surfaced in the GET
   *  response so the operator can see exactly what ran. */
  argv: string[];
}

/** Last N stdout/stderr lines retained per job. Bounded so a chatty
 *  script can't blow the API server's memory. */
const LOG_TAIL_LIMIT = 200;

/** Maximum age of finished jobs before garbage collection. Keeps the
 *  registry from growing without bound across a long-lived server. */
const FINISHED_JOB_TTL_MS = 60 * 60 * 1000; // 1 hour

/** Resolve the repo root from this file's path. The API server runs
 *  from ``ma_poc/frontend/api/dist`` (build) or ``...src`` (dev), so
 *  walk up enough levels to land at ``ma_poc/``. */
function resolveMaPocRoot(): string {
  // import.meta.url is the running file. Walk up to find /ma_poc/.
  const here = new URL(import.meta.url).pathname;
  // On Windows the URL path is ``/C:/...``; trim the leading slash.
  const cleaned = process.platform === 'win32' && here.startsWith('/')
    ? here.slice(1)
    : here;
  const marker = '/ma_poc/';
  const idx = cleaned.toLowerCase().lastIndexOf(marker);
  if (idx < 0) {
    // Fallback — assume CWD is the repo root.
    return resolve(process.cwd());
  }
  return cleaned.slice(0, idx + marker.length - 1);
}

function pushBounded(arr: string[], line: string): void {
  arr.push(line);
  if (arr.length > LOG_TAIL_LIMIT) arr.shift();
}

/* ──────────────────────────────────────────────────────────────────
 * Preflight — answers "if I click Send now, will it work?"
 *
 * The subprocess approach has three independent things that can go
 * wrong silently in a deploy:
 *
 *   1. ``PYTHON_BIN`` interpreter not installed / not on PATH
 *   2. ``GMAIL_DELEGATED_USER`` not set (gmail_api transport requires it)
 *   3. ``REPORT_RECIPIENTS`` empty AND no override passed
 *
 * The preflight endpoint surfaces each of these as a distinct check
 * so the UI can render a precise pre-click status. We keep the
 * checks cheap — spawnSync for the Python version probe (a few ms)
 * and env-var presence checks (a microsecond each). No imports are
 * attempted from the Python module itself; that's deferred to the
 * actual send so we don't double-pay the import cost.
 * ─────────────────────────────────────────────────────────────── */

interface PreflightCheck {
  name: string;
  ok: boolean;
  detail: string;
}

interface PreflightResult {
  ok: boolean;
  checks: PreflightCheck[];
  /** Convenience — denormalised values the UI surfaces above the
   *  Send button so the operator sees them at a glance. */
  python: { bin: string; found: boolean; version: string | null };
  emailTransport: string;
  delegatedUser: string | null;
  recipients: string[];
  scriptModule: string;
}

function probePython(bin: string): { found: boolean; version: string | null } {
  // spawnSync with a short timeout — the script's --version is
  // sub-second, but we cap at 3s to keep a hung interpreter from
  // wedging the preflight endpoint.
  try {
    const r = spawnSync(bin, ['--version'], {
      timeout: 3000,
      windowsHide: true,
      encoding: 'utf-8',
    });
    if (r.error || r.status !== 0) {
      return { found: false, version: null };
    }
    // Python prints "Python 3.X.Y" on stdout (>=3.4) or stderr (<3.4).
    // We try both and pick the first non-empty line.
    const out = (r.stdout || '').trim() || (r.stderr || '').trim();
    return { found: true, version: out || null };
  } catch {
    return { found: false, version: null };
  }
}

function parseRecipients(raw: string | undefined | null): string[] {
  if (!raw) return [];
  return raw
    .split(',')
    .map((s) => s.trim())
    .filter((s) => s.length > 0);
}

function runPreflight(): PreflightResult {
  const checks: PreflightCheck[] = [];
  const bin = process.env.PYTHON_BIN || 'python';
  const python = probePython(bin);
  checks.push({
    name: 'python_interpreter',
    ok: python.found,
    detail: python.found
      ? `${bin} → ${python.version ?? 'unknown version'}`
      : `${bin} not found on PATH. Set PYTHON_BIN to the venv interpreter.`,
  });

  const transport = (process.env.EMAIL_TRANSPORT || 'gmail_api').toLowerCase();
  checks.push({
    name: 'email_transport',
    ok: transport === 'gmail_api' || transport === 'mcp',
    detail: `EMAIL_TRANSPORT=${transport}`,
  });

  const delegatedUser = process.env.GMAIL_DELEGATED_USER || null;
  if (transport === 'gmail_api') {
    checks.push({
      name: 'gmail_delegated_user',
      ok: !!delegatedUser,
      detail: delegatedUser
        ? `GMAIL_DELEGATED_USER=${delegatedUser}`
        : 'GMAIL_DELEGATED_USER not set (required for gmail_api transport).',
    });
  } else {
    checks.push({
      name: 'gmail_mcp_command',
      ok: !!process.env.GMAIL_MCP_COMMAND || !!process.env.GMAIL_MCP_ARGS,
      detail: process.env.GMAIL_MCP_COMMAND
        ? `GMAIL_MCP_COMMAND=${process.env.GMAIL_MCP_COMMAND}`
        : 'GMAIL_MCP_COMMAND / GMAIL_MCP_ARGS not set; using defaults from script.',
    });
  }

  const recipients = parseRecipients(process.env.REPORT_RECIPIENTS);
  checks.push({
    name: 'default_recipients',
    ok: recipients.length > 0,
    detail: recipients.length > 0
      ? `REPORT_RECIPIENTS has ${recipients.length} address(es)`
      : 'REPORT_RECIPIENTS empty — caller must pass recipients explicitly.',
  });

  // Source-tree presence check — verify ma_poc/scripts/email/daily_failures.py
  // exists. Catches deploys where the Python source wasn't copied into the
  // image alongside the Node API server.
  const root = resolveMaPocRoot();
  const scriptPath = resolve(root, 'scripts', 'email', 'daily_failures.py');
  const scriptExists = existsSync(scriptPath);
  checks.push({
    name: 'script_present',
    ok: scriptExists,
    detail: scriptExists
      ? `Found at ${scriptPath}`
      : `Missing at ${scriptPath}. Source tree may not be on this host.`,
  });

  return {
    ok: checks.every((c) => c.ok),
    checks,
    python: { bin, found: python.found, version: python.version },
    emailTransport: transport,
    delegatedUser,
    recipients,
    scriptModule: 'ma_poc.scripts.email.daily_failures',
  };
}

/**
 * Create the admin route group.
 *
 * Pure factory — owns its own job registry so multiple instances
 * (tests, multiple Express apps in one process) stay isolated. The
 * route handlers close over the registry.
 */
export function createAdminRoutes(): Router {
  const router = Router();

  // jobId → record. One Map for both running + recently-finished jobs.
  const jobs = new Map<string, JobRecord>();

  // Trim finished jobs older than FINISHED_JOB_TTL_MS on every request.
  // Cheap because the map is bounded by single-digit jobs/day in
  // practice; avoids a setInterval that would keep the test process
  // alive after vitest finishes.
  const gc = () => {
    const cutoff = Date.now() - FINISHED_JOB_TTL_MS;
    for (const [id, j] of jobs) {
      if (j.status === 'running') continue;
      const t = j.finishedAt ? new Date(j.finishedAt).getTime() : 0;
      if (t < cutoff) jobs.delete(id);
    }
  };

  /** Find the currently-running job of the given kind, if any. */
  const findRunning = (kind: JobRecord['kind']): JobRecord | undefined => {
    for (const j of jobs.values()) {
      if (j.kind === kind && j.status === 'running') return j;
    }
    return undefined;
  };

  const triggerSchema = z.object({
    runDate: z.string().regex(/^\d{4}-\d{2}-\d{2}$/).optional(),
    recipients: z.string().optional(),
    localDir: z.string().optional(),
    useGcs: z.boolean().optional(),
    dryRun: z.boolean().optional(),
  });

  // Pre-flight inspection. The UI calls this on mount + after every
  // job-failure event so the operator always sees the current state
  // of the email-send environment without having to click and wait.
  router.get('/email-daily-failures/preflight', (_req, res) => {
    res.json(runPreflight());
  });

  router.post('/email-daily-failures', (req, res, next) => {
    try {
      gc();

      const parsed = triggerSchema.safeParse(req.body ?? {});
      if (!parsed.success) {
        res.status(400).json({
          error: 'invalid_body',
          details: parsed.error.issues.map((i) => `${i.path.join('.')}: ${i.message}`).join('; '),
        });
        return;
      }
      const body = parsed.data;

      // Single-job concurrency gate — return the running job's id so
      // the UI can latch onto it.
      const inflight = findRunning('email-daily-failures');
      if (inflight) {
        res.status(409).json({
          error: 'job_already_running',
          jobId: inflight.jobId,
          startedAt: inflight.startedAt,
        });
        return;
      }

      // Preflight check — surface common misconfigurations BEFORE we
      // spawn a doomed subprocess. Skipped when the caller passes
      // ``recipients`` explicitly (since the default-recipients check
      // is the only thing the body can override). Also skipped on
      // ``dryRun`` since dry-run smokes shouldn't be blocked by
      // missing recipients.
      const preflight = runPreflight();
      const skippableForBody = body.recipients ? new Set(['default_recipients']) : new Set<string>();
      const blockingFailures = preflight.checks.filter(
        (c) => !c.ok && !skippableForBody.has(c.name) && !body.dryRun,
      );
      if (blockingFailures.length > 0) {
        res.status(412).json({
          error: 'preflight_failed',
          failures: blockingFailures,
          preflight,
        });
        return;
      }

      // Build argv. The script is invoked as a module via -m so its
      // package-relative imports resolve. Python interpreter comes
      // from PYTHON_BIN env (defaults to ``python``); operators can
      // pin the venv interpreter when needed.
      const repoRoot = resolveMaPocRoot();
      const python = process.env.PYTHON_BIN || 'python';
      const argv = [
        '-m', 'ma_poc.scripts.email.daily_failures',
      ];
      if (body.runDate) argv.push('--run-date', body.runDate);
      if (body.recipients) argv.push('--recipients', body.recipients);
      if (body.localDir) argv.push('--local-dir', body.localDir);
      if (body.useGcs) argv.push('--use-gcs');
      if (body.dryRun) argv.push('--dry-run');

      const jobId = randomUUID();
      const record: JobRecord = {
        jobId,
        kind: 'email-daily-failures',
        status: 'running',
        startedAt: new Date().toISOString(),
        stdoutTail: [],
        stderrTail: [],
        argv: [python, ...argv],
      };

      // Spawn the subprocess. CWD is the repo root so the script's
      // sys.path bootstrap finds ma_poc/. The child inherits env so
      // GMAIL_DELEGATED_USER / REPORT_RECIPIENTS / EMAIL_TRANSPORT
      // flow through from however the API server was started.
      const child = spawn(python, argv, {
        cwd: resolve(repoRoot, '..'),
        env: process.env,
        windowsHide: true,
      });
      record.child = child;

      // Stream stdout/stderr into the bounded tail. The Python script
      // logs structured one-liners — splitting on newlines keeps each
      // log entry as one tail line.
      child.stdout.setEncoding('utf-8');
      child.stderr.setEncoding('utf-8');
      child.stdout.on('data', (chunk: string) => {
        for (const line of chunk.split(/\r?\n/)) {
          if (line) pushBounded(record.stdoutTail, line);
        }
      });
      child.stderr.on('data', (chunk: string) => {
        for (const line of chunk.split(/\r?\n/)) {
          if (line) pushBounded(record.stderrTail, line);
        }
      });
      child.on('error', (err: NodeJS.ErrnoException) => {
        record.status = 'failed';
        // Map common spawn errors to actionable messages — bare
        // ``spawn ENOENT`` tells the operator nothing.
        if (err.code === 'ENOENT') {
          record.error = (
            `Python interpreter "${python}" not found on PATH. ` +
            `Set PYTHON_BIN to a valid interpreter (e.g. ` +
            `C:\\path\\to\\.venv\\Scripts\\python.exe or /usr/bin/python3) ` +
            `and restart the API server.`
          );
        } else if (err.code === 'EACCES') {
          record.error = (
            `Permission denied launching "${python}". Check the ` +
            `interpreter's executable bit / ACL.`
          );
        } else {
          record.error = `${err.code ?? 'spawn_error'}: ${err.message}`;
        }
        record.finishedAt = new Date().toISOString();
        record.child = undefined;
      });
      child.on('close', (code) => {
        // Distinct success vs failure messaging in the record.error
        // field for the failure path — the UI tooltip surfaces it
        // verbatim so a stderr tail isn't always required.
        record.status = code === 0 ? 'succeeded' : 'failed';
        record.exitCode = code;
        if (record.status === 'failed' && !record.error) {
          const lastStderr = record.stderrTail[record.stderrTail.length - 1];
          record.error = lastStderr
            ? `Script exited ${code}: ${lastStderr}`
            : `Script exited with non-zero status ${code}; check stderrTail.`;
        }
        record.finishedAt = new Date().toISOString();
        record.child = undefined;
      });

      jobs.set(jobId, record);

      res.status(202).json({
        jobId,
        status: record.status,
        startedAt: record.startedAt,
        argv: record.argv,
      });
    } catch (err) {
      next(err);
    }
  });

  router.get('/jobs/:jobId', (req, res) => {
    gc();
    const job = jobs.get(req.params.jobId);
    if (!job) {
      res.status(404).json({ error: 'job_not_found' });
      return;
    }
    res.json({
      jobId: job.jobId,
      kind: job.kind,
      status: job.status,
      startedAt: job.startedAt,
      finishedAt: job.finishedAt,
      exitCode: job.exitCode,
      stdoutTail: job.stdoutTail,
      stderrTail: job.stderrTail,
      error: job.error,
      argv: job.argv,
    });
  });

  // Lightweight introspection endpoint for the UI to detect that
  // another tab / session already kicked off a job. Returns the
  // currently-running job for the kind, or null.
  router.get('/jobs', (req, res) => {
    gc();
    const kind = req.query.kind as string | undefined;
    const filter = (j: JobRecord) =>
      (!kind || j.kind === kind);
    const items = Array.from(jobs.values())
      .filter(filter)
      .sort((a, b) => b.startedAt.localeCompare(a.startedAt))
      .map((j) => ({
        jobId: j.jobId, kind: j.kind, status: j.status,
        startedAt: j.startedAt, finishedAt: j.finishedAt,
        exitCode: j.exitCode,
      }));
    res.json({ items });
  });

  return router;
}
