import { apiClient } from './client';

export interface LlmPropertySummary {
  propertyId: string;
  calls: number;
  tokensInput: number;
  tokensOutput: number;
  tokensTotal: number;
  costUsd: number;
  successful: number;
  failed: number;
}

export interface LlmBreakdownEntry { calls: number; costUsd: number; tokensTotal: number }

export interface LlmRunReport {
  date: string;
  generatedAt: string;
  totalPropertiesWithLlm: number;
  totalCalls: number;
  successfulCalls: number;
  failedCalls: number;
  tokensInput: number;
  tokensOutput: number;
  tokensTotal: number;
  totalCostUsd: number;
  byTier: Record<string, LlmBreakdownEntry>;
  byProvider: Record<string, LlmBreakdownEntry>;
  byModel: Record<string, LlmBreakdownEntry>;
  byProperty: LlmPropertySummary[];
}

export interface LlmInteraction {
  tier: string;
  callType: string;
  provider: string;
  model: string;
  tokensInput: number;
  tokensOutput: number;
  tokensTotal: number;
  costUsd: number;
  latencyMs: number;
  timestamp: string;
  success: boolean;
  error: string | null;
  systemPrompt?: string;
  userPrompt?: string;
  rawResponse?: string;
}

export interface LlmPropertyDetail {
  propertyId: string;
  generatedAt: string;
  totalCalls: number;
  successfulCalls: number;
  failedCalls: number;
  tokensInput: number;
  tokensOutput: number;
  tokensTotal: number;
  totalCostUsd: number;
  byTier: Record<string, LlmBreakdownEntry>;
  byProvider: Record<string, LlmBreakdownEntry>;
  byModel: Record<string, LlmBreakdownEntry>;
  interactions: LlmInteraction[];
}

export async function fetchRunHistory(limit = 30) { const { data } = await apiClient.get('/runs', { params: { limit } }); return data; }
export async function fetchLatestRun() { const { data } = await apiClient.get('/runs/latest'); return data; }
export async function fetchRunByDate(date: string) { const { data } = await apiClient.get(`/runs/${date}`); return data; }

/** Full LLM aggregate for a run (includes per-property summary array). */
export async function fetchLlmReport(date: string): Promise<LlmRunReport | null> {
  try {
    const { data } = await apiClient.get<LlmRunReport>(`/runs/${date}/llm`);
    return data;
  } catch (err: any) {
    if (err?.response?.status === 404) return null;
    throw err;
  }
}

/** Full per-property LLM detail (every call, prompt, response). */
export async function fetchLlmPropertyDetail(date: string, propertyId: string): Promise<LlmPropertyDetail | null> {
  try {
    const { data } = await apiClient.get<LlmPropertyDetail>(
      `/runs/${date}/llm/${encodeURIComponent(propertyId)}`
    );
    return data;
  } catch (err: any) {
    if (err?.response?.status === 404) return null;
    throw err;
  }
}
