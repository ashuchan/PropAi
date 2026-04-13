import { motion } from 'framer-motion';
import { useProperties, usePropertyStats } from '@/hooks/useProperties';
import { useFilterStore } from '@/stores/filterStore';
import { HeroPropertyCard } from './HeroPropertyCard';
import { SidebarPropertyCard } from './SidebarPropertyCard';
import { GridPropertyCard } from './GridPropertyCard';
import { EditorialStats } from './EditorialStats';
import { LoadingSkeleton } from '@/components/shared/LoadingSkeleton';
import { EmptyState } from '@/components/shared/EmptyState';
import { Pagination } from '@/components/shared/Pagination';
import { fadeSlideUp } from '@/utils/motion';

export function EditorialView() {
  const { data, isLoading } = useProperties();
  const { data: stats } = usePropertyStats();
  const { page, setPage } = useFilterStore();
  if (isLoading) return <div className="space-y-6"><LoadingSkeleton variant="metric" count={4} /><LoadingSkeleton variant="card" count={3} /></div>;
  const items = data?.items || [];
  if (items.length === 0) return <EmptyState title="No properties found" description="Try adjusting your filters or search query." />;
  const [hero, ...rest] = items;
  const sidebar = rest.slice(0, 3);
  const grid = rest.slice(3);
  return (
    <motion.div className="space-y-6" data-testid="view-editorial" {...fadeSlideUp}>
      {stats && <EditorialStats stats={stats} />}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-5">
        <div className="lg:col-span-3"><HeroPropertyCard property={hero} /></div>
        <div className="space-y-3 lg:col-span-2">{sidebar.map((p) => <SidebarPropertyCard key={p.id} property={p} />)}</div>
      </div>
      {grid.length > 0 && <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">{grid.map((p) => <GridPropertyCard key={p.id} property={p} />)}</div>}
      {data && data.totalPages > 1 && <div className="flex justify-center pt-4"><Pagination page={page} totalPages={data.totalPages} onPageChange={setPage} /></div>}
    </motion.div>
  );
}
