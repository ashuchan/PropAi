import { useState, useEffect } from 'react';
import { clsx } from 'clsx';
import { CheckCircle2, XCircle, AlertTriangle, Info, Clock, FileText, DollarSign } from 'lucide-react';
import { useRunHistory } from '@/hooks/useRunHistory';
import { useQuery } from '@tanstack/react-query';
import { fetchRunByDate } from '@/api/runs';
import { MetricCard } from '@/components/shared/MetricCard';
import { LoadingSkeleton } from '@/components/shared/LoadingSkeleton';
import { EmptyState } from '@/components/shared/EmptyState';
import { ErrorBoundary } from '@/components/shared/ErrorBoundary';
import { formatPercent, formatNumber, formatDuration, formatDate, formatCostUsd } from '@/utils/formatters';

export function ReportsPage() {
  const { data: history, isLoading } = useRunHistory(60);
  const [selectedDate, setSelectedDate] = useState<string | null>(null);

  useEffect(() => {
    if (!selectedDate && history && history.length > 0) setSelectedDate(history[0].date);
  }, [history, selectedDate]);

  const { data: report, isLoading: reportLoading } = useQuery({
    queryKey: ['runs', selectedDate],
    queryFn: () => fetchRunByDate(selectedDate as string),
    enabled: !!selectedDate,
  });

  if (isLoading) return <LoadingSkeleton variant="card" count={3} />;
  if (!history || history.length === 0) return <EmptyState title="No reports available" description="No pipeline runs have been recorded yet." />;

  return (
    <ErrorBoundary><div className="space-y-6">
      <div className="flex items-center gap-3">
        <FileText size={22} className="text-rent-400" />
        <h1 className="text-[22px] font-medium text-slate-900 dark:text-slate-100">Property Reports</h1>
        <span className="ml-2 rounded-full bg-slate-100 px-2 py-0.5 text-[11px] font-mono text-slate-600 dark:bg-slate-800 dark:text-slate-400">{history.length} runs</span>
      </div>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-[280px_1fr]">
        <aside className="space-y-1" data-testid="reports-history-list">
          <h2 className="mb-2 text-[11px] font-medium uppercase tracking-wide text-slate-500">Run History</h2>
          {history.map((run: any) => {
            const ok = run.exitStatus === 'OK';
            const isSelected = selectedDate === run.date;
            return (
              <button
                key={run.date}
                onClick={() => setSelectedDate(run.date)}
                className={clsx(
                  'flex w-full items-center gap-3 rounded-lg border px-3 py-2 text-left transition-colors',
                  isSelected
                    ? 'border-rent-400 bg-rent-50 dark:border-rent-400 dark:bg-rent-900/20'
                    : 'border-slate-200 bg-white hover:bg-slate-50 dark:border-slate-700 dark:bg-slate-900 dark:hover:bg-slate-800'
                )}
              >
                {ok
                  ? <CheckCircle2 size={16} className="shrink-0 text-emerald-500" />
                  : <XCircle size={16} className="shrink-0 text-red-500" />}
                <div className="min-w-0 flex-1">
                  <div className="truncate font-mono text-[12px] text-slate-900 dark:text-slate-100">{run.date}</div>
                  <div className="mt-0.5 flex items-center gap-2 text-[10px] text-slate-500">
                    <span>{run.succeeded}/{run.totalProperties}</span>
                    <span>·</span>
                    <span>{formatPercent(run.successRate, 0)}</span>
                    <span>·</span>
                    <span>{formatDuration(run.durationSeconds)}</span>
                  </div>
                  {run.llmCostUsd > 0 && <div className="mt-0.5 flex items-center gap-1 text-[10px] text-amber-600 dark:text-amber-400"><DollarSign size={10} /><span className="font-mono">{formatCostUsd(run.llmCostUsd)}</span><span className="text-slate-500">· {run.llmCallCount} calls</span></div>}
                </div>
              </button>
            );
          })}
        </aside>

        <section data-testid="reports-detail">
          {reportLoading && <LoadingSkeleton variant="card" count={2} />}
          {!reportLoading && report && <ReportContent report={report} />}
          {!reportLoading && !report && selectedDate && (
            <EmptyState title="Report not found" description={`No report data available for ${selectedDate}.`} />
          )}
        </section>
      </div>
    </div></ErrorBoundary>
  );
}

function ReportContent({ report }: { report: any }) {
  const ok = report.exitStatus === 'OK';
  const successRateColor = report.successRate >= 0.95 ? '#1D9E75' : report.successRate >= 0.8 ? '#EF9F27' : '#E24B4A';
  const issuesBySeverity = report.issues?.bySeverity || {};
  const issuesByCode = report.issues?.byCode || {};
  const byCodeEntries = Object.entries(issuesByCode).sort((a, b) => (b[1] as number) - (a[1] as number));

  return (
    <div className="space-y-5">
      <header className="rounded-xl border border-slate-200 bg-white p-5 dark:border-slate-700 dark:bg-slate-900">
        <div className="flex items-start justify-between gap-3">
          <div>
            <h2 className="font-display text-[22px] text-slate-900 dark:text-slate-100">Daily Run Report</h2>
            <p className="mt-1 font-mono text-[13px] text-slate-500">{formatDate(report.date, 'long')}</p>
          </div>
          <span
            className={clsx(
              'rounded-full px-3 py-1 text-[11px] font-mono font-medium',
              ok ? 'bg-emerald-50 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-200'
                 : 'bg-red-50 text-red-800 dark:bg-red-950 dark:text-red-200'
            )}
          >
            {ok ? 'OK' : report.exitStatus}
          </span>
        </div>
        <div className="mt-4 grid grid-cols-2 gap-3 text-[12px] lg:grid-cols-4">
          <div className="flex items-center gap-2"><Clock size={14} className="text-slate-400" /><div><div className="text-slate-500">Started</div><div className="font-mono text-slate-900 dark:text-slate-100">{formatDate(report.startedAt, 'short')}</div></div></div>
          <div className="flex items-center gap-2"><Clock size={14} className="text-slate-400" /><div><div className="text-slate-500">Finished</div><div className="font-mono text-slate-900 dark:text-slate-100">{formatDate(report.finishedAt, 'short')}</div></div></div>
          <div><div className="text-slate-500">Duration</div><div className="font-mono text-slate-900 dark:text-slate-100">{formatDuration(report.durationSeconds)}</div></div>
          <div><div className="text-slate-500">Retry Mode</div><div className="font-mono text-slate-900 dark:text-slate-100">{report.retryMode || '—'}</div></div>
        </div>
      </header>

      <section>
        <h3 className="mb-2 text-[11px] font-medium uppercase tracking-wide text-slate-500">Totals</h3>
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-5">
          <MetricCard label="Success Rate" value={formatPercent(report.successRate)} accentColor={successRateColor} />
          <MetricCard label="Properties" value={`${formatNumber(report.succeeded)} / ${formatNumber(report.totalProperties)}`} />
          <MetricCard label="Failed" value={formatNumber(report.failed)} accentColor={report.failed > 0 ? '#E24B4A' : undefined} />
          <MetricCard label="Units Extracted" value={formatNumber(report.unitsExtracted)} />
          <MetricCard label="LLM Cost" value={formatCostUsd(report.llmCostUsd)} subtitle={`${formatNumber(report.llmCallCount)} calls`} accentColor={report.llmCostUsd > 0 ? '#EF9F27' : undefined} />
        </div>
      </section>

      <LlmSection report={report} />

      <section className="rounded-xl border border-slate-200 bg-white p-5 dark:border-slate-700 dark:bg-slate-900">
        <h3 className="mb-3 text-[16px] font-medium text-slate-900 dark:text-slate-100">Pipeline Breakdown</h3>
        <div className="grid grid-cols-2 gap-x-6 gap-y-2 text-[12px] lg:grid-cols-3">
          <KV label="CSV Rows" value={report.totals?.csvRowsTotal} />
          <KV label="Rows Eligible" value={report.totals?.rowsEligible} />
          <KV label="Rows Processed" value={report.totals?.rowsProcessed} />
          <KV label="Rows Succeeded" value={report.totals?.rowsSucceeded} accent={report.totals?.rowsSucceeded > 0 ? 'emerald' : undefined} />
          <KV label="Rows Failed" value={report.totals?.rowsFailed} accent={report.totals?.rowsFailed > 0 ? 'red' : undefined} />
          <KV label="Properties in Output" value={report.totals?.propertiesInOutput} />
        </div>
      </section>

      <section className="rounded-xl border border-slate-200 bg-white p-5 dark:border-slate-700 dark:bg-slate-900">
        <h3 className="mb-3 text-[16px] font-medium text-slate-900 dark:text-slate-100">State Diff vs Previous Run</h3>
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          <StateDiffCard label="New" value={report.stateDiff?.unitsNew} color="#378ADD" />
          <StateDiffCard label="Updated" value={report.stateDiff?.unitsUpdated} color="#EF9F27" />
          <StateDiffCard label="Unchanged" value={report.stateDiff?.unitsUnchanged} color="#868E96" />
          <StateDiffCard label="Disappeared" value={report.stateDiff?.unitsDisappeared} color="#E24B4A" />
        </div>
        <div className="mt-3 flex items-center gap-4 text-[12px] text-slate-500">
          <span>Extracted: <span className="font-mono text-slate-900 dark:text-slate-100">{formatNumber(report.stateDiff?.unitsExtracted)}</span></span>
          <span>Carry-forward: <span className="font-mono text-slate-900 dark:text-slate-100">{formatNumber(report.stateDiff?.unitsCarriedForward)}</span></span>
          <span>Properties using carry-forward: <span className="font-mono text-slate-900 dark:text-slate-100">{formatNumber(report.stateDiff?.carryForwardCount)}</span></span>
        </div>
      </section>

      <section className="rounded-xl border border-slate-200 bg-white p-5 dark:border-slate-700 dark:bg-slate-900">
        <h3 className="mb-3 text-[16px] font-medium text-slate-900 dark:text-slate-100">Issues ({formatNumber(report.issues?.total || 0)})</h3>
        <div className="mb-4 flex flex-wrap gap-2">
          <SeverityPill icon={<XCircle size={12} />} label="ERROR" count={issuesBySeverity.ERROR || 0} tone="red" />
          <SeverityPill icon={<AlertTriangle size={12} />} label="WARNING" count={issuesBySeverity.WARNING || 0} tone="amber" />
          <SeverityPill icon={<Info size={12} />} label="INFO" count={issuesBySeverity.INFO || 0} tone="blue" />
        </div>
        {byCodeEntries.length > 0 ? (
          <div className="space-y-2">
            <h4 className="text-[11px] font-medium uppercase tracking-wide text-slate-500">Top Codes</h4>
            {byCodeEntries.slice(0, 8).map(([code, count]) => (
              <div key={code} className="flex items-center justify-between rounded-lg bg-slate-50 px-3 py-2 dark:bg-slate-800/50">
                <code className="text-[12px] text-slate-900 dark:text-slate-100">{code}</code>
                <span className="font-mono text-[12px] text-slate-600 dark:text-slate-400">{count as number}</span>
              </div>
            ))}
          </div>
        ) : (
          <p className="text-[12px] text-slate-500">No issues reported.</p>
        )}
      </section>

      {report.failedProperties && report.failedProperties.length > 0 && (
        <section className="rounded-xl border border-red-200 bg-red-50/50 p-5 dark:border-red-900 dark:bg-red-950/20">
          <h3 className="mb-3 text-[16px] font-medium text-red-900 dark:text-red-200">Failed Properties ({report.failedProperties.length})</h3>
          <div className="overflow-auto">
            <table className="w-full text-[12px]">
              <thead><tr className="border-b border-red-200 dark:border-red-900">
                <th className="px-3 py-2 text-left font-medium uppercase tracking-wide text-red-700 dark:text-red-300">Row</th>
                <th className="px-3 py-2 text-left font-medium uppercase tracking-wide text-red-700 dark:text-red-300">Canonical ID</th>
                <th className="px-3 py-2 text-left font-medium uppercase tracking-wide text-red-700 dark:text-red-300">Reason</th>
              </tr></thead>
              <tbody>{report.failedProperties.map((fp: any, i: number) => (
                <tr key={i} className="border-b border-red-100 last:border-0 dark:border-red-900/50">
                  <td className="px-3 py-2 font-mono text-slate-900 dark:text-slate-100">{fp.rowIndex}</td>
                  <td className="px-3 py-2 font-mono text-slate-900 dark:text-slate-100">{fp.canonicalId}</td>
                  <td className="px-3 py-2 text-red-700 dark:text-red-300">{fp.reason}</td>
                </tr>
              ))}</tbody>
            </table>
          </div>
        </section>
      )}
    </div>
  );
}

function LlmSection({ report }: { report: any }) {
  const br = report.llmBreakdown || {};
  const hasLlm = (report.llmCostUsd || 0) > 0 || (report.llmCallCount || 0) > 0;
  return (
    <section className="rounded-xl border border-amber-200 bg-amber-50/40 p-5 dark:border-amber-900/50 dark:bg-amber-950/20">
      <div className="mb-3 flex items-center gap-2">
        <DollarSign size={18} className="text-amber-600 dark:text-amber-400" />
        <h3 className="text-[16px] font-medium text-slate-900 dark:text-slate-100">LLM Cost & Usage</h3>
      </div>
      {!hasLlm && <p className="text-[12px] text-slate-500">No LLM calls were made during this run.</p>}
      {hasLlm && (
        <>
          <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
            <MetricCard label="Total Cost" value={formatCostUsd(report.llmCostUsd)} accentColor="#EF9F27" />
            <MetricCard label="Total Calls" value={formatNumber(report.llmCallCount)} subtitle={`${br.successfulCalls ?? 0} ok · ${br.failedCalls ?? 0} failed`} />
            <MetricCard label="Total Tokens" value={formatNumber(report.llmTokensTotal)} subtitle={`${formatNumber(br.tokensInput ?? 0)} in / ${formatNumber(br.tokensOutput ?? 0)} out`} />
            <MetricCard label="Properties Using LLM" value={formatNumber(br.propertiesWithLlm ?? 0)} />
          </div>
          <div className="mt-4 grid grid-cols-1 gap-4 lg:grid-cols-3">
            <LlmBreakdownCard title="By Tier" entries={br.byTier} />
            <LlmBreakdownCard title="By Provider" entries={br.byProvider} />
            <LlmBreakdownCard title="By Model" entries={br.byModel} />
          </div>
        </>
      )}
    </section>
  );
}

function LlmBreakdownCard({ title, entries }: { title: string; entries?: Record<string, { calls: number; costUsd: number; tokensTotal: number }> }) {
  const rows = Object.entries(entries || {}).sort((a, b) => b[1].costUsd - a[1].costUsd);
  return (
    <div className="rounded-lg border border-slate-200 bg-white p-3 dark:border-slate-700 dark:bg-slate-900">
      <h4 className="mb-2 text-[11px] font-medium uppercase tracking-wide text-slate-500">{title}</h4>
      {rows.length === 0 ? (
        <p className="text-[11px] text-slate-500">—</p>
      ) : (
        <ul className="space-y-1.5">
          {rows.map(([key, v]) => (
            <li key={key} className="flex items-center justify-between gap-2 text-[12px]">
              <span className="truncate text-slate-700 dark:text-slate-300" title={key}>{key}</span>
              <span className="font-mono text-slate-900 dark:text-slate-100">{formatCostUsd(v.costUsd)}</span>
              <span className="w-12 text-right font-mono text-[10px] text-slate-500">{v.calls}×</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function KV({ label, value, accent }: { label: string; value: number | undefined | null; accent?: 'emerald' | 'red' }) {
  const accentCls = accent === 'emerald' ? 'text-emerald-600 dark:text-emerald-400' : accent === 'red' ? 'text-red-600 dark:text-red-400' : 'text-slate-900 dark:text-slate-100';
  return (
    <div className="flex items-center justify-between border-b border-slate-100 py-1.5 dark:border-slate-800">
      <span className="text-slate-500">{label}</span>
      <span className={clsx('font-mono', accentCls)}>{formatNumber(value ?? 0)}</span>
    </div>
  );
}

function StateDiffCard({ label, value, color }: { label: string; value: number | undefined; color: string }) {
  return (
    <div className="rounded-lg bg-slate-50 p-4 dark:bg-slate-800/50">
      <div className="text-[11px] uppercase tracking-wide text-slate-500">{label}</div>
      <div className="mt-1 font-mono text-[22px]" style={{ color }}>{formatNumber(value ?? 0)}</div>
    </div>
  );
}

function SeverityPill({ icon, label, count, tone }: { icon: React.ReactNode; label: string; count: number; tone: 'red' | 'amber' | 'blue' }) {
  const toneCls = {
    red: 'bg-red-50 text-red-800 dark:bg-red-950 dark:text-red-200',
    amber: 'bg-amber-50 text-amber-800 dark:bg-amber-950 dark:text-amber-200',
    blue: 'bg-blue-50 text-blue-800 dark:bg-blue-950 dark:text-blue-200',
  }[tone];
  return (
    <span className={clsx('inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-[11px] font-mono', toneCls)}>
      {icon}{label} <span className="opacity-70">·</span> {count}
    </span>
  );
}
