import { BrowserRouter, Routes, Route } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { AppShell } from '@/components/layout/AppShell';
import { ExplorePage } from '@/pages/ExplorePage';
import { PropertyDetailPage } from '@/pages/PropertyDetailPage';
import { DailyDiffPage } from '@/pages/DailyDiffPage';
import { SystemPage } from '@/pages/SystemPage';
import { ReportsPage } from '@/pages/ReportsPage';
import { NotFoundPage } from '@/pages/NotFoundPage';

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { staleTime: 30_000, gcTime: 300_000, refetchOnWindowFocus: false, retry: 2 },
  },
});

export function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <Routes>
          <Route element={<AppShell />}>
            <Route path="/" element={<ExplorePage />} />
            <Route path="/properties/:id" element={<PropertyDetailPage />} />
            <Route path="/diff" element={<DailyDiffPage />} />
            <Route path="/system" element={<SystemPage />} />
            <Route path="/reports" element={<ReportsPage />} />
            <Route path="*" element={<NotFoundPage />} />
          </Route>
        </Routes>
      </BrowserRouter>
    </QueryClientProvider>
  );
}
