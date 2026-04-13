import { apiClient } from './client';
export async function fetchHealthSummary() { const { data } = await apiClient.get('/health'); return data; }
export async function fetchTierDistribution() { const { data } = await apiClient.get('/health/tiers'); return data; }
export async function fetchTopFailures() { const { data } = await apiClient.get('/health/failures'); return data; }
export async function fetchEntityResolutionStats() { const { data } = await apiClient.get('/health/identity'); return data; }
