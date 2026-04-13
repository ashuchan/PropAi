import { create } from 'zustand';
import { persist } from 'zustand/middleware';
import type { ViewMode } from '@/types/views';
interface ViewState { activeView: ViewMode; setActiveView: (view: ViewMode) => void; }
export const useViewStore = create<ViewState>()(persist((set) => ({ activeView: 'editorial', setActiveView: (view) => set({ activeView: view }) }), { name: 'ma-view-mode' }));
