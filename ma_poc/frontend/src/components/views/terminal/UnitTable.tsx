import { useState, useMemo } from 'react';
import { clsx } from 'clsx';
import { ChevronUp, ChevronDown } from 'lucide-react';
import { StatusDot } from '@/components/shared/StatusDot';
import { formatCurrency } from '@/utils/formatters';

interface UnitData {
  unitId: string; askingRent: number; marketRentLow: number; marketRentHigh: number;
  sqft?: number | null; availabilityStatus: string; availableDate?: string | null;
  beds?: number | null; baths?: number | null; area?: number | null;
  floorPlanName?: string | null; leaseTerm?: number | null;
}
type SortField = 'unitId' | 'askingRent' | 'sqft' | 'availabilityStatus' | 'beds' | 'baths';

export function UnitTable({ units }: { units: UnitData[] }) {
  const [sortField, setSortField] = useState<SortField>('unitId');
  const [sortDir, setSortDir] = useState<'asc' | 'desc'>('asc');

  // Detect V2 by checking if any unit has beds/baths populated
  const isV2 = useMemo(() => units.some(u => u.beds != null || u.baths != null), [units]);

  const sorted = useMemo(() => [...units].sort((a, b) => {
    const mult = sortDir === 'asc' ? 1 : -1;
    const aV = (a as unknown as Record<string, unknown>)[sortField];
    const bV = (b as unknown as Record<string, unknown>)[sortField];
    if (aV == null && bV == null) return 0; if (aV == null) return 1; if (bV == null) return -1;
    if (typeof aV === 'string') return mult * aV.localeCompare(bV as string);
    return mult * ((aV as number) - (bV as number));
  }), [units, sortField, sortDir]);

  const handleSort = (field: SortField) => { if (sortField === field) setSortDir(d => d === 'asc' ? 'desc' : 'asc'); else { setSortField(field); setSortDir('asc'); } };
  const SortIcon = ({ field }: { field: SortField }) => { if (sortField !== field) return null; return sortDir === 'asc' ? <ChevronUp size={12} /> : <ChevronDown size={12} />; };

  const v1Cols: [SortField, string][] = [['unitId','Unit'],['askingRent','Rent Range'],['sqft','Sqft'],['availabilityStatus','Status']];
  const v2Cols: [SortField, string][] = [['unitId','Unit'],['beds','Beds'],['baths','Baths'],['askingRent','Rent Range'],['sqft','Area'],['availabilityStatus','Status']];
  const cols = isV2 ? v2Cols : v1Cols;

  return (
    <div className="overflow-auto" data-testid="unit-table">
      <table className="w-full text-[12px]">
        <thead><tr className="border-b border-slate-200 dark:border-slate-700">
          {cols.map(([f,l]) => <th key={f} onClick={() => handleSort(f)} className="cursor-pointer px-3 py-2 text-left font-medium uppercase tracking-wide text-slate-500 hover:text-slate-700 dark:text-slate-400"><span className="inline-flex items-center gap-1">{l}<SortIcon field={f} /></span></th>)}
          <th className="px-3 py-2 text-left font-medium uppercase tracking-wide text-slate-500 dark:text-slate-400">Date</th>
          {isV2 && <th className="px-3 py-2 text-left font-medium uppercase tracking-wide text-slate-500 dark:text-slate-400">Lease</th>}
        </tr></thead>
        <tbody>{sorted.map((u, i) => <tr key={u.unitId || i} className={clsx('border-b border-slate-100 dark:border-slate-800', i % 2 === 1 && 'bg-slate-50/50 dark:bg-slate-800/25')}>
          <td className="px-3 py-2 font-mono text-slate-900 dark:text-slate-100">{u.unitId || '—'}</td>
          {isV2 && <td className="px-3 py-2 font-mono text-slate-900 dark:text-slate-100">{u.beds != null ? (u.beds === 0 ? 'Studio' : u.beds) : '—'}</td>}
          {isV2 && <td className="px-3 py-2 font-mono text-slate-900 dark:text-slate-100">{u.baths != null ? u.baths : '—'}</td>}
          <td className="px-3 py-2 font-mono text-slate-900 dark:text-slate-100">{formatCurrency(u.marketRentLow)} – {formatCurrency(u.marketRentHigh)}</td>
          <td className="px-3 py-2 font-mono text-slate-600 dark:text-slate-400">{(isV2 ? u.area : u.sqft) || '—'}{isV2 && u.area && u.area > 0 ? ' sf' : ''}</td>
          <td className="px-3 py-2"><StatusDot status={u.availabilityStatus === 'AVAILABLE' ? 'available' : u.availabilityStatus === 'UNAVAILABLE' ? 'leased' : 'unknown'} label={u.availabilityStatus} /></td>
          <td className="px-3 py-2 text-slate-500 dark:text-slate-400">{u.availableDate || '—'}</td>
          {isV2 && <td className="px-3 py-2 font-mono text-slate-600 dark:text-slate-400">{u.leaseTerm ? `${u.leaseTerm} mo` : '—'}</td>}
        </tr>)}</tbody>
      </table>
    </div>
  );
}
