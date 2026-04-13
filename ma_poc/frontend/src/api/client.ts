import axios from 'axios';
import { log } from '@/utils/logger';
export const apiClient = axios.create({ baseURL: '/api', timeout: 30_000, headers: { 'Content-Type': 'application/json' } });
apiClient.interceptors.request.use((config) => { (config as any)._startTime = Date.now(); log.debug('API request', { method: config.method, url: config.url }); return config; });
apiClient.interceptors.response.use(
  (response) => { const duration = Date.now() - (response.config as any)._startTime; log.info('API response', { method: response.config.method, url: response.config.url, status: response.status, duration_ms: duration }); return response; },
  (error) => { log.error('API error', { url: error.config?.url, status: error.response?.status, message: error.message }); return Promise.reject(error); },
);
