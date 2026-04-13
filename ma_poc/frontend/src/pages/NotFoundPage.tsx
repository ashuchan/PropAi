import { Link } from 'react-router-dom';
import { Home } from 'lucide-react';
export function NotFoundPage() {
  return <div className="flex flex-col items-center justify-center py-24 text-center"><p className="font-mono text-[64px] font-medium text-slate-200 dark:text-slate-700">404</p><h1 className="mt-2 text-[18px] font-medium text-slate-900 dark:text-slate-100">Page not found</h1><p className="mt-1 text-[13px] text-slate-500 dark:text-slate-400">The page you're looking for doesn't exist.</p><Link to="/" className="mt-6 inline-flex items-center gap-2 rounded-lg bg-rent-400 px-4 py-2 text-[13px] font-medium text-white hover:bg-rent-600 transition-colors"><Home size={16} />Back to Explore</Link></div>;
}
