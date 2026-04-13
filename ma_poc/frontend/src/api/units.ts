import { apiClient } from './client';
export async function fetchUnitsByProperty(propertyId: string) { const { data } = await apiClient.get(`/properties/${propertyId}`); return data.units || []; }
