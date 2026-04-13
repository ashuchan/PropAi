import { AnimatePresence } from 'framer-motion';
import { useViewStore } from '@/stores/viewStore';
import { FilterPanel } from '@/components/filters/FilterPanel';
import { EditorialView } from '@/components/views/editorial/EditorialView';
import { TerminalView } from '@/components/views/terminal/TerminalView';
import { SpatialView } from '@/components/views/spatial/SpatialView';
import { ErrorBoundary } from '@/components/shared/ErrorBoundary';
export function ExplorePage() {
  const { activeView } = useViewStore();
  return <div className="space-y-4"><FilterPanel /><ErrorBoundary><AnimatePresence mode="wait">{activeView === 'editorial' && <EditorialView key="editorial" />}{activeView === 'terminal' && <TerminalView key="terminal" />}{activeView === 'spatial' && <SpatialView key="spatial" />}</AnimatePresence></ErrorBoundary></div>;
}
