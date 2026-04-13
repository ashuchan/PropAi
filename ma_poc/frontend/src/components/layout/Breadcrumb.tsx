import { Link } from 'react-router-dom';
import { ChevronRight } from 'lucide-react';
interface BreadcrumbItem { label: string; to?: string; }
interface BreadcrumbProps { items: BreadcrumbItem[]; }
export function Breadcrumb({ items }: BreadcrumbProps) {
  return <nav className="flex items-center gap-1 text-[12px]">{items.map((item, i) => <span key={i} className="flex items-center gap-1">{i > 0 && <ChevronRight size={12} className="text-slate-400" />}{item.to ? <Link to={item.to} className="text-slate-500 hover:text-slate-900 dark:text-slate-400 dark:hover:text-slate-100">{item.label}</Link> : <span className="text-slate-900 dark:text-slate-100 font-medium">{item.label}</span>}</span>)}</nav>;
}
