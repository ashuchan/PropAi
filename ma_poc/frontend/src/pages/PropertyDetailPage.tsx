import { useParams } from 'react-router-dom';
import { usePropertyDetail } from '@/hooks/usePropertyDetail';
import { Breadcrumb } from '@/components/layout/Breadcrumb';
import { MetricCard } from '@/components/shared/MetricCard';
import { TierBadge } from '@/components/shared/TierBadge';
import { ConcessionTag } from '@/components/shared/ConcessionTag';
import { PropertyImage } from '@/components/shared/PropertyImage';
import { LoadingSkeleton } from '@/components/shared/LoadingSkeleton';
import { EmptyState } from '@/components/shared/EmptyState';
import { ErrorBoundary } from '@/components/shared/ErrorBoundary';
import { StatusDot } from '@/components/shared/StatusDot';
import { formatCurrency, formatNumber, formatPercent } from '@/utils/formatters';

export function PropertyDetailPage() {
  const { id } = useParams<{ id: string }>();
  const { data: property, isLoading } = usePropertyDetail(id);
  if (isLoading) return <div className="space-y-6"><LoadingSkeleton variant="text-block" /><LoadingSkeleton variant="metric" count={5} /><LoadingSkeleton variant="card" count={2} /></div>;
  if (!property) return <EmptyState title="Property not found" description="This property could not be loaded." />;
  const units = property.units || [];
  return (
    <ErrorBoundary><div className="space-y-6">
      <Breadcrumb items={[{ label: 'Explore', to: '/' }, { label: property.name }]} />
      <div className="grid grid-cols-1 gap-6 lg:grid-cols-3">
        <div className="lg:col-span-1"><PropertyImage imageUrl={property.imageUrl} propertyId={property.id} stories={property.stories} totalUnits={property.totalUnits} className="h-[200px] w-full rounded-xl" /></div>
        <div className="lg:col-span-2">
          <div className="flex items-start justify-between gap-3">
            <div><h1 className="font-display text-[22px] text-slate-900 dark:text-slate-100">{property.name}</h1><p className="mt-1 text-[13px] text-slate-500 dark:text-slate-400">{property.address}, {property.city}, {property.state} {property.zip}</p>{property.managementCompany && <p className="mt-0.5 text-[12px] text-slate-400 dark:text-slate-500">{property.managementCompany}</p>}</div>
            <div className="flex items-center gap-2"><TierBadge tier={property.extractionTier} size="md" /><StatusDot status={property.scrapeStatus === 'SUCCESS' ? 'available' : property.scrapeStatus === 'FAILED' ? 'failed' : 'unknown'} label={property.scrapeStatus} /></div>
          </div>
          {property.activeConcession && <div className="mt-3"><ConcessionTag text={property.activeConcession} /></div>}
          <div className="mt-4 grid grid-cols-2 gap-2 text-[12px] text-slate-600 dark:text-slate-400">
            <div>Year Built: <span className="font-mono text-slate-900 dark:text-slate-100">{property.yearBuilt || '—'}</span></div>
            <div>Stories: <span className="font-mono text-slate-900 dark:text-slate-100">{property.stories || '—'}</span></div>
            <div>Type: <span className="text-slate-900 dark:text-slate-100">{property.propertyStatus}</span></div>
            <div>Website: {property.websiteUrl ? <a href={property.websiteUrl} target="_blank" rel="noopener noreferrer" className="text-rent-400 hover:underline">Visit</a> : '—'}</div>
          </div>
        </div>
      </div>
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-5"><MetricCard label="Min Rent" value={formatCurrency(property.marketMetrics?.minRent)} /><MetricCard label="Max Rent" value={formatCurrency(property.marketMetrics?.maxRent)} /><MetricCard label="Median Rent" value={formatCurrency(property.medianAskingRent)} /><MetricCard label="Total Units" value={formatNumber(property.totalUnits)} /><MetricCard label="Availability" value={formatPercent(property.availabilityRate)} /></div>
      {units.length > 0 && <div className="rounded-xl border border-slate-200 bg-white p-5 dark:border-slate-700 dark:bg-slate-900"><h2 className="mb-4 text-[16px] font-medium text-slate-900 dark:text-slate-100">Units ({units.length})</h2><div className="overflow-auto"><table className="w-full text-[12px]"><thead><tr className="border-b border-slate-200 dark:border-slate-700"><th className="px-3 py-2 text-left font-medium uppercase tracking-wide text-slate-500">Unit</th><th className="px-3 py-2 text-left font-medium uppercase tracking-wide text-slate-500">Rent Range</th><th className="px-3 py-2 text-left font-medium uppercase tracking-wide text-slate-500">Status</th><th className="px-3 py-2 text-left font-medium uppercase tracking-wide text-slate-500">Available Date</th><th className="px-3 py-2 text-left font-medium uppercase tracking-wide text-slate-500">Concessions</th></tr></thead><tbody>{units.map((unit: any, i: number) => <tr key={unit.unitId} className={i % 2 === 1 ? 'bg-slate-50/50 dark:bg-slate-800/25' : ''}><td className="px-3 py-2 font-mono text-slate-900 dark:text-slate-100">{unit.unitId}</td><td className="px-3 py-2 font-mono text-slate-900 dark:text-slate-100">{formatCurrency(unit.marketRentLow)} – {formatCurrency(unit.marketRentHigh)}</td><td className="px-3 py-2"><StatusDot status={unit.availabilityStatus === 'AVAILABLE' ? 'available' : 'unknown'} label={unit.availabilityStatus} /></td><td className="px-3 py-2 text-slate-500">{unit.availableDate || '—'}</td><td className="px-3 py-2 text-slate-500">{unit.concessions || '—'}</td></tr>)}</tbody></table></div></div>}
    </div></ErrorBoundary>
  );
}
