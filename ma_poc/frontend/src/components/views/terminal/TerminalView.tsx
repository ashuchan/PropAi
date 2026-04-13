import { motion } from 'framer-motion';
import { useProperties } from '@/hooks/useProperties';
import { useSelectionStore } from '@/stores/selectionStore';
import { PropertyList } from './PropertyList';
import { DetailPane } from './DetailPane';
import { LoadingSkeleton } from '@/components/shared/LoadingSkeleton';
import { EmptyState } from '@/components/shared/EmptyState';
import { fadeSlideUp } from '@/utils/motion';

export function TerminalView() {
  const { data, isLoading } = useProperties();
  const { selectedPropertyId, setSelectedPropertyId } = useSelectionStore();
  if (isLoading) return <div className="flex gap-4" style={{ height: 'calc(100vh - 140px)' }}><div className="w-[340px]"><LoadingSkeleton variant="table-row" count={10} /></div><div className="flex-1"><LoadingSkeleton variant="card" /></div></div>;
  const items = data?.items || [];
  if (items.length === 0) return <EmptyState title="No properties found" description="Adjust filters to see properties." />;
  const activeId = selectedPropertyId || items[0]?.id;
  return (
    <motion.div className="flex gap-4" style={{ height: 'calc(100vh - 140px)' }} data-testid="view-terminal" {...fadeSlideUp}>
      <PropertyList items={items} selectedId={activeId} onSelect={setSelectedPropertyId} total={data?.total || 0} />
      <DetailPane propertyId={activeId} />
    </motion.div>
  );
}
