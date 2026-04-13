interface LoadingSkeletonProps { variant: 'card' | 'table-row' | 'metric' | 'text-block'; count?: number; }
export function LoadingSkeleton({ variant, count = 1 }: LoadingSkeletonProps) {
  return (
    <div data-testid="loading-skeleton" className="space-y-3">
      {Array.from({ length: count }, (_, i) => (
        <div key={i} className="animate-pulse">
          {variant === 'card' && <div className="rounded-xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 p-4"><div className="h-32 rounded-lg bg-slate-200 dark:bg-slate-700 mb-3" /><div className="h-4 w-3/4 rounded bg-slate-200 dark:bg-slate-700 mb-2" /><div className="h-3 w-1/2 rounded bg-slate-200 dark:bg-slate-700" /></div>}
          {variant === 'table-row' && <div className="flex items-center gap-4 px-4 py-3"><div className="h-4 w-24 rounded bg-slate-200 dark:bg-slate-700" /><div className="h-4 w-32 rounded bg-slate-200 dark:bg-slate-700" /><div className="h-4 w-16 rounded bg-slate-200 dark:bg-slate-700" /></div>}
          {variant === 'metric' && <div className="rounded-lg bg-slate-50 dark:bg-slate-800/50 p-4"><div className="h-3 w-16 rounded bg-slate-200 dark:bg-slate-700 mb-2" /><div className="h-6 w-20 rounded bg-slate-200 dark:bg-slate-700" /></div>}
          {variant === 'text-block' && <div className="space-y-2"><div className="h-4 w-full rounded bg-slate-200 dark:bg-slate-700" /><div className="h-4 w-5/6 rounded bg-slate-200 dark:bg-slate-700" /><div className="h-4 w-4/6 rounded bg-slate-200 dark:bg-slate-700" /></div>}
        </div>
      ))}
    </div>
  );
}
