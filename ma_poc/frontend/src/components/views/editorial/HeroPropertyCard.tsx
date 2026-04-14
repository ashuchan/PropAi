import { Link } from 'react-router-dom';
import { motion } from 'framer-motion';
import { PropertyImage } from '@/components/shared/PropertyImage';
import { TierBadge } from '@/components/shared/TierBadge';
import { ConcessionTag } from '@/components/shared/ConcessionTag';
import { MetricCard } from '@/components/shared/MetricCard';
import { formatCurrency, formatPercent, formatCostUsd } from '@/utils/formatters';
import { cardHover } from '@/utils/motion';
import type { ApiPropertySummary } from '@/api/properties';

export function HeroPropertyCard({ property }: { property: ApiPropertySummary }) {
  return (
    <motion.div {...cardHover}>
      <Link to={`/properties/${property.id}`} className="block overflow-hidden rounded-xl border border-slate-200 bg-white dark:border-slate-700 dark:bg-slate-900 transition-shadow hover:shadow-lg" data-testid="hero-card">
        <PropertyImage imageUrl={property.imageUrl} propertyId={property.id} stories={property.stories} totalUnits={property.totalUnits} className="h-[200px] w-full" />
        <div className="p-5">
          <div className="flex items-start justify-between gap-3">
            <div>
              <h2 className="font-display text-[22px] text-slate-900 dark:text-slate-100">{property.name}</h2>
              <p className="mt-0.5 text-[13px] text-slate-500 dark:text-slate-400">{property.address}, {property.city}, {property.state} {property.zip}</p>
            </div>
            <TierBadge tier={property.extractionTier} />
          </div>
          {property.activeConcession && <div className="mt-3"><ConcessionTag text={property.activeConcession} /></div>}
          <div className="mt-4 grid grid-cols-2 gap-3 lg:grid-cols-4">
            <MetricCard label="Units" value={property.totalUnits} />
            <MetricCard label="Avg Rent" value={formatCurrency(property.avgAskingRent)} />
            <MetricCard label="Availability" value={formatPercent(property.availabilityRate)} />
            <MetricCard label="LLM Cost" value={formatCostUsd(property.llmCostUsd)} subtitle={property.llmCallCount > 0 ? `${property.llmCallCount} calls` : 'none'} accentColor={property.llmCallCount > 0 ? '#EF9F27' : undefined} />
          </div>
        </div>
      </Link>
    </motion.div>
  );
}
