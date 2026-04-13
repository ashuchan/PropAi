import { useQuery } from '@tanstack/react-query';
import { fetchLatestDiff, fetchDiffByDate } from '@/api/diff';
export function useDailyDiff(date?: string) { return useQuery({ queryKey: ['diff', date || 'latest'], queryFn: () => (date ? fetchDiffByDate(date) : fetchLatestDiff()) }); }
