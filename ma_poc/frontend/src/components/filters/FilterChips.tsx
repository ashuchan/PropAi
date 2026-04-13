import { clsx } from 'clsx';
import { X } from 'lucide-react';
import { useFilterStore } from '@/stores/filterStore';
const TIER_OPTIONS = [{ value: 'TIER_1_API', label: 'API' }, { value: 'TIER_2_JSONLD', label: 'JSON-LD' }, { value: 'TIER_3_DOM', label: 'DOM' }, { value: 'TIER_4_LLM', label: 'LLM' }, { value: 'TIER_5_VISION', label: 'Vision' }, { value: 'FAILED', label: 'Failed' }] as const;
const STATUS_OPTIONS = [{ value: 'SUCCESS', label: 'Success' }, { value: 'FAILED', label: 'Failed' }, { value: 'CARRIED_FORWARD', label: 'Carried' }] as const;
export function FilterChips() {
  const { tiers, statuses, toggleTier, toggleStatus, resetAll } = useFilterStore();
  const hasFilters = tiers.length > 0 || statuses.length > 0;
  return (
    <div className="flex flex-wrap items-center gap-2">
      <span className="text-[11px] font-medium uppercase tracking-wide text-slate-500 dark:text-slate-400">Tier</span>
      {TIER_OPTIONS.map((opt) => <button key={opt.value} onClick={() => toggleTier(opt.value)} className={clsx('rounded-full px-2.5 py-1 text-[11px] font-medium transition-colors', tiers.includes(opt.value) ? 'bg-rent-400 text-white' : 'bg-slate-100 text-slate-600 hover:bg-slate-200 dark:bg-slate-800 dark:text-slate-400 dark:hover:bg-slate-700')} data-testid={`filter-tier-${opt.value}`}>{opt.label}</button>)}
      <span className="ml-2 text-[11px] font-medium uppercase tracking-wide text-slate-500 dark:text-slate-400">Status</span>
      {STATUS_OPTIONS.map((opt) => <button key={opt.value} onClick={() => toggleStatus(opt.value)} className={clsx('rounded-full px-2.5 py-1 text-[11px] font-medium transition-colors', statuses.includes(opt.value) ? 'bg-rent-400 text-white' : 'bg-slate-100 text-slate-600 hover:bg-slate-200 dark:bg-slate-800 dark:text-slate-400 dark:hover:bg-slate-700')} data-testid={`filter-status-${opt.value}`}>{opt.label}</button>)}
      {hasFilters && <button onClick={resetAll} className="ml-2 inline-flex items-center gap-1 text-[11px] text-slate-500 hover:text-slate-700 dark:text-slate-400 dark:hover:text-slate-300"><X size={12} />Clear</button>}
    </div>
  );
}
