import { Link, useLocation } from 'react-router-dom';
import { Moon, Sun, Building2 } from 'lucide-react';
import { clsx } from 'clsx';
import { useLocalStorage } from '@/hooks/useLocalStorage';
import { ViewSwitcher } from './ViewSwitcher';
import { useEffect } from 'react';
export function TopNav() {
  const location = useLocation();
  const [isDark, setIsDark] = useLocalStorage('ma-dark-mode', false);
  useEffect(() => { document.documentElement.classList.toggle('dark', isDark); }, [isDark]);
  const isExplorePage = location.pathname === '/';
  const navLinks = [{ to: '/', label: 'Explore' }, { to: '/diff', label: 'Diff' }, { to: '/system', label: 'System' }];
  return (
    <header className="sticky top-0 z-40 border-b border-slate-200 bg-white/80 backdrop-blur-sm dark:border-slate-800 dark:bg-slate-950/80">
      <div className="mx-auto flex h-14 max-w-7xl items-center gap-6 px-6">
        <Link to="/" className="flex items-center gap-2 text-slate-900 dark:text-slate-100"><Building2 size={20} className="text-rent-400" /><span className="text-[14px] font-medium">MA Rent Intelligence</span></Link>
        <nav className="flex items-center gap-1">{navLinks.map((link) => <Link key={link.to} to={link.to} className={clsx('rounded-md px-3 py-1.5 text-[13px] font-medium transition-colors', location.pathname === link.to ? 'bg-slate-100 text-slate-900 dark:bg-slate-800 dark:text-slate-100' : 'text-slate-500 hover:text-slate-900 dark:text-slate-400 dark:hover:text-slate-100')}>{link.label}</Link>)}</nav>
        <div className="flex-1" />
        {isExplorePage && <ViewSwitcher />}
        <button onClick={() => setIsDark(!isDark)} className="rounded-md p-2 text-slate-500 hover:bg-slate-100 hover:text-slate-900 dark:text-slate-400 dark:hover:bg-slate-800 dark:hover:text-slate-100 transition-colors" aria-label={isDark ? 'Switch to light mode' : 'Switch to dark mode'}>{isDark ? <Sun size={18} /> : <Moon size={18} />}</button>
      </div>
    </header>
  );
}
