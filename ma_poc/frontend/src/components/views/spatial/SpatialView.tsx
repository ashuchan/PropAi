import { useState } from 'react';
import { motion } from 'framer-motion';
import { PanelRightClose, PanelRightOpen } from 'lucide-react';
import { useProperties } from '@/hooks/useProperties';
import { PropertyMap } from './PropertyMap';
import { MapSidebar } from './MapSidebar';
import { LoadingSkeleton } from '@/components/shared/LoadingSkeleton';
import { fadeSlideUp } from '@/utils/motion';

export function SpatialView() {
  const { data, isLoading } = useProperties();
  const [sidebarOpen, setSidebarOpen] = useState(true);
  if (isLoading) return <LoadingSkeleton variant="card" />;
  const items = data?.items || [];
  return (
    <motion.div className="flex gap-4" style={{ height: 'calc(100vh - 140px)' }} data-testid="view-spatial" {...fadeSlideUp}>
      <div className="relative flex-1 overflow-hidden rounded-xl border border-slate-200 dark:border-slate-700">
        <PropertyMap properties={items} />
        <button onClick={() => setSidebarOpen(!sidebarOpen)} className="absolute right-3 top-3 z-10 rounded-lg bg-white p-2 shadow-md hover:bg-slate-50 dark:bg-slate-800 dark:hover:bg-slate-700">{sidebarOpen ? <PanelRightClose size={16} /> : <PanelRightOpen size={16} />}</button>
      </div>
      {sidebarOpen && <MapSidebar properties={items} />}
    </motion.div>
  );
}
