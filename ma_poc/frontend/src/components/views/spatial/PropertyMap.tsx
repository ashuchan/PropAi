import { MapContainer, TileLayer, CircleMarker, Popup } from 'react-leaflet';
import { MapPopup } from './MapPopup';
import { TIER_CHART_COLORS } from '@/utils/colors';
import 'leaflet/dist/leaflet.css';
import type { ApiPropertySummary } from '@/api/properties';

export function PropertyMap({ properties }: { properties: ApiPropertySummary[] }) {
  const valid = properties.filter(p => p.latitude && p.longitude);
  const center = valid.length > 0 ? { lat: valid.reduce((s, p) => s + p.latitude, 0) / valid.length, lng: valid.reduce((s, p) => s + p.longitude, 0) / valid.length } : { lat: 39.8283, lng: -98.5795 };
  return (
    <MapContainer center={[center.lat, center.lng]} zoom={valid.length > 0 ? 10 : 4} className="h-full w-full" data-testid="map-container">
      <TileLayer attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>' url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png" />
      {valid.map((p) => <CircleMarker key={p.id} center={[p.latitude, p.longitude]} radius={Math.max(6, Math.min(20, 6 + Math.sqrt(p.totalUnits)))} pathOptions={{ color: TIER_CHART_COLORS[p.extractionTier] || '#868E96', fillColor: TIER_CHART_COLORS[p.extractionTier] || '#868E96', fillOpacity: p.scrapeStatus === 'FAILED' ? 0.3 : 0.7, weight: 2 }}><Popup><MapPopup property={p} /></Popup></CircleMarker>)}
    </MapContainer>
  );
}
