import { MetricCard } from '@/components/shared/MetricCard';
import { usePropertyStats } from '@/hooks/useProperties';
import { formatCurrency, formatNumber, formatPercent } from '@/utils/formatters';
import { TIER_CHART_COLORS } from '@/utils/colors';
import { Link } from 'react-router-dom';
import type { ApiPropertySummary } from '@/api/properties';

const TIER_LABELS: Record<string, string> = { TIER_1_API: 'API', TIER_2_JSONLD: 'JSON-LD', TIER_3_DOM: 'DOM', TIER_4_LLM: 'LLM', TIER_5_VISION: 'Vision', FAILED: 'Failed' };

export function MapSidebar({ properties }: { properties: ApiPropertySummary[] }) {
  const { data: stats } = usePropertyStats();
  const topByRent = [...properties].sort((a, b) => b.avgAskingRent - a.avgAskingRent).slice(0, 5);
  const mostAvailable = [...properties].sort((a, b) => b.availableUnits - a.availableUnits).slice(0, 5);
  const counts: Record<string, number> = {}; for (const p of properties) counts[p.extractionTier] = (counts[p.extractionTier] || 0) + 1;
  const total = properties.length || 1;
  return (
    <div className="w-[280px] flex-shrink-0 overflow-auto rounded-xl border border-slate-200 bg-white p-4 dark:border-slate-700 dark:bg-slate-900" data-testid="map-sidebar">
      <h3 className="mb-3 text-[14px] font-medium text-slate-900 dark:text-slate-100">Market Summary</h3>
      {stats && <div className="grid grid-cols-2 gap-2 mb-4"><MetricCard label="Properties" value={formatNumber(stats.totalProperties)} /><MetricCard label="Units" value={formatNumber(stats.totalUnits)} /><MetricCard label="Avg Rent" value={formatCurrency(stats.avgRent)} /><MetricCard label="Success" value={formatPercent(stats.successRate)} /></div>}
      <div><h4 className="mb-2 text-[12px] font-medium text-slate-700 dark:text-slate-300">Tier Distribution</h4><div className="space-y-1.5">{Object.entries(TIER_LABELS).map(([tier, label]) => { const c = counts[tier] || 0; return <div key={tier} className="flex items-center gap-2 text-[11px]"><span className="w-14 text-slate-500 dark:text-slate-400">{label}</span><div className="flex-1 h-3 rounded-full bg-slate-100 dark:bg-slate-800 overflow-hidden"><div className="h-full rounded-full transition-all" style={{ width: `${(c / total) * 100}%`, backgroundColor: TIER_CHART_COLORS[tier] || '#868E96' }} /></div><span className="w-8 text-right font-mono text-slate-600 dark:text-slate-400">{c}</span></div>; })}</div></div>
      <div className="mt-4"><h4 className="mb-2 text-[12px] font-medium text-slate-700 dark:text-slate-300">Top by Rent</h4><div className="space-y-1">{topByRent.map((item, i) => <Link key={item.id} to={`/properties/${item.id}`} className="flex items-center gap-2 rounded-md px-2 py-1 text-[11px] hover:bg-slate-50 dark:hover:bg-slate-800"><span className="w-4 font-mono text-slate-400">{i + 1}</span><span className="flex-1 truncate text-slate-700 dark:text-slate-300">{item.name}</span><span className="font-mono text-slate-900 dark:text-slate-100">{formatCurrency(item.avgAskingRent)}</span></Link>)}</div></div>
      <div className="mt-4"><h4 className="mb-2 text-[12px] font-medium text-slate-700 dark:text-slate-300">Most Available</h4><div className="space-y-1">{mostAvailable.map((item, i) => <Link key={item.id} to={`/properties/${item.id}`} className="flex items-center gap-2 rounded-md px-2 py-1 text-[11px] hover:bg-slate-50 dark:hover:bg-slate-800"><span className="w-4 font-mono text-slate-400">{i + 1}</span><span className="flex-1 truncate text-slate-700 dark:text-slate-300">{item.name}</span><span className="font-mono text-slate-900 dark:text-slate-100">{formatNumber(item.availableUnits)}</span></Link>)}</div></div>
    </div>
  );
}
