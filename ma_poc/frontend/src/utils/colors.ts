export const TIER_STYLES = {
  TIER_1_API: { bg: 'bg-emerald-50 dark:bg-emerald-950', text: 'text-emerald-800 dark:text-emerald-200', label: 'API' },
  TIER_2_JSONLD: { bg: 'bg-blue-50 dark:bg-blue-950', text: 'text-blue-800 dark:text-blue-200', label: 'JSON-LD' },
  TIER_3_DOM: { bg: 'bg-violet-50 dark:bg-violet-950', text: 'text-violet-800 dark:text-violet-200', label: 'DOM' },
  TIER_4_LLM: { bg: 'bg-amber-50 dark:bg-amber-950', text: 'text-amber-800 dark:text-amber-200', label: 'LLM' },
  TIER_5_VISION: { bg: 'bg-orange-50 dark:bg-orange-950', text: 'text-orange-800 dark:text-orange-200', label: 'Vision' },
  FAILED: { bg: 'bg-red-50 dark:bg-red-950', text: 'text-red-800 dark:text-red-200', label: 'Failed' },
} as const;
export const STATUS_COLORS = { available: 'bg-emerald-500', leased: 'bg-slate-400', unknown: 'bg-amber-500', failed: 'bg-red-500' } as const;
export const CHANGE_COLORS = { up: '#E24B4A', down: '#1D9E75', new: '#378ADD', gone: '#868E96' } as const;
export const TIER_CHART_COLORS: Record<string, string> = { TIER_1_API: '#1D9E75', TIER_2_JSONLD: '#378ADD', TIER_3_DOM: '#534AB7', TIER_4_LLM: '#EF9F27', TIER_5_VISION: '#D85A30', FAILED: '#E24B4A' };
