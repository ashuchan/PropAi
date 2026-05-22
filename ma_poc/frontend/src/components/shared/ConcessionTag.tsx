import { Star } from 'lucide-react';

/**
 * Concession callout pill.
 *
 * Prefers the deterministic ``banner`` produced by the services-layer
 * enricher (``ma_poc/services/src/utils/concession.ts``) — a short
 * structured render like ``"2 months FREE rent · by 5/31/2026 · select
 * units"``. Falls back to the raw concession copy from
 * ``activeConcession`` when the banner is null (the enricher couldn't
 * recognise a structured offer shape — usually marketing prose or a
 * header-only "Limited Time Offer!" snippet).
 *
 * Both legacy callers (``text="..."``) and new callers
 * (``banner=... raw=...``) are supported so the migration can roll out
 * one view at a time without breaking screenshots / e2e.
 */
interface ConcessionTagProps {
  /** Legacy single-string callsite. Kept for back-compat — the new
   *  callsite shape uses ``banner`` + ``raw`` so the fallback is
   *  visible at the call site. */
  text?: string | null;
  /** Short structured banner — prefer this for display. */
  banner?: string | null;
  /** Raw concession copy — used when ``banner`` is null/empty. */
  raw?: string | null;
  /** Full tooltip / title attribute. Defaults to the raw text so the
   *  reviewer can hover to see the underlying producer string when the
   *  banner has trimmed marketing context away. */
  title?: string | null;
}

export function ConcessionTag({ text, banner, raw, title }: ConcessionTagProps) {
  // Display preference: banner → raw → legacy text. All three are
  // optional so callers can adopt the new shape incrementally.
  const display = (banner && banner.trim())
    || (raw && raw.trim())
    || (text && text.trim())
    || '';
  if (!display) return null;
  // Hover tooltip shows the raw producer string so the reviewer can
  // audit what the banner was derived from.
  const tooltip = title ?? raw ?? text ?? display;
  return (
    <span
      className="inline-flex items-center gap-1 rounded-full bg-amber-50 px-2.5 py-0.5 text-[11px] font-medium text-amber-800 dark:bg-amber-950 dark:text-amber-200"
      data-testid="concession-tag"
      title={tooltip || undefined}
    >
      <Star size={10} className="fill-current" />
      {display}
    </span>
  );
}
