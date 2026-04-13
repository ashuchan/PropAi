import { useState } from 'react';
import { SlidersHorizontal, ChevronDown, ChevronUp } from 'lucide-react';
import { SearchBar } from './SearchBar';
import { FilterChips } from './FilterChips';
import { SortSelect } from './SortSelect';
export function FilterPanel() {
  const [expanded, setExpanded] = useState(false);
  return (
    <div className="space-y-3">
      <div className="flex items-center gap-3">
        <div className="flex-1"><SearchBar /></div>
        <SortSelect />
        <button onClick={() => setExpanded(!expanded)} className="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-3 py-2 text-[12px] font-medium text-slate-600 hover:bg-slate-50 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-400 dark:hover:bg-slate-800 transition-colors"><SlidersHorizontal size={14} />Filters{expanded ? <ChevronUp size={12} /> : <ChevronDown size={12} />}</button>
      </div>
      {expanded && <div className="rounded-lg border border-slate-200 bg-white p-3 dark:border-slate-700 dark:bg-slate-900"><FilterChips /></div>}
    </div>
  );
}
