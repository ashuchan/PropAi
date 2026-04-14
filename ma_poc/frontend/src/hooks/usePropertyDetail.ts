import { useQuery } from '@tanstack/react-query';
import { fetchPropertyById, fetchPropertyReport, fetchPropertyProfile } from '@/api/properties';
export function usePropertyDetail(id: string | undefined) { return useQuery({ queryKey: ['properties', id], queryFn: () => fetchPropertyById(id!), enabled: !!id }); }
export function usePropertyReport(id: string | undefined, enabled = true) {
  return useQuery({ queryKey: ['properties', id, 'report'], queryFn: () => fetchPropertyReport(id!), enabled: !!id && enabled, retry: false });
}
export function usePropertyProfile(id: string | undefined, enabled = true) {
  return useQuery({ queryKey: ['properties', id, 'profile'], queryFn: () => fetchPropertyProfile(id!), enabled: !!id && enabled, retry: false });
}
