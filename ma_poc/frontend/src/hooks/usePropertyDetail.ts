import { useQuery } from '@tanstack/react-query';
import { fetchPropertyById, fetchPropertyReport, fetchPropertyProfile } from '@/api/properties';
import { useScopeStore } from '@/stores/scopeStore';
export function usePropertyDetail(id: string | undefined) {
  const scope = useScopeStore((s) => s.scope);
  return useQuery({ queryKey: ['properties', id, scope], queryFn: () => fetchPropertyById(id!, scope), enabled: !!id });
}
export function usePropertyReport(id: string | undefined, enabled = true) {
  return useQuery({ queryKey: ['properties', id, 'report'], queryFn: () => fetchPropertyReport(id!), enabled: !!id && enabled, retry: false });
}
export function usePropertyProfile(id: string | undefined, enabled = true) {
  return useQuery({ queryKey: ['properties', id, 'profile'], queryFn: () => fetchPropertyProfile(id!), enabled: !!id && enabled, retry: false });
}
