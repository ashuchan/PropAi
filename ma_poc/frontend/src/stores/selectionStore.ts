import { create } from 'zustand';
interface SelectionState { selectedPropertyId: string | null; setSelectedPropertyId: (id: string | null) => void; }
export const useSelectionStore = create<SelectionState>()((set) => ({ selectedPropertyId: null, setSelectedPropertyId: (id) => set({ selectedPropertyId: id }) }));
