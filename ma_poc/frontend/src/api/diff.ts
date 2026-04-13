import { apiClient } from './client';
export async function fetchLatestDiff() { const { data } = await apiClient.get('/diff/latest'); return data; }
export async function fetchDiffByDate(date: string) { const { data } = await apiClient.get(`/diff/${date}`); return data; }
