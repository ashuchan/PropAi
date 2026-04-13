import { apiClient } from './client';
export async function fetchRunHistory(limit = 30) { const { data } = await apiClient.get('/runs', { params: { limit } }); return data; }
export async function fetchLatestRun() { const { data } = await apiClient.get('/runs/latest'); return data; }
export async function fetchRunByDate(date: string) { const { data } = await apiClient.get(`/runs/${date}`); return data; }
