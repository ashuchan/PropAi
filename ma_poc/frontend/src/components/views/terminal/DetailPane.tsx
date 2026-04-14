import { Link } from 'react-router-dom';
import { ExternalLink } from 'lucide-react';
import { usePropertyDetail } from '@/hooks/usePropertyDetail';
import { MetricCard } from '@/components/shared/MetricCard';
import { TierBadge } from '@/components/shared/TierBadge';
import { ConcessionTag } from '@/components/shared/ConcessionTag';
import { LoadingSkeleton } from '@/components/shared/LoadingSkeleton';
import { EmptyState } from '@/components/shared/EmptyState';
import { UnitTable } from './UnitTable';
import { formatCurrency, formatNumber, formatPercent, formatCostUsd } from '@/utils/formatters';

export function DetailPane({ propertyId }: { propertyId: string | null }) {
  const { data: property, isLoading } = usePropertyDetail(propertyId || undefined);
  if (!propertyId) return <div className="flex flex-1 items-center justify-center rounded-xl border border-slate-200 bg-white dark:border-slate-700 dark:bg-slate-900"><EmptyState title="Select a property" description="Click a property in the list to view details." /></div>;
  if (isLoading) return <div className="flex-1 rounded-xl border border-slate-200 bg-white p-6 dark:border-slate-700 dark:bg-slate-900"><LoadingSkeleton variant="text-block" /><div className="mt-4"><LoadingSkeleton variant="metric" count={3} /></div></div>;
  if (!property) return <div className="flex flex-1 items-center justify-center rounded-xl border border-slate-200 bg-white dark:border-slate-700 dark:bg-slate-900"><EmptyState title="Property not found" description="The selected property could not be loaded." /></div>;
  return (
    <div className="flex-1 overflow-auto rounded-xl border border-slate-200 bg-white dark:border-slate-700 dark:bg-slate-900" data-testid="detail-pane">
      <div className="border-b border-slate-200 p-5 dark:border-slate-700">
        <div className="flex items-start justify-between gap-3">
          <div><h2 className="text-[18px] font-medium text-slate-900 dark:text-slate-100">{property.name}</h2><p className="mt-0.5 text-[13px] text-slate-500 dark:text-slate-400">{property.address}, {property.city}, {property.state} {property.zip}</p></div>
          <div className="flex items-center gap-2"><TierBadge tier={property.extractionTier} size="md" /><Link to={`/properties/${property.id}`} className="rounded-md p-1.5 text-slate-400 hover:bg-slate-100 hover:text-slate-600 dark:hover:bg-slate-800" title="View full detail"><ExternalLink size={16} /></Link></div>
        </div>
        {property.activeConcession && <div className="mt-2"><ConcessionTag text={property.activeConcession} /></div>}
        <p className="mt-3 font-mono text-[28px] font-medium text-slate-900 dark:text-slate-100">{formatCurrency(property.avgAskingRent)}<span className="ml-2 text-[13px] font-normal text-slate-500">avg rent</span></p>
      </div>
      <div className="grid grid-cols-3 gap-3 p-5 lg:grid-cols-6"><MetricCard label="Total Units" value={formatNumber(property.totalUnits)} /><MetricCard label="Available" value={formatNumber(property.availableUnits)} /><MetricCard label="Median Rent" value={formatCurrency(property.medianAskingRent)} /><MetricCard label="Availability" value={formatPercent(property.availabilityRate)} /><MetricCard label="Year Built" value={property.yearBuilt || '—'} /><MetricCard label="LLM Cost" value={formatCostUsd(property.llmCostUsd)} subtitle={property.llmCallCount > 0 ? `${property.llmCallCount} calls` : 'none'} accentColor={property.llmCallCount > 0 ? '#EF9F27' : undefined} /></div>
      {property.units && property.units.length > 0 && <div className="border-t border-slate-200 p-5 dark:border-slate-700"><h3 className="mb-3 text-[14px] font-medium text-slate-900 dark:text-slate-100">Units ({property.units.length})</h3><UnitTable units={property.units} /></div>}
    </div>
  );
}
