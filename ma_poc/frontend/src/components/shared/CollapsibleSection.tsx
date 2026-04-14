import { useState, type ReactNode } from 'react';
import { ChevronRight } from 'lucide-react';
import { clsx } from 'clsx';

interface CollapsibleSectionProps {
  title: ReactNode;
  subtitle?: ReactNode;
  icon?: ReactNode;
  defaultOpen?: boolean;
  accentColor?: string;
  children: ReactNode;
  actions?: ReactNode;
  'data-testid'?: string;
}

/**
 * Generic collapsible panel used for property-detail drill-downs
 * (scrape report, LLM interaction log, scrape profile JSON).
 */
export function CollapsibleSection({
  title, subtitle, icon, defaultOpen = false, accentColor, children, actions, 'data-testid': testId,
}: CollapsibleSectionProps) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <section
      className="overflow-hidden rounded-xl border border-slate-200 bg-white dark:border-slate-700 dark:bg-slate-900"
      data-testid={testId}
      style={accentColor ? { borderLeftColor: accentColor, borderLeftWidth: 3 } : undefined}
    >
      <button
        type="button"
        onClick={() => setOpen(o => !o)}
        className="flex w-full items-center gap-3 px-5 py-3 text-left transition-colors hover:bg-slate-50 dark:hover:bg-slate-800/50"
        aria-expanded={open}
      >
        <ChevronRight
          size={16}
          className={clsx('shrink-0 text-slate-500 transition-transform', open && 'rotate-90')}
        />
        {icon}
        <div className="min-w-0 flex-1">
          <div className="text-[14px] font-medium text-slate-900 dark:text-slate-100">{title}</div>
          {subtitle && <div className="mt-0.5 text-[11px] text-slate-500">{subtitle}</div>}
        </div>
        {actions && <div onClick={(e) => e.stopPropagation()}>{actions}</div>}
      </button>
      {open && (
        <div className="border-t border-slate-200 px-5 py-4 dark:border-slate-700">
          {children}
        </div>
      )}
    </section>
  );
}

/**
 * Pretty-print a JSON value in a monospace code block with syntax colouring.
 * Truncates huge blobs with an explicit note so the UI stays responsive.
 */
export function JsonViewer({ value, maxChars = 120_000 }: { value: unknown; maxChars?: number }) {
  const text = JSON.stringify(value, null, 2);
  const truncated = text.length > maxChars;
  const display = truncated ? text.slice(0, maxChars) + '\n... (truncated)' : text;
  return (
    <pre className="max-h-[480px] overflow-auto rounded-lg bg-slate-50 p-3 font-mono text-[11px] leading-relaxed text-slate-800 dark:bg-slate-950 dark:text-slate-200">
      {display}
    </pre>
  );
}
