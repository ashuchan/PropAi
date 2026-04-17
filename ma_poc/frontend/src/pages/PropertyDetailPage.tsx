import { useParams } from 'react-router-dom';
import { FileText, Settings2, Image } from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { usePropertyDetail, usePropertyReport, usePropertyProfile } from '@/hooks/usePropertyDetail';
import { Breadcrumb } from '@/components/layout/Breadcrumb';
import { MetricCard } from '@/components/shared/MetricCard';
import { TierBadge } from '@/components/shared/TierBadge';
import { ConcessionTag } from '@/components/shared/ConcessionTag';
import { PropertyImage } from '@/components/shared/PropertyImage';
import { LoadingSkeleton } from '@/components/shared/LoadingSkeleton';
import { EmptyState } from '@/components/shared/EmptyState';
import { ErrorBoundary } from '@/components/shared/ErrorBoundary';
import { StatusDot } from '@/components/shared/StatusDot';
import { CollapsibleSection, JsonViewer } from '@/components/shared/CollapsibleSection';
import { formatCurrency, formatNumber, formatPercent, formatCostUsd, formatDate } from '@/utils/formatters';
import type { ApiPropertyDetail, ApiUnit } from '@/api/properties';

export function PropertyDetailPage() {
  const { id } = useParams<{ id: string }>();
  const { data: property, isLoading } = usePropertyDetail(id);
  const { data: report, isLoading: reportLoading } = usePropertyReport(id);
  const { data: profile, isLoading: profileLoading } = usePropertyProfile(id);
  if (isLoading) return <div className="space-y-6"><LoadingSkeleton variant="text-block" /><LoadingSkeleton variant="metric" count={5} /><LoadingSkeleton variant="card" count={2} /></div>;
  if (!property) return <EmptyState title="Property not found" description="This property could not be loaded." />;
  const units: ApiUnit[] = property.units || [];
  const isV2 = property.schemaVersion === 'v2';
  return (
    <ErrorBoundary><div className="space-y-6">
      <Breadcrumb items={[{ label: 'Explore', to: '/' }, { label: property.name }]} />
      <div className="grid grid-cols-1 gap-6 lg:grid-cols-3">
        <div className="lg:col-span-1"><PropertyImage imageUrl={property.imageUrl} propertyId={property.id} stories={property.stories} totalUnits={property.totalUnits} className="h-[200px] w-full rounded-xl" /></div>
        <div className="lg:col-span-2">
          <div className="flex items-start justify-between gap-3">
            <div>
              <h1 className="font-display text-[22px] text-slate-900 dark:text-slate-100">{property.name}</h1>
              <p className="mt-1 text-[13px] text-slate-500 dark:text-slate-400">{property.address}, {property.city}, {property.state} {property.zip}</p>
              {property.managementCompany && <p className="mt-0.5 text-[12px] text-slate-400 dark:text-slate-500">{property.managementCompany}</p>}
            </div>
            <div className="flex items-center gap-2">
              {isV2 && <span className="rounded bg-violet-50 px-1.5 py-0.5 text-[10px] font-medium text-violet-700 dark:bg-violet-950 dark:text-violet-300">V2</span>}
              <TierBadge tier={property.extractionTier} size="md" />
              <StatusDot status={property.scrapeStatus === 'SUCCESS' ? 'available' : property.scrapeStatus === 'FAILED' ? 'failed' : 'unknown'} label={property.scrapeStatus} />
            </div>
          </div>
          {property.activeConcession && <div className="mt-3"><ConcessionTag text={property.activeConcession} /></div>}
          <div className="mt-4 grid grid-cols-2 gap-2 text-[12px] text-slate-600 dark:text-slate-400">
            {!isV2 && <div>Year Built: <span className="font-mono text-slate-900 dark:text-slate-100">{property.yearBuilt || '—'}</span></div>}
            {!isV2 && <div>Stories: <span className="font-mono text-slate-900 dark:text-slate-100">{property.stories || '—'}</span></div>}
            <div>Type: <span className="text-slate-900 dark:text-slate-100">{property.propertyStatus}</span></div>
            <div>Website: {property.websiteUrl ? <a href={property.websiteUrl} target="_blank" rel="noopener noreferrer" className="text-rent-400 hover:underline">Visit</a> : '—'}</div>
            {isV2 && property.websiteDesign && <div>Platform: <span className="text-slate-900 dark:text-slate-100">{property.websiteDesign}</span></div>}
            {isV2 && property.emailAddress && <div>Email: <span className="font-mono text-slate-900 dark:text-slate-100">{property.emailAddress}</span></div>}
            {isV2 && property.phone && <div>Phone: <span className="font-mono text-slate-900 dark:text-slate-100">{property.phone}</span></div>}
            {property.unitMix && <div>Unit Mix: <span className="text-slate-900 dark:text-slate-100">{property.unitMix}</span></div>}
          </div>
        </div>
      </div>
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-5"><MetricCard label="Min Rent" value={formatCurrency(property.marketMetrics?.minRent)} /><MetricCard label="Max Rent" value={formatCurrency(property.marketMetrics?.maxRent)} /><MetricCard label="Median Rent" value={formatCurrency(property.medianAskingRent)} /><MetricCard label="Total Units" value={formatNumber(property.totalUnits)} /><MetricCard label="Availability" value={formatPercent(property.availabilityRate)} /></div>

      {/* Property Media Section */}
      {property.media && (property.media.floorPlanImages?.length > 0 || property.media.galleryUrls?.length > 0) && (
        <CollapsibleSection title="Property Media" subtitle={`${property.media.floorPlanImages?.length || 0} floor plans · ${property.media.galleryUrls?.length || 0} gallery images`} icon={<Image size={16} className="text-rent-400" />} defaultOpen={false}>
          {property.media.galleryUrls?.length > 0 && <div className="mb-4"><h4 className="mb-2 text-[12px] font-medium uppercase tracking-wide text-slate-500">Gallery</h4><div className="flex gap-2 overflow-auto">{property.media.galleryUrls.map((url: string, i: number) => <img key={i} src={url} alt={`Gallery ${i + 1}`} loading="lazy" className="h-24 w-36 rounded-lg border border-slate-200 object-cover dark:border-slate-700" />)}</div></div>}
          {property.media.floorPlanImages?.length > 0 && <div><h4 className="mb-2 text-[12px] font-medium uppercase tracking-wide text-slate-500">Floor Plan Images</h4><div className="grid grid-cols-2 gap-3 lg:grid-cols-4">{property.media.floorPlanImages.map((fp: any, i: number) => <div key={i} className="rounded-lg border border-slate-200 p-2 dark:border-slate-700"><img src={fp.imageUrl} alt={fp.floorPlanName} loading="lazy" className="h-28 w-full rounded object-contain bg-white dark:bg-slate-800" /><p className="mt-1 text-center text-[11px] text-slate-500">{fp.floorPlanName} ({fp.unitIds?.length || 0} units)</p></div>)}</div></div>}
        </CollapsibleSection>
      )}

      <div className="rounded-xl border border-slate-200 bg-white p-5 dark:border-slate-700 dark:bg-slate-900">
        <h2 className="mb-3 text-[16px] font-medium text-slate-900 dark:text-slate-100">LLM Usage & Cost</h2>
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          <MetricCard label="Total Cost" value={formatCostUsd(property.llmCostUsd)} accentColor={property.llmCallCount > 0 ? '#EF9F27' : undefined} />
          <MetricCard label="API Calls" value={formatNumber(property.llmCallCount)} />
          <MetricCard label="Total Tokens" value={formatNumber(property.llmTokensTotal)} />
          <MetricCard label="Avg $/Call" value={property.llmCallCount > 0 ? formatCostUsd(property.llmCostUsd / property.llmCallCount) : '—'} />
        </div>
        {property.llmCallCount === 0 && <p className="mt-3 text-[12px] text-slate-500">No LLM calls were needed to extract this property&apos;s data.</p>}
      </div>
      <PropertyReportSection report={report} loading={reportLoading} />
      <PropertyProfileSection profile={profile} loading={profileLoading} />
      {units.length > 0 && <UnitsSection units={units} isV2={isV2} />}
    </div></ErrorBoundary>
  );
}

function UnitsSection({ units, isV2 }: { units: ApiUnit[]; isV2: boolean }) {
  return (
    <div className="rounded-xl border border-slate-200 bg-white p-5 dark:border-slate-700 dark:bg-slate-900">
      <h2 className="mb-4 text-[16px] font-medium text-slate-900 dark:text-slate-100">Units ({units.length})</h2>
      <div className="overflow-auto">
        <table className="w-full text-[12px]">
          <thead><tr className="border-b border-slate-200 dark:border-slate-700">
            {!isV2 && <th className="px-3 py-2 text-left font-medium uppercase tracking-wide text-slate-500">Floor Plan</th>}
            <th className="px-3 py-2 text-left font-medium uppercase tracking-wide text-slate-500">Unit</th>
            {isV2 && <th className="px-3 py-2 text-left font-medium uppercase tracking-wide text-slate-500">Floor Plan</th>}
            {isV2 && <th className="px-3 py-2 text-left font-medium uppercase tracking-wide text-slate-500">Beds</th>}
            {isV2 && <th className="px-3 py-2 text-left font-medium uppercase tracking-wide text-slate-500">Baths</th>}
            {isV2 && <th className="px-3 py-2 text-left font-medium uppercase tracking-wide text-slate-500">Area</th>}
            <th className="px-3 py-2 text-left font-medium uppercase tracking-wide text-slate-500">Rent Range</th>
            <th className="px-3 py-2 text-left font-medium uppercase tracking-wide text-slate-500">Status</th>
            <th className="px-3 py-2 text-left font-medium uppercase tracking-wide text-slate-500">Available Date</th>
            {isV2 && <th className="px-3 py-2 text-left font-medium uppercase tracking-wide text-slate-500">Lease Term</th>}
            {!isV2 && <th className="px-3 py-2 text-left font-medium uppercase tracking-wide text-slate-500">Concessions</th>}
          </tr></thead>
          <tbody>
            {units.map((unit, i) => (
              <tr key={unit.unitId || i} className={i % 2 === 1 ? 'bg-slate-50/50 dark:bg-slate-800/25' : ''}>
                {!isV2 && <td className="px-3 py-2">{unit.floorplanImageUrl ? <a href={unit.floorplanImageUrl} target="_blank" rel="noopener noreferrer"><img src={unit.floorplanImageUrl} alt={`Floor plan ${unit.unitId}`} loading="lazy" className="h-14 w-20 rounded border border-slate-200 object-contain bg-white dark:border-slate-700" /></a> : <span className="text-slate-400">—</span>}</td>}
                <td className="px-3 py-2 font-mono text-slate-900 dark:text-slate-100">{unit.unitId || '—'}</td>
                {isV2 && <td className="px-3 py-2 text-slate-700 dark:text-slate-300">{unit.floorPlanName || unit.floorPlanType || '—'}</td>}
                {isV2 && <td className="px-3 py-2 font-mono text-slate-900 dark:text-slate-100">{unit.beds != null ? (unit.beds === 0 ? 'Studio' : unit.beds) : '—'}</td>}
                {isV2 && <td className="px-3 py-2 font-mono text-slate-900 dark:text-slate-100">{unit.baths != null ? unit.baths : '—'}</td>}
                {isV2 && <td className="px-3 py-2 font-mono text-slate-600 dark:text-slate-400">{unit.area != null && unit.area > 0 ? `${unit.area} sf` : '—'}</td>}
                <td className="px-3 py-2 font-mono text-slate-900 dark:text-slate-100">{formatCurrency(unit.marketRentLow)} – {formatCurrency(unit.marketRentHigh)}</td>
                <td className="px-3 py-2"><StatusDot status={unit.availabilityStatus === 'AVAILABLE' ? 'available' : 'unknown'} label={unit.availabilityStatus} /></td>
                <td className="px-3 py-2 text-slate-500">{unit.availableDate || '—'}</td>
                {isV2 && <td className="px-3 py-2 font-mono text-slate-600 dark:text-slate-400">{unit.leaseTerm ? `${unit.leaseTerm} mo` : '—'}</td>}
                {!isV2 && <td className="px-3 py-2 text-slate-500">{unit.concessions || '—'}</td>}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function PropertyReportSection({ report, loading }: { report: any; loading: boolean }) {
  const subtitle = report?.runDate ? `Run ${formatDate(report.runDate, 'short')} · property_reports/${report.propertyId}.md` : 'Per-property markdown report';
  return (
    <CollapsibleSection
      title="Property Scrape Report"
      subtitle={subtitle}
      icon={<FileText size={16} className="text-rent-400" />}
      defaultOpen={false}
      data-testid="property-report-section"
    >
      {loading && <LoadingSkeleton variant="text-block" />}
      {!loading && !report && <p className="text-[12px] text-slate-500">No markdown report was generated for this property. Check <span className="font-mono">data/runs/&#123;date&#125;/property_reports/&#123;id&#125;.md</span>.</p>}
      {!loading && report && (
        <div className="space-y-3">
          <MarkdownReport markdown={report.markdown} />
          <p className="text-[10px] font-mono text-slate-400">{report.filePath}</p>
        </div>
      )}
    </CollapsibleSection>
  );
}

function MarkdownReport({ markdown }: { markdown: string }) {
  return (
    <article className="prose-report max-h-[720px] overflow-auto rounded-lg border border-slate-200 bg-white p-4 text-[13px] leading-relaxed text-slate-800 dark:border-slate-700 dark:bg-slate-950 dark:text-slate-200">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          h1: (p) => <h1 className="mt-0 font-display text-[20px] text-slate-900 dark:text-slate-100" {...p} />,
          h2: (p) => <h2 className="mt-6 mb-2 border-b border-slate-200 pb-1 text-[16px] font-medium text-slate-900 dark:border-slate-700 dark:text-slate-100" {...p} />,
          h3: (p) => <h3 className="mt-4 mb-2 text-[14px] font-medium text-slate-900 dark:text-slate-100" {...p} />,
          h4: (p) => <h4 className="mt-3 mb-1 text-[12px] font-medium uppercase tracking-wide text-slate-500" {...p} />,
          p: (p) => <p className="my-2 text-[13px] text-slate-700 dark:text-slate-300" {...p} />,
          strong: (p) => <strong className="font-medium text-slate-900 dark:text-slate-100" {...p} />,
          code: (p) => <code className="rounded bg-slate-100 px-1 py-0.5 font-mono text-[11px] text-slate-800 dark:bg-slate-800 dark:text-slate-200" {...p} />,
          pre: (p) => <pre className="my-3 overflow-auto rounded-lg bg-slate-50 p-3 font-mono text-[11px] dark:bg-slate-900" {...p} />,
          a: (p) => <a className="break-all text-rent-400 hover:underline" target="_blank" rel="noopener noreferrer" {...p} />,
          ul: (p) => <ul className="my-2 ml-5 list-disc space-y-1 text-[13px]" {...p} />,
          ol: (p) => <ol className="my-2 ml-5 list-decimal space-y-1 text-[13px]" {...p} />,
          li: (p) => <li className="text-slate-700 dark:text-slate-300" {...p} />,
          table: (p) => <div className="my-3 overflow-auto rounded-lg border border-slate-200 dark:border-slate-700"><table className="w-full text-[12px]" {...p} /></div>,
          thead: (p) => <thead className="bg-slate-50 dark:bg-slate-800/50" {...p} />,
          th: (p) => <th className="border-b border-slate-200 px-3 py-2 text-left font-medium uppercase tracking-wide text-slate-500 dark:border-slate-700" {...p} />,
          td: (p) => <td className="border-b border-slate-100 px-3 py-1.5 align-top text-slate-700 last:border-0 dark:border-slate-800 dark:text-slate-300" {...p} />,
          hr: (p) => <hr className="my-4 border-slate-200 dark:border-slate-700" {...p} />,
          blockquote: (p) => <blockquote className="my-3 border-l-4 border-rent-400 bg-rent-50/40 px-3 py-2 text-[12px] dark:bg-rent-900/10" {...p} />,
        }}
      >
        {markdown}
      </ReactMarkdown>
    </article>
  );
}

function PropertyProfileSection({ profile, loading }: { profile: any; loading: boolean }) {
  const data = profile?.data || null;
  const subtitle = data ? `v${data.version ?? '?'} · ${data.updated_by ?? 'unknown'} · ${data.confidence?.maturity ?? 'COLD'}` : 'No profile';
  return (
    <CollapsibleSection
      title="Scrape Profile"
      subtitle={subtitle}
      icon={<Settings2 size={16} className="text-violet-500" />}
      defaultOpen={false}
      data-testid="property-profile-section"
    >
      {loading && <LoadingSkeleton variant="text-block" />}
      {!loading && !profile && <p className="text-[12px] text-slate-500">No scrape profile exists for this property yet. A profile is bootstrapped on first run.</p>}
      {!loading && profile && data && (
        <div className="space-y-4">
          <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
            <MetricCard label="Maturity" value={data.confidence?.maturity ?? '—'} />
            <MetricCard label="Preferred Tier" value={data.confidence?.preferred_tier ?? '—'} />
            <MetricCard label="Successes" value={formatNumber(data.confidence?.consecutive_successes)} />
            <MetricCard label="Failures" value={formatNumber(data.confidence?.consecutive_failures)} />
          </div>
          {data.navigation?.winning_page_url && (
            <div className="text-[12px]">
              <span className="text-slate-500">Winning URL: </span>
              <a href={data.navigation.winning_page_url} target="_blank" rel="noopener noreferrer" className="break-all font-mono text-rent-400 hover:underline">{data.navigation.winning_page_url}</a>
            </div>
          )}
          {data.api_hints?.api_provider && (
            <div className="text-[12px]"><span className="text-slate-500">API Provider: </span><span className="font-mono text-slate-900 dark:text-slate-100">{data.api_hints.api_provider}</span></div>
          )}
          <details className="rounded-lg bg-slate-50 dark:bg-slate-800/50" open>
            <summary className="cursor-pointer px-3 py-2 text-[11px] font-medium uppercase tracking-wide text-slate-500">Full Profile JSON</summary>
            <div className="p-3"><JsonViewer value={data} /></div>
          </details>
          <p className="text-[10px] font-mono text-slate-400">{profile.filePath}</p>
        </div>
      )}
    </CollapsibleSection>
  );
}
