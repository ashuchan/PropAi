/**
 * @file PropertyStateStore.ts (postgres adapter)
 * @description Reads the V2-strict `properties` table. Populates V2 aliases
 * on the returned PropertyStateRecord; V1 aliases remain undefined since
 * the column has been renamed.
 */

import type { IPropertyStateStore, PropertyStateRecord } from '../contracts.js';
import type { PgPool } from './client.js';

interface Row {
  canonical_id: string;
  apartment_id: number | null;
  proj_name: string | null;
  address: string | null;
  city: string | null;
  state: string | null;
  zip_code: string | null;
  country: string | null;
  phone: string | null;
  email_address: string | null;
  website: string | null;
  pmc: string | null;
  website_design: string | null;
  concessions: string | null;
  first_seen_date: string | null;
  last_seen_at: string | null;
  last_scrape_status: string | null;
  last_units_count: number | null;
  extra: Record<string, unknown> | null;
}

const COLS = `
  canonical_id, apartment_id, proj_name, address, city, state, zip_code,
  country, phone, email_address, website, pmc, website_design, concessions,
  first_seen_date, last_seen_at,
  last_scrape_status, last_units_count, extra
`;

export class PgPropertyStateStore implements IPropertyStateStore {
  constructor(private readonly pool: PgPool) {}

  async all(): Promise<PropertyStateRecord[]> {
    const { rows } = await this.pool.query<Row>(
      `select ${COLS} from properties order by canonical_id`,
    );
    return rows.map(this.toRecord);
  }

  async get(canonicalId: string): Promise<PropertyStateRecord | null> {
    const { rows } = await this.pool.query<Row>(
      `select ${COLS} from properties where canonical_id = $1`,
      [canonicalId],
    );
    return rows[0] ? this.toRecord(rows[0]) : null;
  }

  private toRecord(row: Row): PropertyStateRecord {
    return {
      canonicalId: row.canonical_id,
      apartmentId: row.apartment_id,
      projName: row.proj_name,
      // Mirror v2 name into v1 alias so code that reads `.name` still works.
      name: row.proj_name,
      address: row.address,
      city: row.city,
      state: row.state,
      zipCode: row.zip_code,
      zip: row.zip_code,
      country: row.country,
      phone: row.phone,
      emailAddress: row.email_address,
      website: row.website,
      pmc: row.pmc,
      websiteDesign: row.website_design,
      concessions: row.concessions,
      firstSeenDate: row.first_seen_date,
      lastSeenAt: row.last_seen_at,
      lastScrapeStatus: row.last_scrape_status,
      lastUnitsCount: row.last_units_count,
      extra: row.extra ?? {},
    };
  }
}
