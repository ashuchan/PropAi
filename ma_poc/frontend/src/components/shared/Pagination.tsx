import { ChevronLeft, ChevronRight } from 'lucide-react';
import { clsx } from 'clsx';
interface PaginationProps { page: number; totalPages: number; onPageChange: (page: number) => void; }
function getVisiblePages(current: number, total: number): (number | '...')[] {
  if (total <= 7) return Array.from({ length: total }, (_, i) => i + 1);
  const pages: (number | '...')[] = [1];
  if (current > 3) pages.push('...');
  for (let i = Math.max(2, current - 1); i <= Math.min(total - 1, current + 1); i++) pages.push(i);
  if (current < total - 2) pages.push('...');
  pages.push(total); return pages;
}
export function Pagination({ page, totalPages, onPageChange }: PaginationProps) {
  if (totalPages <= 1) return null;
  const pages = getVisiblePages(page, totalPages);
  return (
    <nav className="flex items-center gap-1" data-testid="pagination">
      <button onClick={() => onPageChange(page - 1)} disabled={page <= 1} className="rounded-md p-1.5 text-slate-500 hover:bg-slate-100 disabled:opacity-30 dark:hover:bg-slate-800"><ChevronLeft size={16} /></button>
      {pages.map((p, i) => p === '...' ? <span key={`e-${i}`} className="px-1 text-[12px] text-slate-400">...</span> : <button key={p} onClick={() => onPageChange(p as number)} className={clsx('min-w-[28px] rounded-md px-2 py-1 text-[12px] font-medium transition-colors', p === page ? 'bg-rent-400 text-white' : 'text-slate-600 hover:bg-slate-100 dark:text-slate-400 dark:hover:bg-slate-800')}>{p}</button>)}
      <button onClick={() => onPageChange(page + 1)} disabled={page >= totalPages} className="rounded-md p-1.5 text-slate-500 hover:bg-slate-100 disabled:opacity-30 dark:hover:bg-slate-800"><ChevronRight size={16} /></button>
    </nav>
  );
}
