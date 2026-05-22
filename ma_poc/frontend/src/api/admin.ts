/**
 * @file admin.ts
 * @description Thin client for /api/admin/* — operator-triggered jobs.
 *
 * Two endpoints:
 *
 *   POST /api/admin/email-daily-failures
 *     Spawns the Python ``daily_failures`` script. Returns
 *     ``{ jobId, status, startedAt, argv }`` immediately; the
 *     subprocess runs ~3 minutes in the background.
 *
 *   GET /api/admin/jobs/:jobId
 *     Poll for status. ``status`` is one of ``running`` /
 *     ``succeeded`` / ``failed``. When terminal, ``finishedAt`` +
 *     ``exitCode`` are populated and ``stdoutTail`` / ``stderrTail``
 *     carry the last ~200 log lines.
 */
import { apiClient } from './client';

export type JobStatus = 'running' | 'succeeded' | 'failed';

export interface EmailDailyFailuresRequest {
  /** YYYY-MM-DD. Defaults to today on the backend if omitted. */
  runDate?: string;
  /** Comma-separated. Defaults to ``REPORT_RECIPIENTS`` env var on
   *  the backend if omitted. */
  recipients?: string;
  /** Force-read from a local mirror dir (e.g. ``C:/tmp/run-2026-05-21``)
   *  instead of Cloud SQL / GCS. Operator override; rarely set from UI. */
  localDir?: string;
  /** Force the GCS fallback even when Cloud SQL has rows. */
  useGcs?: boolean;
  /** Build the xlsx + render HTML but do NOT send. Useful smoke. */
  dryRun?: boolean;
}

export interface JobTriggerResponse {
  jobId: string;
  status: JobStatus;
  startedAt: string;
  argv: string[];
}

export interface JobStatusResponse {
  jobId: string;
  kind: string;
  status: JobStatus;
  startedAt: string;
  finishedAt?: string;
  exitCode?: number | null;
  stdoutTail: string[];
  stderrTail: string[];
  error?: string;
  argv: string[];
}

export interface JobConflictResponse {
  /** Returned with HTTP 409 when a job of the same kind is already
   *  running. The UI should latch onto this ``jobId`` and poll. */
  error: 'job_already_running';
  jobId: string;
  startedAt: string;
}

/**
 * Kick off the daily failures email script. Resolves to the new job
 * record or — when 409 — the in-flight job's record so the UI can
 * latch onto it instead of double-spawning.
 */
export async function triggerEmailDailyFailures(
  body: EmailDailyFailuresRequest = {},
): Promise<JobTriggerResponse> {
  try {
    const { data } = await apiClient.post('/admin/email-daily-failures', body);
    return data as JobTriggerResponse;
  } catch (err: any) {
    if (err?.response?.status === 409 && err.response.data?.jobId) {
      const conflict = err.response.data as JobConflictResponse;
      return {
        jobId: conflict.jobId,
        status: 'running',
        startedAt: conflict.startedAt,
        argv: [],
      };
    }
    throw err;
  }
}

/** Poll a job's current status. */
export async function fetchJobStatus(jobId: string): Promise<JobStatusResponse> {
  const { data } = await apiClient.get(`/admin/jobs/${jobId}`);
  return data as JobStatusResponse;
}

/* ────────────────────────────────────────────────────────────────────
 * Preflight — "is the email-send environment configured?"
 *
 * Backed by GET /api/admin/email-daily-failures/preflight. The UI
 * fetches this on mount + after any failure so the operator sees
 * what's wrong without having to click and wait 3 minutes for a
 * doomed subprocess.
 * ──────────────────────────────────────────────────────────────── */

export interface PreflightCheck {
  name: string;
  ok: boolean;
  detail: string;
}

export interface PreflightResponse {
  ok: boolean;
  checks: PreflightCheck[];
  python: { bin: string; found: boolean; version: string | null };
  emailTransport: string;
  delegatedUser: string | null;
  recipients: string[];
  scriptModule: string;
}

export async function fetchEmailPreflight(): Promise<PreflightResponse> {
  const { data } = await apiClient.get('/admin/email-daily-failures/preflight');
  return data as PreflightResponse;
}
