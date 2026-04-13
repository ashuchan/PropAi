import { ArrowUpDown } from 'lucide-react';
import { useFilterStore } from '@/stores/filterStore';
const SORT_OPTIONS = [{ value: 'name', label: 'Name' }, { value: 'avgAskingRent', label: 'Avg Rent' }, { value: 'totalUnits', label: 'Units' }, { value: 'availabilityRate', label: 'Availability' }, { value: 'city', label: 'City' }];
export function SortSelect() {
  const { sortField, sortDirection, setSort } = useFilterStore();
  return (
    <div className="flex items-center gap-1" data-testid="sort-select">
      <select value={sortField} onChange={(e) => setSort(e.target.value, sortDirection)} className="rounded-lg border border-slate-200 bg-white py-2 pl-3 pr-8 text-[12px] text-slate-700 focus:border-rent-400 focus:outline-none dark:border-slate-700 dark:bg-slate-900 dark:text-slate-300">{SORT_OPTIONS.map((opt) => <option key={opt.value} value={opt.value}>{opt.label}</option>)}</select>
      <button onClick={() => setSort(sortField, sortDirection === 'asc' ? 'desc' : 'asc')} className="rounded-md p-2 text-slate-500 hover:bg-slate-100 dark:hover:bg-slate-800 transition-colors"><ArrowUpDown size={14} /></button>
    </div>
  );
}
