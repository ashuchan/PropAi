import { useQuery } from '@tanstack/react-query';
import { fetchRunHistory, fetchLatestRun } from '@/api/runs';
export function useRunHistory(limit = 30) { return useQuery({ queryKey: ['runs', limit], queryFn: () => fetchRunHistory(limit) }); }
export function useLatestRun() { return useQuery({ queryKey: ['runs', 'latest'], queryFn: fetchLatestRun }); }
