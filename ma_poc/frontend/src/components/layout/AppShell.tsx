import { Outlet } from 'react-router-dom';
import { TopNav } from './TopNav';
export function AppShell() {
  return <div className="min-h-screen bg-white dark:bg-slate-950"><TopNav /><main className="mx-auto max-w-7xl px-6 py-6"><Outlet /></main></div>;
}
