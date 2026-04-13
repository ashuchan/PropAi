import { MetricCard } from '@/components/shared/MetricCard';
import { formatCurrency, formatNumber, formatPercent } from '@/utils/formatters';
import type { ApiPropertyAggregates } from '@/api/properties';
export function EditorialStats({ stats }: { stats: ApiPropertyAggregates }) {
  return <div className="grid grid-cols-2 gap-3 lg:grid-cols-4"><MetricCard label="Properties" value={formatNumber(stats.totalProperties)} /><MetricCard label="Total Units" value={formatNumber(stats.totalUnits)} /><MetricCard label="Avg Rent" value={formatCurrency(stats.avgRent)} /><MetricCard label="Success Rate" value={formatPercent(stats.successRate)} /></div>;
}
