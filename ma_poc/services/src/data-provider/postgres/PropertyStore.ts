/**
 * @file PropertyStore.ts (postgres adapter)
 * @description Reads per-run property payloads from the `property_snapshots`
 * table. Rows are preserved in `ordinal` order, matching the original
 * properties.json sequence.
 */

import type { PgPool } from './client.js';
import type { IPropertyStore, PropertySummaryRow, RawRunProperty } from '../contracts.js';

interface SummaryDbRow {
  canonical_id: string;
  apartment_id: number | null;
  proj_name: string | null;
  address: string | null;
  city: string | null;
  state: string | null;
  zip_code: string | null;
  pmc: string | null;
  phone: string | null;
  website: string | null;
  total_units: string | number | null;     // pg returns count() as string
  available_units: string | number | null;
  avg_rent: string | number | null;
  median_rent: string | number | null;
  last_seen_at: string | null;
  last_scrape_status: string | null;
  ledger_status: string | null;
  extraction_tier: string | null;
  concessions: string | null;
}

export class PgPropertyStore implements IPropertyStore {
  constructor(private readonly pool: PgPool) {}

  async listForRun(runDate: string): Promise<RawRunProperty[]> {
    const { rows } = await this.pool.query<{ payload: RawRunProperty }>(
      `select payload from property_snapshots
         where run_date = $1
         order by ordinal asc`,
      [runDate],
    );
    return rows.map((r) => r.payload);
  }

  async getForRun(runDate: string, canonicalId: string): Promise<RawRunProperty | null> {
    const { rows } = await this.pool.query<{ payload: RawRunProperty }>(
      `select payload from property_snapshots
         where run_date = $1 and canonical_id = $2
         limit 1`,
      [runDate, canonicalId],
    );
    return rows[0]?.payload ?? null;
  }

  /**
   * Single-round-trip projection over `properties ⨝ aggregated units ⨝
   * latest run_ledger`. Replaces the legacy "load all 4482 JSONB snapshot
   * payloads, sum nested arrays in JS" path that was taking ~16s cold and
   * timing out under parallel load.
   *
   * Why query everything at once instead of paginating in SQL: the service
   * layer already wraps this in a 60s in-process cache and applies
   * filter/sort/page in JS. Pushing pagination into SQL would invalidate
   * the cache for every page change. ~5K-row aggregate is small enough
   * that fetching once is cheaper than re-querying per page.
   */
  async listSummariesFast(): Promise<PropertySummaryRow[]> {
    const { rows } = await this.pool.query<SummaryDbRow>(`
      with unit_agg as (
        select
          canonical_id,
          count(*)::int as total_units,
          count(*) filter (
            where available_date is not null and available_date <> ''
          )::int as available_units,
          avg(
            case
              when coalesce(rent_low, 0) > 0 and coalesce(rent_high, 0) > 0
                then (rent_low + rent_high) / 2.0
              when coalesce(rent_low, 0) > 0 then rent_low
              when coalesce(rent_high, 0) > 0 then rent_high
              else null
            end
          ) as avg_rent,
          percentile_cont(0.5) within group (order by
            case
              when coalesce(rent_low, 0) > 0 and coalesce(rent_high, 0) > 0
                then (rent_low + rent_high) / 2.0
              when coalesce(rent_low, 0) > 0 then rent_low
              when coalesce(rent_high, 0) > 0 then rent_high
              else null
            end
          ) as median_rent
        from units
        group by canonical_id
      ),
      latest_run as (
        select run_date from runs order by run_date desc limit 1
      ),
      ledger_latest as (
        select canonical_id, status, extra
        from run_ledger
        where run_date = (select run_date from latest_run)
      )
      select
        p.canonical_id,
        p.apartment_id,
        p.proj_name,
        p.address, p.city, p.state, p.zip_code,
        p.pmc, p.phone, p.website,
        coalesce(u.total_units, 0) as total_units,
        coalesce(u.available_units, 0) as available_units,
        coalesce(u.avg_rent, 0) as avg_rent,
        coalesce(u.median_rent, 0) as median_rent,
        p.last_seen_at,
        p.last_scrape_status,
        l.status as ledger_status,
        (l.extra->>'extraction_tier') as extraction_tier,
        p.concessions
      from properties p
      left join unit_agg u  on u.canonical_id = p.canonical_id
      left join ledger_latest l on l.canonical_id = p.canonical_id
    `);

    return rows.map((r) => ({
      canonicalId: r.canonical_id,
      apartmentId: r.apartment_id,
      name: r.proj_name ?? '',
      address: r.address ?? '',
      city: r.city ?? '',
      state: r.state ?? '',
      zip: r.zip_code ?? '',
      pmc: r.pmc ?? '',
      phone: r.phone ?? '',
      website: r.website ?? '',
      totalUnits: Number(r.total_units ?? 0),
      availableUnits: Number(r.available_units ?? 0),
      avgRent: Number(r.avg_rent ?? 0),
      medianRent: Number(r.median_rent ?? 0),
      lastSeenAt: r.last_seen_at,
      lastScrapeStatus: r.last_scrape_status,
      ledgerStatus: r.ledger_status,
      extractionTier: r.extraction_tier,
      concessions: r.concessions,
    }));
  }
}
