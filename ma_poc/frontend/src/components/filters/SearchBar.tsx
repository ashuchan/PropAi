import { Search, X } from 'lucide-react';
import { useFilterStore } from '@/stores/filterStore';
export function SearchBar() {
  const { search, setSearch } = useFilterStore();
  return (
    <div className="relative" data-testid="search-input">
      <Search size={16} className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" />
      <input type="text" value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Search properties..." className="w-full rounded-lg border border-slate-200 bg-white py-2 pl-9 pr-8 text-[13px] text-slate-900 placeholder-slate-400 focus:border-rent-400 focus:outline-none focus:ring-1 focus:ring-rent-400 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-100 dark:placeholder-slate-500" />
      {search && <button onClick={() => setSearch('')} className="absolute right-2 top-1/2 -translate-y-1/2 rounded p-0.5 text-slate-400 hover:text-slate-600 dark:hover:text-slate-300"><X size={14} /></button>}
    </div>
  );
}
