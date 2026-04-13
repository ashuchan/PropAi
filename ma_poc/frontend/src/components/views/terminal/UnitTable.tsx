import { useState, useMemo } from 'react';
import { clsx } from 'clsx';
import { ChevronUp, ChevronDown } from 'lucide-react';
import { StatusDot } from '@/components/shared/StatusDot';
import { formatCurrency } from '@/utils/formatters';

interface UnitData { unitId: string; askingRent: number; marketRentLow: number; marketRentHigh: number; sqft?: number | null; availabilityStatus: string; availableDate?: string | null; }
type SortField = 'unitId' | 'askingRent' | 'sqft' | 'availabilityStatus';

export function UnitTable({ units }: { units: UnitData[] }) {
  const [sortField, setSortField] = useState<SortField>('unitId');
  const [sortDir, setSortDir] = useState<'asc' | 'desc'>('asc');
  const sorted = useMemo(() => [...units].sort((a, b) => { const mult = sortDir === 'asc' ? 1 : -1; const aV = a[sortField]; const bV = b[sortField]; if (aV == null && bV == null) return 0; if (aV == null) return 1; if (bV == null) return -1; if (typeof aV === 'string') return mult * aV.localeCompare(bV as string); return mult * ((aV as number) - (bV as number)); }), [units, sortField, sortDir]);
  const handleSort = (field: SortField) => { if (sortField === field) setSortDir(d => d === 'asc' ? 'desc' : 'asc'); else { setSortField(field); setSortDir('asc'); } };
  const SortIcon = ({ field }: { field: SortField }) => { if (sortField !== field) return null; return sortDir === 'asc' ? <ChevronUp size={12} /> : <ChevronDown size={12} />; };
  return (
    <div className="overflow-auto" data-testid="unit-table">
      <table className="w-full text-[12px]">
        <thead><tr className="border-b border-slate-200 dark:border-slate-700">
          {([['unitId','Unit'],['askingRent','Rent Range'],['sqft','Sqft'],['availabilityStatus','Status']] as [SortField,string][]).map(([f,l]) => <th key={f} onClick={() => handleSort(f)} className="cursor-pointer px-3 py-2 text-left font-medium uppercase tracking-wide text-slate-500 hover:text-slate-700 dark:text-slate-400"><span className="inline-flex items-center gap-1">{l}<SortIcon field={f} /></span></th>)}
          <th className="px-3 py-2 text-left font-medium uppercase tracking-wide text-slate-500 dark:text-slate-400">Date</th>
        </tr></thead>
        <tbody>{sorted.map((u, i) => <tr key={u.unitId} className={clsx('border-b border-slate-100 dark:border-slate-800', i % 2 === 1 && 'bg-slate-50/50 dark:bg-slate-800/25')}>
          <td className="px-3 py-2 font-mono text-slate-900 dark:text-slate-100">{u.unitId}</td>
          <td className="px-3 py-2 font-mono text-slate-900 dark:text-slate-100">{formatCurrency(u.marketRentLow)} – {formatCurrency(u.marketRentHigh)}</td>
          <td className="px-3 py-2 font-mono text-slate-600 dark:text-slate-400">{u.sqft || '—'}</td>
          <td className="px-3 py-2"><StatusDot status={u.availabilityStatus === 'AVAILABLE' ? 'available' : u.availabilityStatus === 'UNAVAILABLE' ? 'leased' : 'unknown'} label={u.availabilityStatus} /></td>
          <td className="px-3 py-2 text-slate-500 dark:text-slate-400">{u.availableDate || '—'}</td>
        </tr>)}</tbody>
      </table>
    </div>
  );
}
