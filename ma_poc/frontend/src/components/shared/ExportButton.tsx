import { Download } from 'lucide-react';
interface ExportButtonProps { onClick: () => void; label?: string; disabled?: boolean; }
export function ExportButton({ onClick, label = 'Export CSV', disabled = false }: ExportButtonProps) {
  return <button onClick={onClick} disabled={disabled} className="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-[12px] font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-300 dark:hover:bg-slate-800 transition-colors" data-testid="export-button"><Download size={13} />{label}</button>;
}
