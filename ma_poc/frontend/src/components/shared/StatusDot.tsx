import { clsx } from 'clsx';
type DotStatus = 'available' | 'leased' | 'unknown' | 'failed';
const STATUS_COLORS: Record<DotStatus, string> = { available: 'bg-emerald-500', leased: 'bg-slate-400', unknown: 'bg-amber-500', failed: 'bg-red-500' };
interface StatusDotProps { status: DotStatus; pulse?: boolean; label?: string; }
export function StatusDot({ status, pulse = false, label }: StatusDotProps) {
  return (
    <span className="inline-flex items-center gap-1.5" data-testid={`status-dot-${status}`}>
      <span className="relative flex h-2 w-2">
        {pulse && <span className={clsx('absolute inline-flex h-full w-full animate-ping rounded-full opacity-75', STATUS_COLORS[status])} />}
        <span className={clsx('relative inline-flex h-2 w-2 rounded-full', STATUS_COLORS[status])} />
      </span>
      {label && <span className="text-[11px] text-slate-600 dark:text-slate-400">{label}</span>}
    </span>
  );
}
