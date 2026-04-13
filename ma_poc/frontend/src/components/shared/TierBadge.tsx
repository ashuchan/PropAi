import { clsx } from 'clsx';
type ExtractionTier = 'TIER_1_API' | 'TIER_2_JSONLD' | 'TIER_3_DOM' | 'TIER_4_LLM' | 'TIER_5_VISION' | 'FAILED';
const TIER_STYLES: Record<ExtractionTier, { bg: string; text: string; label: string }> = {
  TIER_1_API: { bg: 'bg-emerald-50 dark:bg-emerald-950', text: 'text-emerald-800 dark:text-emerald-200', label: 'API' },
  TIER_2_JSONLD: { bg: 'bg-blue-50 dark:bg-blue-950', text: 'text-blue-800 dark:text-blue-200', label: 'JSON-LD' },
  TIER_3_DOM: { bg: 'bg-violet-50 dark:bg-violet-950', text: 'text-violet-800 dark:text-violet-200', label: 'DOM' },
  TIER_4_LLM: { bg: 'bg-amber-50 dark:bg-amber-950', text: 'text-amber-800 dark:text-amber-200', label: 'LLM' },
  TIER_5_VISION: { bg: 'bg-orange-50 dark:bg-orange-950', text: 'text-orange-800 dark:text-orange-200', label: 'Vision' },
  FAILED: { bg: 'bg-red-50 dark:bg-red-950', text: 'text-red-800 dark:text-red-200', label: 'Failed' },
};
interface TierBadgeProps { tier: ExtractionTier | string; size?: 'sm' | 'md'; }
export function TierBadge({ tier, size = 'sm' }: TierBadgeProps) {
  const style = TIER_STYLES[tier as ExtractionTier] || TIER_STYLES.FAILED;
  return <span className={clsx('inline-flex items-center rounded-full font-medium', style.bg, style.text, size === 'sm' ? 'px-2 py-0.5 text-[10px]' : 'px-2.5 py-1 text-[11px]')} data-testid={`tier-badge-${tier}`}>{style.label}</span>;
}
