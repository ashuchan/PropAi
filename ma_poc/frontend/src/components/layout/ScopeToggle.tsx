import { clsx } from 'clsx';
import { useScopeStore, type DataScope } from '@/stores/scopeStore';

const OPTIONS: { value: DataScope; label: string; title: string }[] = [
  { value: 'today', label: 'Today', title: 'Units seen in the most recent scrape run' },
  { value: 'all', label: 'All', title: 'Every unit on record across all runs' },
];

/**
 * Two-state segmented control wired to `useScopeStore`. Persists across
 * pages and refreshes via localStorage. Rendered globally in TopNav.
 */
export function ScopeToggle() {
  const scope = useScopeStore((s) => s.scope);
  const setScope = useScopeStore((s) => s.setScope);
  return (
    <div
      role="group"
      aria-label="Data freshness scope"
      className="inline-flex items-center rounded-md border border-slate-200 bg-slate-50 p-0.5 dark:border-slate-700 dark:bg-slate-800/60"
      data-testid="scope-toggle"
    >
      {OPTIONS.map((opt) => {
        const active = scope === opt.value;
        return (
          <button
            key={opt.value}
            type="button"
            onClick={() => setScope(opt.value)}
            title={opt.title}
            aria-pressed={active}
            data-testid={`scope-toggle-${opt.value}`}
            className={clsx(
              'rounded px-3 py-1 text-[12px] font-medium transition-colors',
              active
                ? 'bg-white text-slate-900 shadow-sm dark:bg-slate-700 dark:text-slate-100'
                : 'text-slate-500 hover:text-slate-900 dark:text-slate-400 dark:hover:text-slate-100',
            )}
          >
            {opt.label}
          </button>
        );
      })}
    </div>
  );
}
