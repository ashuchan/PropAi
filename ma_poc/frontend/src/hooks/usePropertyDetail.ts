import { useQuery } from '@tanstack/react-query';
import { fetchPropertyById } from '@/api/properties';
export function usePropertyDetail(id: string | undefined) { return useQuery({ queryKey: ['properties', id], queryFn: () => fetchPropertyById(id!), enabled: !!id }); }
