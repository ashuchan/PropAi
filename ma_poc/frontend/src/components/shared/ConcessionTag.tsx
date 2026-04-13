import { Star } from 'lucide-react';
interface ConcessionTagProps { text: string; }
export function ConcessionTag({ text }: ConcessionTagProps) {
  return <span className="inline-flex items-center gap-1 rounded-full bg-amber-50 px-2.5 py-0.5 text-[11px] font-medium text-amber-800 dark:bg-amber-950 dark:text-amber-200" data-testid="concession-tag"><Star size={10} className="fill-current" />{text}</span>;
}
