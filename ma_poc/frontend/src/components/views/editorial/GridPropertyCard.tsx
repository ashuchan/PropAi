import { Link } from 'react-router-dom';
import { motion } from 'framer-motion';
import { PropertyImage } from '@/components/shared/PropertyImage';
import { TierBadge } from '@/components/shared/TierBadge';
import { ConcessionTag } from '@/components/shared/ConcessionTag';
import { StatusDot } from '@/components/shared/StatusDot';
import { formatCurrency, formatCostUsd } from '@/utils/formatters';
import { cardHover } from '@/utils/motion';
import type { ApiPropertySummary } from '@/api/properties';

export function GridPropertyCard({ property }: { property: ApiPropertySummary }) {
  const isFailed = property.scrapeStatus === 'FAILED';
  return (
    <motion.div {...cardHover}>
      <Link to={`/properties/${property.id}`} className="block overflow-hidden rounded-xl border border-slate-200 bg-white transition-shadow hover:shadow-md dark:border-slate-700 dark:bg-slate-900">
        {isFailed && <div className="bg-red-500 px-3 py-1 text-[10px] font-medium text-white">Scrape Failed</div>}
        <PropertyImage imageUrl={property.imageUrl} propertyId={property.id} stories={property.stories} totalUnits={property.totalUnits} className="h-24 w-full" />
        <div className="p-3">
          <div className="flex items-start justify-between gap-1"><h3 className="truncate text-[13px] font-medium text-slate-900 dark:text-slate-100">{property.name}</h3><TierBadge tier={property.extractionTier} size="sm" /></div>
          <p className="mt-0.5 truncate text-[11px] text-slate-500 dark:text-slate-400">{property.city}, {property.state}</p>
          <div className="mt-2 space-y-1 text-[12px]">
            <div className="flex justify-between"><span className="text-slate-500 dark:text-slate-400">Avg Rent</span><span className="font-mono text-slate-900 dark:text-slate-100">{formatCurrency(property.avgAskingRent)}</span></div>
            <div className="flex justify-between"><span className="text-slate-500 dark:text-slate-400">Units</span><span className="font-mono text-slate-900 dark:text-slate-100">{property.totalUnits}</span></div>
            <div className="flex items-center justify-between"><span className="text-slate-500 dark:text-slate-400">Status</span><StatusDot status={property.scrapeStatus === 'SUCCESS' ? 'available' : property.scrapeStatus === 'FAILED' ? 'failed' : 'unknown'} label={property.scrapeStatus} /></div>
            {property.llmCallCount > 0 && <div className="flex justify-between"><span className="text-slate-500 dark:text-slate-400">LLM Cost</span><span className="font-mono text-amber-600 dark:text-amber-400" title={`${property.llmCallCount} calls · ${property.llmTokensTotal.toLocaleString()} tokens`}>{formatCostUsd(property.llmCostUsd)}</span></div>}
          </div>
          {property.activeConcession && <div className="mt-2"><ConcessionTag text={property.activeConcession} /></div>}
        </div>
      </Link>
    </motion.div>
  );
}
