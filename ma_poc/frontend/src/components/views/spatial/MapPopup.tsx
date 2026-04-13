import { Link } from 'react-router-dom';
import { ExternalLink } from 'lucide-react';
import { TierBadge } from '@/components/shared/TierBadge';
import { ConcessionTag } from '@/components/shared/ConcessionTag';
import { formatCurrency, formatPercent } from '@/utils/formatters';
import type { ApiPropertySummary } from '@/api/properties';

export function MapPopup({ property }: { property: ApiPropertySummary }) {
  return (
    <div className="min-w-[220px] p-1" data-testid="map-popup">
      <div className="flex items-start justify-between gap-2"><h3 className="text-[13px] font-medium text-slate-900">{property.name}</h3><TierBadge tier={property.extractionTier} size="sm" /></div>
      <p className="mt-0.5 text-[11px] text-slate-500">{property.address}, {property.city}</p>
      <div className="mt-2 grid grid-cols-3 gap-2 text-center text-[11px]">
        <div><p className="font-mono font-medium text-slate-900">{property.totalUnits}</p><p className="text-slate-500">Units</p></div>
        <div><p className="font-mono font-medium text-slate-900">{formatCurrency(property.avgAskingRent)}</p><p className="text-slate-500">Avg Rent</p></div>
        <div><p className="font-mono font-medium text-slate-900">{formatPercent(property.availabilityRate)}</p><p className="text-slate-500">Avail</p></div>
      </div>
      {property.activeConcession && <div className="mt-2"><ConcessionTag text={property.activeConcession} /></div>}
      <Link to={`/properties/${property.id}`} className="mt-2 inline-flex items-center gap-1 text-[11px] font-medium text-rent-400 hover:text-rent-600">View detail <ExternalLink size={10} /></Link>
    </div>
  );
}
