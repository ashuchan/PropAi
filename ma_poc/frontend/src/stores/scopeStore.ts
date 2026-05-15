import { create } from 'zustand';
import { persist } from 'zustand/middleware';

export type DataScope = 'today' | 'all';

interface ScopeState {
  scope: DataScope;
  setScope: (scope: DataScope) => void;
}

/**
 * Global freshness scope applied to every property/unit query.
 * `today` = units last seen in the most recent run (default).
 * `all`   = every unit on record across all runs.
 * Persisted in localStorage under `ma-data-scope`.
 */
export const useScopeStore = create<ScopeState>()(
  persist(
    (set) => ({
      scope: 'today',
      setScope: (scope) => set({ scope }),
    }),
    { name: 'ma-data-scope' },
  ),
);
