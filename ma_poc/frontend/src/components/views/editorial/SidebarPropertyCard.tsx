import { Link } from 'react-router-dom';
import { PropertyImage } from '@/components/shared/PropertyImage';
import { TierBadge } from '@/components/shared/TierBadge';
import { ConcessionTag } from '@/components/shared/ConcessionTag';
import { formatCurrency } from '@/utils/formatters';
import type { ApiPropertySummary } from '@/api/properties';

export function SidebarPropertyCard({ property }: { property: ApiPropertySummary }) {
  return (
    <Link to={`/properties/${property.id}`} className="flex gap-3 rounded-xl border border-slate-200 bg-white p-3 transition-shadow hover:shadow-md dark:border-slate-700 dark:bg-slate-900">
      <PropertyImage imageUrl={property.imageUrl} propertyId={property.id} stories={property.stories} totalUnits={property.totalUnits} className="h-20 w-20 flex-shrink-0 rounded-lg" />
      <div className="flex-1 min-w-0">
        <div className="flex items-start justify-between gap-2"><h3 className="truncate text-[14px] font-medium text-slate-900 dark:text-slate-100">{property.name}</h3><TierBadge tier={property.extractionTier} /></div>
        <p className="mt-0.5 truncate text-[12px] text-slate-500 dark:text-slate-400">{property.city}, {property.state}</p>
        <div className="mt-2 flex items-center gap-3 text-[12px]">
          <span className="font-mono text-slate-900 dark:text-slate-100">{formatCurrency(property.avgAskingRent)}</span>
          <span className="text-slate-500 dark:text-slate-400">{property.totalUnits} units</span>
          <span className="text-slate-500 dark:text-slate-400">{property.availableUnits} avail</span>
        </div>
        {property.activeConcession && <div className="mt-1.5"><ConcessionTag text={property.activeConcession} /></div>}
      </div>
    </Link>
  );
}
