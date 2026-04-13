import { Inbox } from 'lucide-react';
interface EmptyStateProps { title: string; description: string; action?: { label: string; onClick: () => void }; }
export function EmptyState({ title, description, action }: EmptyStateProps) {
  return (
    <div className="flex flex-col items-center justify-center py-16 text-center" data-testid="empty-state">
      <Inbox size={48} className="mb-4 text-slate-300 dark:text-slate-600" />
      <h3 className="text-[16px] font-medium text-slate-900 dark:text-slate-100">{title}</h3>
      <p className="mt-1 max-w-md text-[13px] text-slate-500 dark:text-slate-400">{description}</p>
      {action && <button onClick={action.onClick} className="mt-4 rounded-lg bg-rent-400 px-4 py-2 text-[13px] font-medium text-white hover:bg-rent-600 transition-colors">{action.label}</button>}
    </div>
  );
}
