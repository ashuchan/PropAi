/**
 * @file FloorPlansPage.tsx
 * @description CSV-vs-DB floor-plan comparison dashboard. Shows top-level
 * match stats, a per-property table with match-quality scores, and a
 * deep-dive panel for the selected property listing every CSV row + the
 * matched/unmatched DB floor plans.
 *
 * Backed by `compare_floor_plans_csv.py` writing into
 * `floor_plan_comparison_runs` + `floor_plan_comparison_rows`.
 */

import { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { clsx } from 'clsx';
import { ClipboardList, ChevronRight, X, AlertTriangle } from 'lucide-react';
import {
  fetchFloorPlanComparisonDeepDive,
  fetchFloorPlanComparisonMeta,
  fetchFloorPlanComparisonOverview,
  type FloorPlanComparisonPropertyRow,
} from '@/api/floorPlanComparisons';
import { MetricCard } from '@/components/shared/MetricCard';
import { LoadingSkeleton } from '@/components/shared/LoadingSkeleton';
import { EmptyState } from '@/components/shared/EmptyState';
import { ErrorBoundary } from '@/components/shared/ErrorBoundary';
import { formatNumber, formatPercent } from '@/utils/formatters';

type SortField =
  | 'projName'
  | 'matchRatePct'
  | 'csvRowsTotal'
  | 'dbFloorPlansTotal'
  | 'unmatchedCount'
  | 'missingInCsvCount';

type SortDir = 'asc' | 'desc';

const METHOD_STYLES: Record<string, { label: string; cls: string }> = {
  name_semantic: { label: 'Name match', cls: 'bg-emerald-50 text-emerald-700 dark:bg-emerald-950 dark:text-emerald-200' },
  bed_bath_sqft: { label: 'BR/BA + SqFt', cls: 'bg-blue-50 text-blue-700 dark:bg-blue-950 dark:text-blue-200' },
  bed_bath_only: { label: 'BR/BA only', cls: 'bg-amber-50 text-amber-700 dark:bg-amber-950 dark:text-amber-200' },
  unmatched: { label: 'Unmatched', cls: 'bg-red-50 text-red-700 dark:bg-red-950 dark:text-red-200' },
  property_not_found: { label: 'No DB property', cls: 'bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300' },
};

function MethodBadge({ method }: { method: string }) {
  const s = METHOD_STYLES[method] ?? METHOD_STYLES.unmatched;
  return (
    <span className={clsx('inline-block rounded-md px-2 py-0.5 text-[11px] font-medium', s.cls)}>
      {s.label}
    </span>
  );
}

export function FloorPlansPage() {
  const [selectedRunId, setSelectedRunId] = useState<string | undefined>(undefined);
  const [selectedCanonicalId, setSelectedCanonicalId] = useState<string | null>(null);
  const [filter, setFilter] = useState('');
  const [sortField, setSortField] = useState<SortField>('matchRatePct');
  const [sortDir, setSortDir] = useState<SortDir>('asc');

  const meta = useQuery({
    queryKey: ['fp-comparison', 'meta'],
    queryFn: fetchFloorPlanComparisonMeta,
  });

  const overview = useQuery({
    queryKey: ['fp-comparison', 'overview', selectedRunId ?? 'latest'],
    queryFn: () => fetchFloorPlanComparisonOverview(selectedRunId),
    enabled: meta.data?.supported === true,
  });

  const deepDive = useQuery({
    queryKey: [
      'fp-comparison',
      'deep',
      overview.data?.runId ?? 'latest',
      selectedCanonicalId,
    ],
    queryFn: () =>
      fetchFloorPlanComparisonDeepDive(selectedCanonicalId as string, overview.data?.runId ?? undefined),
    enabled: !!selectedCanonicalId && !!overview.data?.runId,
  });

  const filtered = useMemo<FloorPlanComparisonPropertyRow[]>(() => {
    const rows = overview.data?.properties ?? [];
    const q = filter.trim().toLowerCase();
    const matched = q
      ? rows.filter(
          (r) =>
            (r.projName ?? '').toLowerCase().includes(q) ||
            (r.canonicalId ?? '').toLowerCase().includes(q) ||
            String(r.apartmentId ?? '').includes(q),
        )
      : rows;
    const sorted = [...matched].sort((a, b) => {
      const va = (a[sortField] ?? 0) as number | string;
      const vb = (b[sortField] ?? 0) as number | string;
      const cmp =
        typeof va === 'number' && typeof vb === 'number'
          ? va - vb
          : String(va).localeCompare(String(vb));
      return sortDir === 'asc' ? cmp : -cmp;
    });
    return sorted;
  }, [overview.data, filter, sortField, sortDir]);

  if (meta.isLoading) return <LoadingSkeleton variant="card" count={3} />;

  if (meta.data && !meta.data.supported) {
    return (
      <div className="space-y-6">
        <Header />
        <EmptyState
          title="Floor plan comparisons unavailable"
          description="The active data provider doesn't expose comparison data. Switch to the Postgres provider and run scripts/floor_plans/compare.py to populate this view."
        />
      </div>
    );
  }

  if (overview.isLoading) return <LoadingSkeleton variant="card" count={3} />;

  if (!overview.data || !overview.data.runId) {
    return (
      <div className="space-y-6">
        <Header />
        <EmptyState
          title="No comparison runs yet"
          description="Run python -m ma_poc.scripts.floor_plans.compare --csv ma_poc/config/Floorplan-comparisons.csv to generate the first comparison."
        />
      </div>
    );
  }

  const ov = overview.data;
  const totals = ov.totals;
  const matchedAny = totals.propertiesWithAnyMatch;
  const compared = totals.propertiesCompared;
  const propertyMatchRate = compared > 0 ? (matchedAny * 100) / compared : 0;
  const csvMatchRate =
    totals.csvRowsTotal > 0 ? (totals.matchedTotal * 100) / totals.csvRowsTotal : 0;

  function toggleSort(field: SortField) {
    if (sortField === field) {
      setSortDir(sortDir === 'asc' ? 'desc' : 'asc');
    } else {
      setSortField(field);
      setSortDir(field === 'projName' ? 'asc' : 'desc');
    }
  }

  return (
    <ErrorBoundary>
      <div className="space-y-6" data-testid="floor-plans-page">
        <Header
          runIds={ov.availableRunIds}
          selectedRunId={ov.runId}
          onSelectRunId={(id) => {
            setSelectedRunId(id);
            setSelectedCanonicalId(null);
          }}
        />

        <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
          <MetricCard
            label="Properties matched"
            value={`${formatNumber(matchedAny)} / ${formatNumber(compared)}`}
            subtitle={formatPercent(propertyMatchRate / 100)}
          />
          <MetricCard
            label="CSV rows matched"
            value={`${formatNumber(totals.matchedTotal)} / ${formatNumber(totals.csvRowsTotal)}`}
            subtitle={formatPercent(csvMatchRate / 100)}
          />
          <MetricCard
            label="Name matches"
            value={formatNumber(totals.nameMatchCount)}
            subtitle="rapidfuzz ≥ 85"
          />
          <MetricCard
            label="BR/BA + SqFt matches"
            value={formatNumber(totals.bedBathSqftMatchCount)}
          />
          <MetricCard
            label="BR/BA only"
            value={formatNumber(totals.bedBathOnlyMatchCount)}
            subtitle="no SqFt confirm"
          />
          <MetricCard
            label="Floor plans missing in CSV"
            value={formatNumber(totals.floorPlansMissingInCsv)}
            subtitle={`of ${formatNumber(totals.dbFloorPlansTotal)} DB plans`}
          />
        </div>

        <div className="grid grid-cols-1 gap-6 xl:grid-cols-[1fr_520px]">
          {/* Property table */}
          <section className="rounded-xl border border-slate-200 bg-white dark:border-slate-700 dark:bg-slate-900">
            <div className="flex items-center gap-3 border-b border-slate-200 px-4 py-3 dark:border-slate-700">
              <h2 className="text-[14px] font-medium text-slate-900 dark:text-slate-100">
                Per-property results
              </h2>
              <input
                type="search"
                value={filter}
                onChange={(e) => setFilter(e.target.value)}
                placeholder="Filter by name, canonical id, or apartmentid"
                className="ml-auto w-72 rounded-md border border-slate-200 bg-slate-50 px-3 py-1.5 text-[12px] text-slate-700 placeholder-slate-400 focus:border-rent-400 focus:outline-none dark:border-slate-700 dark:bg-slate-800 dark:text-slate-200"
              />
            </div>
            <div className="max-h-[640px] overflow-auto">
              <table className="w-full text-left text-[12px] tabular-nums">
                <thead className="sticky top-0 bg-slate-50 text-[11px] uppercase tracking-wide text-slate-500 dark:bg-slate-800 dark:text-slate-400">
                  <tr>
                    <Th onClick={() => toggleSort('projName')} active={sortField === 'projName'} dir={sortDir}>
                      Property
                    </Th>
                    <Th onClick={() => toggleSort('csvRowsTotal')} active={sortField === 'csvRowsTotal'} dir={sortDir} align="right">
                      CSV
                    </Th>
                    <Th onClick={() => toggleSort('dbFloorPlansTotal')} active={sortField === 'dbFloorPlansTotal'} dir={sortDir} align="right">
                      DB
                    </Th>
                    <Th onClick={() => toggleSort('matchRatePct')} active={sortField === 'matchRatePct'} dir={sortDir} align="right">
                      Match %
                    </Th>
                    <th className="px-3 py-2 text-right">Name</th>
                    <th className="px-3 py-2 text-right">BR/BA+SF</th>
                    <th className="px-3 py-2 text-right">BR/BA</th>
                    <Th onClick={() => toggleSort('unmatchedCount')} active={sortField === 'unmatchedCount'} dir={sortDir} align="right">
                      Unmatched
                    </Th>
                    <Th onClick={() => toggleSort('missingInCsvCount')} active={sortField === 'missingInCsvCount'} dir={sortDir} align="right">
                      Missing in CSV
                    </Th>
                    <th className="px-3 py-2" />
                  </tr>
                </thead>
                <tbody>
                  {filtered.map((p) => {
                    const isSelected = p.canonicalId === selectedCanonicalId;
                    const rateColor =
                      p.matchRatePct >= 90
                        ? 'text-emerald-600 dark:text-emerald-400'
                        : p.matchRatePct >= 60
                          ? 'text-amber-600 dark:text-amber-400'
                          : 'text-red-600 dark:text-red-400';
                    return (
                      <tr
                        key={p.canonicalId}
                        onClick={() => setSelectedCanonicalId(p.canonicalId)}
                        className={clsx(
                          'cursor-pointer border-b border-slate-100 last:border-b-0 dark:border-slate-800',
                          isSelected
                            ? 'bg-rent-50 dark:bg-rent-900/20'
                            : 'hover:bg-slate-50 dark:hover:bg-slate-800/60',
                        )}
                      >
                        <td className="px-3 py-2">
                          <div className="font-medium text-slate-900 dark:text-slate-100">
                            {p.projName ?? p.canonicalId}
                          </div>
                          <div className="font-mono text-[10px] text-slate-500">
                            {p.apartmentId !== null ? `aid=${p.apartmentId} · ` : ''}
                            {p.canonicalId}
                          </div>
                        </td>
                        <td className="px-3 py-2 text-right">{p.csvRowsTotal}</td>
                        <td className="px-3 py-2 text-right">{p.dbFloorPlansTotal}</td>
                        <td className={clsx('px-3 py-2 text-right font-medium', rateColor)}>
                          {p.matchRatePct.toFixed(0)}%
                        </td>
                        <td className="px-3 py-2 text-right">{p.nameMatchCount}</td>
                        <td className="px-3 py-2 text-right">{p.bedBathSqftMatchCount}</td>
                        <td className="px-3 py-2 text-right">{p.bedBathOnlyMatchCount}</td>
                        <td className={clsx('px-3 py-2 text-right', p.unmatchedCount > 0 && 'text-red-600 dark:text-red-400')}>
                          {p.unmatchedCount}
                        </td>
                        <td className={clsx('px-3 py-2 text-right', p.missingInCsvCount > 0 && 'text-amber-600 dark:text-amber-400')}>
                          {p.missingInCsvCount}
                        </td>
                        <td className="px-2 py-2 text-slate-400">
                          <ChevronRight size={14} />
                        </td>
                      </tr>
                    );
                  })}
                  {filtered.length === 0 && (
                    <tr>
                      <td colSpan={10} className="px-3 py-12 text-center text-slate-500">
                        No properties match the current filter.
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </section>

          {/* Deep dive */}
          <aside className="rounded-xl border border-slate-200 bg-white dark:border-slate-700 dark:bg-slate-900">
            {!selectedCanonicalId && (
              <div className="flex h-full items-center justify-center p-8 text-center text-[13px] text-slate-500">
                Select a property on the left to see every CSV row and the
                matched / missing DB floor plans.
              </div>
            )}
            {selectedCanonicalId && deepDive.isLoading && (
              <div className="p-4">
                <LoadingSkeleton variant="card" count={2} />
              </div>
            )}
            {selectedCanonicalId && deepDive.data && (
              <DeepDivePanel
                data={deepDive.data}
                onClose={() => setSelectedCanonicalId(null)}
              />
            )}
          </aside>
        </div>
      </div>
    </ErrorBoundary>
  );
}

function Header({
  runIds,
  selectedRunId,
  onSelectRunId,
}: {
  runIds?: string[];
  selectedRunId?: string | null;
  onSelectRunId?: (runId: string) => void;
}) {
  return (
    <div className="flex flex-wrap items-center gap-3">
      <ClipboardList size={22} className="text-rent-400" />
      <h1 className="text-[22px] font-medium text-slate-900 dark:text-slate-100">
        Floor Plan Comparison
      </h1>
      <span className="rounded-full bg-slate-100 px-2 py-0.5 font-mono text-[11px] text-slate-600 dark:bg-slate-800 dark:text-slate-400">
        CSV ↔ DB
      </span>
      {runIds && runIds.length > 0 && onSelectRunId && (
        <div className="ml-auto flex items-center gap-2">
          <span className="text-[11px] uppercase tracking-wide text-slate-500">Run</span>
          <select
            value={selectedRunId ?? runIds[0]}
            onChange={(e) => onSelectRunId(e.target.value)}
            className="rounded-md border border-slate-200 bg-white px-2 py-1 font-mono text-[11px] text-slate-700 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-200"
          >
            {runIds.map((id) => (
              <option key={id} value={id}>
                {id}
              </option>
            ))}
          </select>
        </div>
      )}
    </div>
  );
}

function Th({
  children,
  onClick,
  active,
  dir,
  align = 'left',
}: {
  children: React.ReactNode;
  onClick: () => void;
  active: boolean;
  dir: SortDir;
  align?: 'left' | 'right';
}) {
  return (
    <th
      onClick={onClick}
      className={clsx(
        'cursor-pointer px-3 py-2 select-none',
        align === 'right' ? 'text-right' : 'text-left',
        active && 'text-slate-900 dark:text-slate-100',
      )}
    >
      <span className="inline-flex items-center gap-1">
        {children}
        {active && <span>{dir === 'asc' ? '▲' : '▼'}</span>}
      </span>
    </th>
  );
}

function DeepDivePanel({
  data,
  onClose,
}: {
  data: import('@/api/floorPlanComparisons').FloorPlanComparisonDeepDive;
  onClose: () => void;
}) {
  const s = data.summary;
  return (
    <div className="flex h-full flex-col">
      <div className="flex items-start gap-3 border-b border-slate-200 px-4 py-3 dark:border-slate-700">
        <div className="min-w-0 flex-1">
          <div className="truncate text-[14px] font-medium text-slate-900 dark:text-slate-100">
            {s.projName ?? data.canonicalId}
          </div>
          <div className="truncate font-mono text-[10px] text-slate-500">
            {s.apartmentId !== null ? `aid=${s.apartmentId} · ` : ''}
            {data.canonicalId}
          </div>
        </div>
        <button
          onClick={onClose}
          aria-label="Close"
          className="rounded-md p-1 text-slate-500 hover:bg-slate-100 hover:text-slate-900 dark:hover:bg-slate-800 dark:hover:text-slate-100"
        >
          <X size={16} />
        </button>
      </div>

      <div className="grid grid-cols-3 gap-2 border-b border-slate-200 p-3 dark:border-slate-700">
        <Stat label="CSV rows" value={s.csvRowsTotal} />
        <Stat label="DB plans" value={s.dbFloorPlansTotal} />
        <Stat
          label="Match rate"
          value={`${s.matchRatePct.toFixed(0)}%`}
          tone={s.matchRatePct >= 90 ? 'good' : s.matchRatePct >= 60 ? 'warn' : 'bad'}
        />
        <Stat label="Name" value={s.nameMatchCount} />
        <Stat label="BR/BA+SF" value={s.bedBathSqftMatchCount} />
        <Stat label="BR/BA only" value={s.bedBathOnlyMatchCount} />
        <Stat
          label="Unmatched"
          value={s.unmatchedCount}
          tone={s.unmatchedCount > 0 ? 'bad' : 'neutral'}
        />
        <Stat
          label="SqFt confirms"
          value={s.sqftWithinBufferCount}
          tone="neutral"
        />
        <Stat
          label="Missing in CSV"
          value={s.missingInCsvCount}
          tone={s.missingInCsvCount > 0 ? 'warn' : 'neutral'}
        />
      </div>

      <div className="flex-1 overflow-auto">
        <h3 className="px-4 pt-3 text-[11px] font-medium uppercase tracking-wide text-slate-500">
          CSV rows
        </h3>
        <table className="w-full text-left text-[11px] tabular-nums">
          <thead className="text-[10px] uppercase tracking-wide text-slate-500">
            <tr>
              <th className="px-3 py-2">FP #</th>
              <th className="px-3 py-2">Description</th>
              <th className="px-3 py-2 text-right">BR/BA</th>
              <th className="px-3 py-2 text-right">SqFt</th>
              <th className="px-3 py-2">Match</th>
              <th className="px-3 py-2">DB plan</th>
              <th className="px-3 py-2 text-right">Δ SqFt</th>
            </tr>
          </thead>
          <tbody>
            {data.rows.map((r) => (
              <tr
                key={r.id}
                className="border-b border-slate-100 last:border-b-0 dark:border-slate-800"
              >
                <td className="px-3 py-2 font-mono">{r.csvFloorplanNumber ?? '—'}</td>
                <td className="px-3 py-2">{r.csvDescription ?? '—'}</td>
                <td className="px-3 py-2 text-right">
                  {r.csvBed ?? '—'}/{r.csvBath ?? '—'}
                </td>
                <td className="px-3 py-2 text-right">{r.csvArea ?? '—'}</td>
                <td className="px-3 py-2">
                  <MethodBadge method={r.matchMethod} />
                  {r.matchScore !== null && (
                    <span className="ml-1 font-mono text-[10px] text-slate-500">
                      {r.matchScore.toFixed(0)}
                    </span>
                  )}
                </td>
                <td className="px-3 py-2">
                  {r.dbFloorPlanName ?? (r.dbBeds !== null
                    ? `${r.dbBeds}/${r.dbBaths}${r.dbArea !== null ? ` · ${r.dbArea}sf` : ''}`
                    : '—')}
                  {r.dbUnitCount !== null && r.dbUnitCount > 0 && (
                    <span className="ml-1 font-mono text-[10px] text-slate-500">
                      ×{r.dbUnitCount}
                    </span>
                  )}
                </td>
                <td
                  className={clsx(
                    'px-3 py-2 text-right',
                    r.sqftWithinBuffer === false && 'text-amber-600 dark:text-amber-400',
                  )}
                >
                  {r.sqftDelta !== null ? (r.sqftDelta > 0 ? `+${r.sqftDelta}` : r.sqftDelta) : '—'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>

        {data.missingInCsv.length > 0 && (
          <div className="mt-4">
            <h3 className="flex items-center gap-2 px-4 text-[11px] font-medium uppercase tracking-wide text-amber-600 dark:text-amber-400">
              <AlertTriangle size={12} /> DB floor plans missing in CSV
            </h3>
            <table className="mt-1 w-full text-left text-[11px] tabular-nums">
              <thead className="text-[10px] uppercase tracking-wide text-slate-500">
                <tr>
                  <th className="px-3 py-2">Plan</th>
                  <th className="px-3 py-2 text-right">BR/BA</th>
                  <th className="px-3 py-2 text-right">SqFt</th>
                  <th className="px-3 py-2 text-right">Units</th>
                </tr>
              </thead>
              <tbody>
                {data.missingInCsv.map((p, i) => (
                  <tr
                    key={`${p.floorPlanName ?? 'plan'}-${i}`}
                    className="border-b border-slate-100 last:border-b-0 dark:border-slate-800"
                  >
                    <td className="px-3 py-2">{p.floorPlanName ?? '—'}</td>
                    <td className="px-3 py-2 text-right">
                      {p.beds ?? '—'}/{p.baths ?? '—'}
                    </td>
                    <td className="px-3 py-2 text-right">{p.area ?? '—'}</td>
                    <td className="px-3 py-2 text-right">{p.unitCount}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}

function Stat({
  label,
  value,
  tone = 'neutral',
}: {
  label: string;
  value: number | string;
  tone?: 'good' | 'warn' | 'bad' | 'neutral';
}) {
  const toneCls =
    tone === 'good'
      ? 'text-emerald-600 dark:text-emerald-400'
      : tone === 'warn'
        ? 'text-amber-600 dark:text-amber-400'
        : tone === 'bad'
          ? 'text-red-600 dark:text-red-400'
          : 'text-slate-900 dark:text-slate-100';
  return (
    <div className="rounded-md bg-slate-50 px-2 py-2 dark:bg-slate-800/50">
      <div className="text-[10px] uppercase tracking-wide text-slate-500">{label}</div>
      <div className={clsx('font-mono text-[14px] font-medium', toneCls)}>{value}</div>
    </div>
  );
}
