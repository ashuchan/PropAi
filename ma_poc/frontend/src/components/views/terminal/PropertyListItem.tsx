import { clsx } from 'clsx';
import { TierBadge } from '@/components/shared/TierBadge';
import { formatCurrency } from '@/utils/formatters';
import type { ApiPropertySummary } from '@/api/properties';

export function PropertyListItem({ property, isSelected, onSelect }: { property: ApiPropertySummary; isSelected: boolean; onSelect: () => void }) {
  return (
    <button onClick={onSelect} className={clsx('w-full px-3 py-2.5 text-left transition-colors', isSelected ? 'border-l-[3px] border-l-rent-400 bg-rent-50/50 dark:bg-rent-900/20' : 'border-l-[3px] border-l-transparent hover:bg-slate-50 dark:hover:bg-slate-800/50')} role="option" aria-selected={isSelected} data-testid="property-list-item">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0 flex-1"><p className="truncate text-[13px] font-medium text-slate-900 dark:text-slate-100">{property.name}</p><p className="truncate text-[11px] text-slate-500 dark:text-slate-400">{property.city}, {property.state}</p></div>
        <TierBadge tier={property.extractionTier} size="sm" />
      </div>
      <div className="mt-1.5 flex items-center gap-3 text-[11px]"><span className="font-mono text-slate-700 dark:text-slate-300">{formatCurrency(property.avgAskingRent)}</span><span className="text-slate-500 dark:text-slate-400">{property.totalUnits} units</span><span className="text-slate-500 dark:text-slate-400">{property.availableUnits} avail</span></div>
    </button>
  );
}
