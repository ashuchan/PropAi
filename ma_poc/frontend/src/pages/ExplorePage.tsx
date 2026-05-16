import { useEffect } from 'react';
import { AnimatePresence } from 'framer-motion';
import { useViewStore } from '@/stores/viewStore';
import { FilterPanel } from '@/components/filters/FilterPanel';
import { EditorialView } from '@/components/views/editorial/EditorialView';
import { TerminalView } from '@/components/views/terminal/TerminalView';
import { ErrorBoundary } from '@/components/shared/ErrorBoundary';
export function ExplorePage() {
  const { activeView, setActiveView } = useViewStore();
  // Map view is disabled; reset persisted 'spatial' selections to the default.
  useEffect(() => { if (activeView === 'spatial') setActiveView('editorial'); }, [activeView, setActiveView]);
  const resolvedView = activeView === 'spatial' ? 'editorial' : activeView;
  return <div className="space-y-4"><FilterPanel /><ErrorBoundary><AnimatePresence mode="wait">{resolvedView === 'editorial' && <EditorialView key="editorial" />}{resolvedView === 'terminal' && <TerminalView key="terminal" />}</AnimatePresence></ErrorBoundary></div>;
}
