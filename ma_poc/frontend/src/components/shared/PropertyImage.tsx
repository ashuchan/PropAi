import { useMemo } from 'react';
interface PropertyImageProps { imageUrl?: string | null; propertyId: string; stories?: number | null; totalUnits?: number; className?: string; }
function hashCode(str: string): number { let hash = 0; for (let i = 0; i < str.length; i++) { hash = ((hash << 5) - hash) + str.charCodeAt(i); hash |= 0; } return Math.abs(hash); }
function idToColor(id: string): string { return `hsl(${hashCode(id) % 360}, 45%, 55%)`; }
export function PropertyImage({ imageUrl, propertyId, stories, totalUnits = 10, className = '' }: PropertyImageProps) {
  const svgContent = useMemo(() => {
    if (imageUrl) return null;
    const color = idToColor(propertyId); const hash = hashCode(propertyId);
    const bStories = stories || Math.max(3, (hash % 8) + 3);
    const wPerFloor = Math.min(6, Math.max(2, Math.ceil(totalUnits / bStories)));
    const bH = bStories * 24; const bW = wPerFloor * 28 + 20;
    const svgH = bH + 30; const svgW = Math.max(bW + 40, 160);
    const sX = (svgW - bW) / 2; const sY = svgH - bH - 10;
    let windows = '';
    for (let f = 0; f < bStories; f++) for (let w = 0; w < wPerFloor; w++) {
      const x = sX + 10 + w * 28 + 4; const y = sY + f * 24 + 6;
      const lit = (hash + f * 7 + w * 3) % 3 !== 0;
      windows += `<rect x="${x}" y="${y}" width="18" height="14" rx="1" fill="${lit ? '#FCD34D' : '#1E293B'}" opacity="${lit ? 0.85 : 0.5}"/>`;
    }
    return `<svg viewBox="0 0 ${svgW} ${svgH}" xmlns="http://www.w3.org/2000/svg"><rect width="${svgW}" height="${svgH}" fill="#0F172A"/><rect x="${sX}" y="${sY}" width="${bW}" height="${bH + 10}" rx="2" fill="${color}"/><rect x="${sX}" y="${sY - 4}" width="${bW}" height="6" rx="1" fill="${color}" opacity="0.7"/>${windows}<rect x="0" y="${svgH - 8}" width="${svgW}" height="8" fill="#1E293B"/></svg>`;
  }, [imageUrl, propertyId, stories, totalUnits]);
  if (imageUrl) return <div className={`overflow-hidden bg-slate-100 dark:bg-slate-800 ${className}`} data-testid="property-image"><img src={imageUrl} alt="Property" className="h-full w-full object-cover" loading="lazy" /></div>;
  return <div className={`overflow-hidden bg-slate-900 ${className}`} data-testid="property-image" dangerouslySetInnerHTML={{ __html: svgContent || '' }} />;
}
