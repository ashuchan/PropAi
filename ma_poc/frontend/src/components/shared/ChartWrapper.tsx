import { ResponsiveContainer } from 'recharts';
import { LoadingSkeleton } from './LoadingSkeleton';
interface ChartWrapperProps { title: string; height?: number; loading?: boolean; children: React.ReactNode; }
export function ChartWrapper({ title, height = 250, loading = false, children }: ChartWrapperProps) {
  return (
    <div className="rounded-xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 p-4">
      <h4 className="mb-3 text-[13px] font-medium text-slate-700 dark:text-slate-300">{title}</h4>
      {loading ? <div style={{ height }}><LoadingSkeleton variant="text-block" /></div> : <ResponsiveContainer width="100%" height={height}>{children as React.ReactElement}</ResponsiveContainer>}
    </div>
  );
}
