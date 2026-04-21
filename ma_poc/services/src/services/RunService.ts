/**
 * @file RunService.ts
 * @description Impl-agnostic run history service. Reads run registry, per-run
 * report, LLM aggregates, and per-property LLM detail through the provider.
 */

import type { IDataProvider } from '../data-provider/contracts.js';
import type { IRunService } from '../interfaces/IRunService.js';
import type {
  RunSummary, RunDetail, LlmRunReport, LlmPropertySummary,
  LlmPropertyDetail, LlmInteraction,
} from '../types/run.js';

type RawLlmBreakdown = Record<string, { calls: number; cost_usd: number; tokens_total: number }>;

interface RawLlmReport {
  generated_at?: string;
  summary?: {
    total_properties_with_llm?: number;
    total_calls?: number;
    successful_calls?: number;
    failed_calls?: number;
    total_tokens_input?: number;
    total_tokens_output?: number;
    total_tokens?: number;
    total_cost_usd?: number;
    by_tier?: RawLlmBreakdown;
    by_provider?: RawLlmBreakdown;
    by_model?: RawLlmBreakdown;
  };
  by_property?: Array<{
    property_id: string;
    calls: number;
    tokens_input: number;
    tokens_output: number;
    tokens_total: number;
    cost_usd: number;
    successful: number;
    failed: number;
  }>;
}

interface RawLlmInteraction {
  tier?: string; call_type?: string; provider?: string; model?: string;
  system_prompt?: string; user_prompt?: string; raw_response?: string;
  tokens_input?: number; tokens_output?: number; tokens_total?: number;
  cost_usd?: number; latency_ms?: number; timestamp?: string;
  success?: boolean; error?: string | null;
}

interface RawLlmPropertyReport {
  property_id?: string; generated_at?: string;
  summary?: {
    total_calls?: number; successful_calls?: number; failed_calls?: number;
    total_tokens_input?: number; total_tokens_output?: number; total_tokens?: number;
    total_cost_usd?: number;
    by_tier?: RawLlmBreakdown; by_provider?: RawLlmBreakdown; by_model?: RawLlmBreakdown;
  };
  interactions?: RawLlmInteraction[];
}

/** Mirror of Python `_safe_filename` in `ma_poc/llm/interaction_logger.py`. */
function safePropertyFilename(propertyId: string, maxLen = 80): string {
  let out = '';
  for (const ch of propertyId) out += /[A-Za-z0-9\-_]/.test(ch) ? ch : '_';
  return out.slice(0, maxLen);
}

export class RunService implements IRunService {
  constructor(private readonly provider: IDataProvider) {}

  async getRunHistory(limit = 30): Promise<RunSummary[]> {
    const dates = await this.provider.runs.listDates();
    const summaries: RunSummary[] = [];
    for (const date of dates.slice(0, limit)) {
      const report = await this.provider.runs.getReport(date);
      if (!report) continue;
      const llm = await this.provider.artifacts.getLlmReport(date);
      summaries.push(this.toSummary(report, llm as RawLlmReport | null));
    }
    return summaries;
  }

  async getRunByDate(date: string): Promise<RunDetail | null> {
    const report = await this.provider.runs.getReport(date);
    if (!report) return null;
    const llm = await this.provider.artifacts.getLlmReport(date);
    return this.toDetail(report, llm as RawLlmReport | null);
  }

  async getLatestRun(): Promise<RunDetail> {
    const date = await this.provider.runs.getLatestDate();
    if (!date) throw new Error('No runs found');
    const report = await this.provider.runs.getReport(date);
    if (!report) throw new Error(`Report not found for ${date}`);
    const llm = await this.provider.artifacts.getLlmReport(date);
    return this.toDetail(report, llm as RawLlmReport | null);
  }

  async getLlmReport(date: string): Promise<LlmRunReport | null> {
    const raw = (await this.provider.artifacts.getLlmReport(date)) as RawLlmReport | null;
    if (!raw) return null;
    const s = raw.summary ?? {};
    const byProperty: LlmPropertySummary[] = (raw.by_property ?? []).map((p) => ({
      propertyId: p.property_id,
      calls: p.calls ?? 0,
      tokensInput: p.tokens_input ?? 0,
      tokensOutput: p.tokens_output ?? 0,
      tokensTotal: p.tokens_total ?? 0,
      costUsd: p.cost_usd ?? 0,
      successful: p.successful ?? 0,
      failed: p.failed ?? 0,
    }));
    return {
      date,
      generatedAt: raw.generated_at ?? '',
      totalPropertiesWithLlm: s.total_properties_with_llm ?? 0,
      totalCalls: s.total_calls ?? 0,
      successfulCalls: s.successful_calls ?? 0,
      failedCalls: s.failed_calls ?? 0,
      tokensInput: s.total_tokens_input ?? 0,
      tokensOutput: s.total_tokens_output ?? 0,
      tokensTotal: s.total_tokens ?? 0,
      totalCostUsd: s.total_cost_usd ?? 0,
      byTier: this.mapLlmBreakdown(s.by_tier),
      byProvider: this.mapLlmBreakdown(s.by_provider),
      byModel: this.mapLlmBreakdown(s.by_model),
      byProperty,
    };
  }

  async getLlmPropertyDetail(date: string, propertyId: string): Promise<LlmPropertyDetail | null> {
    // Both adapters store the detail under the sanitised filename / property_id,
    // so apply the same sanitiser the Python side uses (interaction_logger.py).
    const safe = safePropertyFilename(propertyId);
    const raw = (await this.provider.artifacts.getLlmPropertyDetail(date, safe)) as RawLlmPropertyReport | null;
    if (!raw) return null;
    const s = raw.summary ?? {};
    const interactions: LlmInteraction[] = (raw.interactions ?? []).map((i) => ({
      tier: i.tier ?? '',
      callType: i.call_type ?? '',
      provider: i.provider ?? '',
      model: i.model ?? '',
      tokensInput: i.tokens_input ?? 0,
      tokensOutput: i.tokens_output ?? 0,
      tokensTotal: i.tokens_total ?? (i.tokens_input ?? 0) + (i.tokens_output ?? 0),
      costUsd: i.cost_usd ?? 0,
      latencyMs: i.latency_ms ?? 0,
      timestamp: i.timestamp ?? '',
      success: Boolean(i.success),
      error: i.error ?? null,
      systemPrompt: i.system_prompt,
      userPrompt: i.user_prompt,
      rawResponse: i.raw_response,
    }));
    return {
      propertyId: raw.property_id ?? propertyId,
      generatedAt: raw.generated_at ?? '',
      totalCalls: s.total_calls ?? interactions.length,
      successfulCalls: s.successful_calls ?? 0,
      failedCalls: s.failed_calls ?? 0,
      tokensInput: s.total_tokens_input ?? 0,
      tokensOutput: s.total_tokens_output ?? 0,
      tokensTotal: s.total_tokens ?? 0,
      totalCostUsd: s.total_cost_usd ?? 0,
      byTier: this.mapLlmBreakdown(s.by_tier),
      byProvider: this.mapLlmBreakdown(s.by_provider),
      byModel: this.mapLlmBreakdown(s.by_model),
      interactions,
    };
  }

  // ── Private helpers: shape mapping for the two report formats ─────────────

  private toSummary(rIn: Record<string, unknown>, llm: RawLlmReport | null): RunSummary {
    const r = rIn as Record<string, unknown>;
    const totals = (r.totals ?? {}) as Record<string, unknown>;
    // New (Jugnu / simplified) shape — has `properties`/`succeeded`/`failed`.
    if ('properties' in totals) {
      const total = Number(totals.properties ?? 0);
      const succeeded = Number(totals.succeeded ?? 0);
      const cost = (r.cost ?? {}) as Record<string, unknown>;
      const llmCost = Object.values(cost).reduce<number>((a, b) => a + (Number(b) || 0), 0);
      return {
        date: String(r.run_date ?? ''),
        startedAt: '',
        finishedAt: '',
        durationSeconds: 0,
        exitStatus: '',
        totalProperties: total,
        succeeded,
        failed: Number(totals.failed ?? 0),
        successRate: total > 0 ? succeeded / total : 0,
        unitsExtracted: 0,
        llmCostUsd: llmCost,
        llmCallCount: llm?.summary?.total_calls ?? 0,
        llmTokensTotal: llm?.summary?.total_tokens ?? 0,
      };
    }
    // Legacy shape — rows_processed / rows_succeeded.
    const total = Number(totals.rows_processed ?? totals.csv_rows_total ?? 0);
    const succeeded = Number(totals.rows_succeeded ?? 0);
    const stateDiff = (r.state_diff ?? {}) as Record<string, unknown>;
    return {
      date: String(r.run_date ?? ''),
      startedAt: String(r.started_at ?? ''),
      finishedAt: String(r.finished_at ?? ''),
      durationSeconds: Number(r.duration_s ?? 0),
      exitStatus: String(r.exit_status ?? ''),
      totalProperties: total,
      succeeded,
      failed: Number(totals.rows_failed ?? 0),
      successRate: total > 0 ? succeeded / total : 0,
      unitsExtracted: Number(stateDiff.units_extracted ?? 0),
      llmCostUsd: llm?.summary?.total_cost_usd ?? 0,
      llmCallCount: llm?.summary?.total_calls ?? 0,
      llmTokensTotal: llm?.summary?.total_tokens ?? 0,
    };
  }

  private mapLlmBreakdown(raw: RawLlmBreakdown | undefined) {
    const out: Record<string, { calls: number; costUsd: number; tokensTotal: number }> = {};
    if (!raw) return out;
    for (const [k, v] of Object.entries(raw)) {
      out[k] = { calls: v.calls, costUsd: v.cost_usd, tokensTotal: v.tokens_total };
    }
    return out;
  }

  private toDetail(rIn: Record<string, unknown>, llm: RawLlmReport | null): RunDetail {
    const r = rIn as Record<string, unknown>;
    const totals = (r.totals ?? {}) as Record<string, unknown>;
    const issues = (r.issues ?? {}) as Record<string, unknown>;
    const stateDiff = (r.state_diff ?? {}) as Record<string, unknown>;
    return {
      ...this.toSummary(rIn, llm),
      llmBreakdown: {
        successfulCalls: llm?.summary?.successful_calls ?? 0,
        failedCalls: llm?.summary?.failed_calls ?? 0,
        tokensInput: llm?.summary?.total_tokens_input ?? 0,
        tokensOutput: llm?.summary?.total_tokens_output ?? 0,
        propertiesWithLlm: llm?.summary?.total_properties_with_llm ?? 0,
        byTier: this.mapLlmBreakdown(llm?.summary?.by_tier),
        byProvider: this.mapLlmBreakdown(llm?.summary?.by_provider),
        byModel: this.mapLlmBreakdown(llm?.summary?.by_model),
      },
      retryMode: String(r.retry_mode ?? ''),
      csvPath: String(r.csv_path ?? ''),
      totals: {
        csvRowsTotal: Number(totals.csv_rows_total ?? totals.csv_rows ?? 0),
        rowsEligible: Number(totals.rows_eligible ?? 0),
        rowsProcessed: Number(totals.rows_processed ?? 0),
        rowsSucceeded: Number(totals.rows_succeeded ?? 0),
        rowsFailed: Number(totals.rows_failed ?? 0),
        propertiesInOutput: Number(totals.properties_in_output ?? 0),
      },
      ledgerAfterRetry: (r.ledger_after_retry ?? {}) as Record<string, number>,
      issues: {
        total: Number(issues.total ?? 0),
        bySeverity: (issues.by_severity ?? {}) as Record<string, number>,
        byCode: (issues.by_code ?? {}) as Record<string, number>,
      },
      stateDiff: {
        carryForwardCount: Number(stateDiff.carry_forward_count ?? 0),
        unitsExtracted: Number(stateDiff.units_extracted ?? 0),
        unitsNew: Number(stateDiff.units_new ?? 0),
        unitsUpdated: Number(stateDiff.units_updated ?? 0),
        unitsUnchanged: Number(stateDiff.units_unchanged ?? 0),
        unitsDisappeared: Number(stateDiff.units_disappeared ?? 0),
        unitsCarriedForward: Number(stateDiff.units_carried_forward ?? 0),
      },
      failedProperties: ((r.failed_properties ?? []) as Array<Record<string, unknown>>).map((fp) => ({
        rowIndex: Number(fp.row_index ?? 0),
        canonicalId: String(fp.canonical_id ?? ''),
        reason: String(fp.reason ?? ''),
      })),
    };
  }
}
