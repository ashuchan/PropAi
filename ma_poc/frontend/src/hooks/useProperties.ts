import { useQuery } from '@tanstack/react-query';
import { fetchProperties, fetchPropertyStats, searchProperties } from '@/api/properties';
import { useFilterStore } from '@/stores/filterStore';
import { useDebounce } from './useDebounce';

export function useProperties() {
  const { search, cities, tiers, statuses, sortField, sortDirection, page, pageSize } = useFilterStore();
  const debouncedSearch = useDebounce(search);
  return useQuery({
    queryKey: ['properties', { search: debouncedSearch, cities, tiers, statuses, sortField, sortDirection, page, pageSize }],
    queryFn: () => fetchProperties({ search: debouncedSearch || undefined, city: cities.join(',') || undefined, tier: tiers.join(',') || undefined, status: statuses.join(',') || undefined, sort: sortField, dir: sortDirection, page, pageSize }),
  });
}
export function usePropertyStats() { return useQuery({ queryKey: ['properties', 'stats'], queryFn: fetchPropertyStats }); }
export function usePropertySearch(query: string) { const dq = useDebounce(query); return useQuery({ queryKey: ['properties', 'search', dq], queryFn: () => searchProperties(dq), enabled: dq.length >= 2 }); }
