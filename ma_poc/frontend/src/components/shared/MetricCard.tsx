import { clsx } from 'clsx';
import { TrendingUp, TrendingDown } from 'lucide-react';

interface MetricCardProps { label: string; value: string | number; subtitle?: string; accentColor?: string; trend?: 'up' | 'down'; 'data-testid'?: string; }

export function MetricCard({ label, value, subtitle, accentColor, trend, 'data-testid': testId }: MetricCardProps) {
  return (
    <div className={clsx('rounded-lg bg-slate-50 dark:bg-slate-800/50 p-4')} style={accentColor ? { borderLeftColor: accentColor, borderLeftWidth: 3 } : undefined} data-testid={testId || `metric-card-${label.toLowerCase().replace(/\s+/g, '-')}`}>
      <p className="text-[11px] font-medium uppercase tracking-wide text-slate-500 dark:text-slate-400">{label}</p>
      <p className="mt-1 font-mono text-[22px] font-medium text-slate-900 dark:text-slate-100">{value}</p>
      <div className="mt-1 flex items-center gap-1">
        {trend && <span className={clsx('flex items-center', trend === 'up' ? 'text-red-500' : 'text-emerald-500')}>{trend === 'up' ? <TrendingUp size={12} /> : <TrendingDown size={12} />}</span>}
        {subtitle && <p className="text-[11px] text-slate-500 dark:text-slate-400">{subtitle}</p>}
      </div>
    </div>
  );
}
