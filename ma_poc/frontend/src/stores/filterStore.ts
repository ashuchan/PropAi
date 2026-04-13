import { create } from 'zustand';
import type { ExtractionTier, ScrapeStatus, PropertyStatus } from '@/types/views';
import { toggleArrayItem } from '@/utils/filtering';

interface FilterState {
  search: string; cities: string[]; tiers: ExtractionTier[]; statuses: ScrapeStatus[];
  propertyStatuses: PropertyStatus[]; sortField: string; sortDirection: 'asc' | 'desc';
  page: number; pageSize: number;
  setSearch: (search: string) => void; toggleCity: (city: string) => void;
  toggleTier: (tier: ExtractionTier) => void; toggleStatus: (status: ScrapeStatus) => void;
  togglePropertyStatus: (status: PropertyStatus) => void;
  setSort: (field: string, direction: 'asc' | 'desc') => void; setPage: (page: number) => void; resetAll: () => void;
}
const initial = { search: '', cities: [] as string[], tiers: [] as ExtractionTier[], statuses: [] as ScrapeStatus[], propertyStatuses: [] as PropertyStatus[], sortField: 'name', sortDirection: 'asc' as const, page: 1, pageSize: 25 };
export const useFilterStore = create<FilterState>()((set) => ({
  ...initial,
  setSearch: (search) => set({ search, page: 1 }),
  toggleCity: (city) => set((s) => ({ cities: toggleArrayItem(s.cities, city), page: 1 })),
  toggleTier: (tier) => set((s) => ({ tiers: toggleArrayItem(s.tiers, tier), page: 1 })),
  toggleStatus: (status) => set((s) => ({ statuses: toggleArrayItem(s.statuses, status), page: 1 })),
  togglePropertyStatus: (status) => set((s) => ({ propertyStatuses: toggleArrayItem(s.propertyStatuses, status), page: 1 })),
  setSort: (field, direction) => set({ sortField: field, sortDirection: direction }),
  setPage: (page) => set({ page }),
  resetAll: () => set(initial),
}));
