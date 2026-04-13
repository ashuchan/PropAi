import { useQuery } from '@tanstack/react-query';
import { fetchHealthSummary, fetchTierDistribution, fetchTopFailures, fetchEntityResolutionStats } from '@/api/health';
export function useHealthSummary() { return useQuery({ queryKey: ['health'], queryFn: fetchHealthSummary }); }
export function useTierDistribution() { return useQuery({ queryKey: ['health', 'tiers'], queryFn: fetchTierDistribution }); }
export function useTopFailures() { return useQuery({ queryKey: ['health', 'failures'], queryFn: fetchTopFailures }); }
export function useEntityResolution() { return useQuery({ queryKey: ['health', 'identity'], queryFn: fetchEntityResolutionStats }); }
