import { useRef, useCallback, useEffect } from 'react';
import { useVirtualizer } from '@tanstack/react-virtual';
import { SearchBar } from '@/components/filters/SearchBar';
import { PropertyListItem } from './PropertyListItem';
import type { ApiPropertySummary } from '@/api/properties';

export function PropertyList({ items, selectedId, onSelect, total }: { items: ApiPropertySummary[]; selectedId: string | null; onSelect: (id: string) => void; total: number }) {
  const parentRef = useRef<HTMLDivElement>(null);
  const virtualizer = useVirtualizer({ count: items.length, getScrollElement: () => parentRef.current, estimateSize: () => 76, overscan: 5 });
  const handleKeyDown = useCallback((e: React.KeyboardEvent) => {
    const idx = items.findIndex(p => p.id === selectedId);
    if (e.key === 'ArrowDown' && idx < items.length - 1) { e.preventDefault(); onSelect(items[idx + 1].id); }
    else if (e.key === 'ArrowUp' && idx > 0) { e.preventDefault(); onSelect(items[idx - 1].id); }
  }, [items, selectedId, onSelect]);
  useEffect(() => { const idx = items.findIndex(p => p.id === selectedId); if (idx >= 0) virtualizer.scrollToIndex(idx, { align: 'auto' }); }, [selectedId, items, virtualizer]);
  return (
    <div className="flex w-[340px] flex-shrink-0 flex-col rounded-xl border border-slate-200 bg-white dark:border-slate-700 dark:bg-slate-900" data-testid="property-list">
      <div className="border-b border-slate-200 p-3 dark:border-slate-700"><SearchBar /><p className="mt-2 text-[11px] text-slate-500 dark:text-slate-400">{total} properties</p></div>
      <div ref={parentRef} className="flex-1 overflow-auto" onKeyDown={handleKeyDown} tabIndex={0} role="listbox">
        <div style={{ height: `${virtualizer.getTotalSize()}px`, position: 'relative' }}>
          {virtualizer.getVirtualItems().map((vi) => { const p = items[vi.index]; return <div key={p.id} style={{ position: 'absolute', top: 0, left: 0, width: '100%', height: `${vi.size}px`, transform: `translateY(${vi.start}px)` }}><PropertyListItem property={p} isSelected={p.id === selectedId} onSelect={() => onSelect(p.id)} /></div>; })}
        </div>
      </div>
    </div>
  );
}
