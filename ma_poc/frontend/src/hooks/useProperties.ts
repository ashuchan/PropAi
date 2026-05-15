import { useQuery } from '@tanstack/react-query';
import { fetchProperties, fetchPropertyStats, searchProperties } from '@/api/properties';
import { useFilterStore } from '@/stores/filterStore';
import { useScopeStore } from '@/stores/scopeStore';
import { useDebounce } from './useDebounce';

export function useProperties() {
  const { search, cities, tiers, statuses, sortField, sortDirection, page, pageSize } = useFilterStore();
  const scope = useScopeStore((s) => s.scope);
  const debouncedSearch = useDebounce(search);
  return useQuery({
    queryKey: ['properties', { scope, search: debouncedSearch, cities, tiers, statuses, sortField, sortDirection, page, pageSize }],
    queryFn: () => fetchProperties({ scope, search: debouncedSearch || undefined, city: cities.join(',') || undefined, tier: tiers.join(',') || undefined, status: statuses.join(',') || undefined, sort: sortField, dir: sortDirection, page, pageSize }),
  });
}
export function usePropertyStats() {
  const scope = useScopeStore((s) => s.scope);
  return useQuery({ queryKey: ['properties', 'stats', scope], queryFn: () => fetchPropertyStats(scope) });
}
export function usePropertySearch(query: string) {
  const scope = useScopeStore((s) => s.scope);
  const dq = useDebounce(query);
  return useQuery({ queryKey: ['properties', 'search', dq, scope], queryFn: () => searchProperties(dq, 20, scope), enabled: dq.length >= 2 });
}
