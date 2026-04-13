import { Newspaper, Terminal, Map } from 'lucide-react';
import { clsx } from 'clsx';
import { useViewStore } from '@/stores/viewStore';
import type { ViewMode } from '@/types/views';
const VIEW_OPTIONS: Array<{ mode: ViewMode; label: string; icon: typeof Newspaper }> = [{ mode: 'editorial', label: 'Magazine', icon: Newspaper }, { mode: 'terminal', label: 'Terminal', icon: Terminal }, { mode: 'spatial', label: 'Map', icon: Map }];
export function ViewSwitcher() {
  const { activeView, setActiveView } = useViewStore();
  return (
    <div className="flex items-center rounded-lg border border-slate-200 bg-slate-50 p-0.5 dark:border-slate-700 dark:bg-slate-800/50">
      {VIEW_OPTIONS.map(({ mode, label, icon: Icon }) => <button key={mode} onClick={() => setActiveView(mode)} className={clsx('flex items-center gap-1.5 rounded-md px-3 py-1.5 text-[12px] font-medium transition-all', activeView === mode ? 'bg-white text-slate-900 shadow-sm dark:bg-slate-700 dark:text-slate-100' : 'text-slate-500 hover:text-slate-700 dark:text-slate-400 dark:hover:text-slate-300')} data-testid={`view-${mode}`}><Icon size={14} />{label}</button>)}
    </div>
  );
}
