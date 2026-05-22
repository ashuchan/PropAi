/**
 * Operator control to trigger the ``daily_failures`` Python script
 * from the UI.
 *
 * Lifecycle:
 *
 *   1. On mount: GET /api/admin/email-daily-failures/preflight to
 *      check that Python + GMAIL_DELEGATED_USER + REPORT_RECIPIENTS
 *      are configured on the API server. Surface the result below
 *      the button so the operator sees the recipients list and any
 *      blocking config issues BEFORE clicking.
 *
 *   2. Click → POST /api/admin/email-daily-failures. The server
 *      re-runs the preflight server-side; if it fails the button
 *      shows the failure inline (no doomed subprocess spawned).
 *
 *   3. While running → 2-second poll of /admin/jobs/:id until
 *      terminal. The button stays disabled.
 *
 *   4. On success → green check + "Sent at HH:MM" persists 30s.
 *      On failure → red x + the mapped error message (ENOENT → "Python
 *      interpreter not found", non-zero exit → last stderr line, etc.).
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { CheckCircle2, Loader2, Mail, XCircle, AlertTriangle } from 'lucide-react';
import { clsx } from 'clsx';
import {
  fetchEmailPreflight,
  fetchJobStatus,
  triggerEmailDailyFailures,
  type JobStatusResponse,
  type PreflightResponse,
} from '@/api/admin';

interface Props {
  /** Default run-date to send; null → the script picks today. */
  runDate?: string | null;
}

type UiState =
  | { kind: 'idle' }
  | { kind: 'sending'; jobId: string }
  | { kind: 'succeeded'; finishedAt: string }
  | { kind: 'failed'; reason: string };

const POLL_INTERVAL_MS = 2000;
const SUCCESS_DISPLAY_MS = 30_000;

export function SendDailyFailuresButton({ runDate }: Props) {
  const [state, setState] = useState<UiState>({ kind: 'idle' });
  const [preflight, setPreflight] = useState<PreflightResponse | null>(null);
  const pollTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const resetTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const refreshPreflight = useCallback(async () => {
    try {
      setPreflight(await fetchEmailPreflight());
    } catch (err: any) {
      // Preflight errors are informational only — the button itself
      // still works; the server will refuse the POST if env is bad.
      console.warn('preflight fetch failed:', err?.message);
    }
  }, []);

  useEffect(() => {
    void refreshPreflight();
    return () => {
      if (pollTimer.current) clearTimeout(pollTimer.current);
      if (resetTimer.current) clearTimeout(resetTimer.current);
    };
  }, [refreshPreflight]);

  const transitionFromJob = (job: JobStatusResponse): UiState => {
    if (job.status === 'running') return { kind: 'sending', jobId: job.jobId };
    if (job.status === 'succeeded') {
      return { kind: 'succeeded', finishedAt: job.finishedAt ?? '' };
    }
    const reason = job.error
      || job.stderrTail[job.stderrTail.length - 1]
      || `exit ${job.exitCode ?? '?'}`;
    return { kind: 'failed', reason };
  };

  const scheduleNextPoll = (jobId: string) => {
    pollTimer.current = setTimeout(async () => {
      try {
        const job = await fetchJobStatus(jobId);
        const next = transitionFromJob(job);
        setState(next);
        if (next.kind === 'sending') {
          scheduleNextPoll(jobId);
        } else if (next.kind === 'succeeded') {
          resetTimer.current = setTimeout(
            () => setState({ kind: 'idle' }),
            SUCCESS_DISPLAY_MS,
          );
        } else if (next.kind === 'failed') {
          // Refresh preflight after a failure — the operator might have
          // just fixed env vars and we want the inline status to reflect
          // that without a page reload.
          void refreshPreflight();
        }
      } catch (err: any) {
        setState({
          kind: 'failed',
          reason: err?.message || 'polling error',
        });
      }
    }, POLL_INTERVAL_MS);
  };

  const handleClick = async () => {
    if (state.kind === 'sending') return;
    setState({ kind: 'sending', jobId: '' });
    try {
      const job = await triggerEmailDailyFailures(
        runDate ? { runDate } : {},
      );
      setState({ kind: 'sending', jobId: job.jobId });
      scheduleNextPoll(job.jobId);
    } catch (err: any) {
      // Server-side preflight rejection (HTTP 412) — surface the
      // specific failed checks so the operator can fix them inline.
      const data = err?.response?.data;
      if (err?.response?.status === 412 && data?.failures) {
        const detail = (data.failures as Array<{ name: string; detail: string }>)
          .map((f) => `${f.name}: ${f.detail}`)
          .join(' | ');
        setState({ kind: 'failed', reason: `Preflight failed — ${detail}` });
        if (data.preflight) setPreflight(data.preflight as PreflightResponse);
        return;
      }
      const reason = data?.error
        || data?.details
        || err?.message
        || 'request failed';
      setState({ kind: 'failed', reason });
    }
  };

  const renderInner = () => {
    switch (state.kind) {
      case 'sending':
        return (
          <>
            <Loader2 size={14} className="animate-spin" />
            <span>Sending report…</span>
          </>
        );
      case 'succeeded':
        return (
          <>
            <CheckCircle2 size={14} />
            <span>Sent</span>
          </>
        );
      case 'failed':
        return (
          <>
            <XCircle size={14} />
            <span>Send failed</span>
          </>
        );
      default:
        return (
          <>
            <Mail size={14} />
            <span>Send Daily Failures Report</span>
          </>
        );
    }
  };

  const toneCls = {
    idle:      'border-slate-300 bg-white text-slate-700 hover:bg-slate-50 dark:border-slate-600 dark:bg-slate-900 dark:text-slate-200 dark:hover:bg-slate-800',
    sending:   'border-rent-400 bg-rent-50 text-rent-800 dark:border-rent-400 dark:bg-rent-900/20 dark:text-rent-200',
    succeeded: 'border-emerald-300 bg-emerald-50 text-emerald-800 dark:border-emerald-700 dark:bg-emerald-950 dark:text-emerald-200',
    failed:    'border-red-300 bg-red-50 text-red-800 dark:border-red-700 dark:bg-red-950 dark:text-red-200',
  }[state.kind];

  const tooltip = state.kind === 'failed'
    ? `Send failed: ${state.reason}`
    : state.kind === 'sending'
      ? state.jobId ? `Job ${state.jobId.slice(0, 8)}… polling every ${POLL_INTERVAL_MS / 1000}s`
                    : 'Spawning job…'
      : undefined;

  const buttonDisabled =
    state.kind === 'sending' ||
    // Disable when preflight has a blocking failure that the body
    // can't override. Recipients can be passed in the body so a
    // missing REPORT_RECIPIENTS doesn't gate the button — every other
    // check does.
    (preflight !== null && !preflight.ok && preflight.checks.some(
      (c) => !c.ok && c.name !== 'default_recipients',
    ));

  const failingChecks = preflight?.checks.filter((c) => !c.ok) ?? [];

  return (
    <div className="flex flex-col items-end gap-1.5">
      <button
        type="button"
        onClick={handleClick}
        disabled={buttonDisabled}
        title={tooltip}
        data-testid="send-daily-failures-button"
        className={clsx(
          'inline-flex items-center gap-2 rounded-lg border px-3 py-1.5 text-[12px] font-medium transition-colors',
          'disabled:cursor-not-allowed disabled:opacity-90',
          toneCls,
        )}
      >
        {renderInner()}
      </button>
      {/* Inline status: recipients + failing checks. Always visible
          when preflight has loaded so the operator never has to wonder
          where the email is going. */}
      {preflight && (
        <div
          className="text-right text-[10px] leading-tight text-slate-500 dark:text-slate-400"
          data-testid="send-daily-failures-meta"
        >
          {preflight.recipients.length > 0 && (
            <div>
              To: <span className="font-mono text-slate-600 dark:text-slate-300">
                {preflight.recipients.slice(0, 2).join(', ')}
                {preflight.recipients.length > 2 ? ` +${preflight.recipients.length - 2}` : ''}
              </span>
            </div>
          )}
          {failingChecks.length > 0 && (
            <div
              className="mt-1 flex items-center justify-end gap-1 text-amber-700 dark:text-amber-400"
              title={failingChecks.map((c) => c.detail).join('\n')}
            >
              <AlertTriangle size={11} />
              <span>{failingChecks.length} preflight issue{failingChecks.length === 1 ? '' : 's'}</span>
            </div>
          )}
        </div>
      )}
      {/* Failure detail row — shows the specific error message so the
          operator can act on it without opening devtools. */}
      {state.kind === 'failed' && (
        <div
          className="max-w-md text-right text-[10px] leading-tight text-red-700 dark:text-red-400"
          data-testid="send-daily-failures-error"
        >
          {state.reason}
        </div>
      )}
    </div>
  );
}
