/**
 * @file PropertyService.scope.test.ts
 * @description Coverage for the Today/All scope toggle behaviour:
 *
 *   1. scope='today' (default) — listSummariesFast is asked for today,
 *      cache is partitioned by scope.
 *   2. scope='all' on getPropertyById falls back to the propertyState
 *      table when the property is absent from today's snapshot —
 *      the bug that motivated the refactor.
 *   3. Lazy unit-state load: scope='today' never touches the units
 *      store, scope='all' does (and only after the property is found).
 *   4. Per-scope cache: today and all don't evict each other.
 *
 * Uses a hand-rolled IDataProvider stub — no real Cloud SQL, no
 * real filesystem. Lets us assert call ordering precisely.
 */

import { describe, expect, test, vi } from 'vitest';
import { PropertyService } from '../../src/services/PropertyService.js';
import type {
  IDataProvider,
  IPropertyStore,
  IPropertyStateStore,
  IUnitStore,
  IRunStore,
  IArtifactStore,
  PropertyStateRecord,
  PropertySummaryRow,
  RawRunProperty,
  RawLedgerEntry,
  SummaryScope,
  UnitStateRecord,
} from '../../src/data-provider/contracts.js';

// ── Stub factory ─────────────────────────────────────────────────────────────

interface Stub {
  provider: IDataProvider;
  spies: {
    listSummariesFast: ReturnType<typeof vi.fn>;
    listForRun: ReturnType<typeof vi.fn>;
    listStateForProperty: ReturnType<typeof vi.fn>;
    propertyStateGet: ReturnType<typeof vi.fn>;
    propertyStateAll: ReturnType<typeof vi.fn>;
    getLatestDate: ReturnType<typeof vi.fn>;
    readLedger: ReturnType<typeof vi.fn>;
    getLlmReport: ReturnType<typeof vi.fn>;
  };
}

function makeStub(opts: {
  fastByScope?: Partial<Record<SummaryScope, PropertySummaryRow[]>>;
  snapshot?: RawRunProperty[];
  state?: PropertyStateRecord[];
  unitsByProperty?: Record<string, UnitStateRecord[]>;
  latestDate?: string | null;
} = {}): Stub {
  const listSummariesFast = vi.fn(async (scope: SummaryScope = 'today') => {
    return opts.fastByScope?.[scope] ?? [];
  });
  const listForRun = vi.fn(async (_d: string) => opts.snapshot ?? []);
  const getForRun = vi.fn(async (_d: string, _id: string) => null);
  const listStateForProperty = vi.fn(async (id: string) => opts.unitsByProperty?.[id] ?? []);
  const propertyStateGet = vi.fn(async (id: string) =>
    (opts.state ?? []).find((s) => s.canonicalId === id) ?? null,
  );
  const propertyStateAll = vi.fn(async () => opts.state ?? []);
  const getLatestDate = vi.fn(async () =>
    opts.latestDate === undefined ? '2026-05-15' : opts.latestDate,
  );
  const readLedger = vi.fn(async (_d: string): Promise<RawLedgerEntry[]> => []);
  const getLlmReport = vi.fn(async (_d: string) => null);

  const properties: IPropertyStore = {
    listForRun,
    getForRun,
    listSummariesFast,
  };
  const propertyState: IPropertyStateStore = {
    all: propertyStateAll,
    get: propertyStateGet,
  };
  const units: IUnitStore = {
    listStateForProperty,
    count: async () => 0,
  };
  const runs: IRunStore = {
    listDates: async () => (opts.latestDate ? [opts.latestDate] : []),
    getLatestDate,
    getReport: async () => null,
    readLedger,
  };
  const artifacts: IArtifactStore = {
    getPropertyReport: async () => null,
    getLlmReport,
    getLlmPropertyDetail: async () => null,
    getLlmDiagnostic: async () => null,
    getProfile: async () => null,
  };
  const provider: IDataProvider = {
    name: 'stub',
    properties,
    propertyState,
    units,
    runs,
    artifacts,
    close: async () => {},
  };
  return {
    provider,
    spies: {
      listSummariesFast,
      listForRun,
      listStateForProperty,
      propertyStateGet,
      propertyStateAll,
      getLatestDate,
      readLedger,
      getLlmReport,
    },
  };
}

function fastRow(canonicalId: string, name = canonicalId): PropertySummaryRow {
  return {
    canonicalId,
    apartmentId: Number(canonicalId.replace(/\D/g, '')) || null,
    name,
    address: `${name} Address`,
    city: 'Cambridge',
    state: 'MA',
    zip: '02139',
    pmc: 'PMC',
    phone: '',
    website: 'https://example.com',
    totalUnits: 10,
    availableUnits: 3,
    avgRent: 2500,
    medianRent: 2500,
    lastSeenAt: '2026-05-15T10:00:00Z',
    lastScrapeStatus: 'SUCCESS',
    ledgerStatus: 'SUCCESS',
    extractionTier: 'TIER_1_API',
    concessions: null,
  };
}

function stateRecord(canonicalId: string, name = canonicalId): PropertyStateRecord {
  return {
    canonicalId,
    projName: name,
    address: `${name} Address`,
    city: 'Cambridge',
    state: 'MA',
    zipCode: '02139',
    pmc: 'PMC',
    phone: '617-555-0100',
    website: 'https://example.com',
    lastScrapeStatus: 'SUCCESS',
    lastSeenAt: '2026-05-10T08:00:00Z',
  };
}

function unitRecord(canonicalId: string, unitId: string): UnitStateRecord {
  return {
    canonicalId,
    unitId,
    rentLow: 2200,
    rentHigh: 2800,
    beds: 1,
    baths: 1,
    area: 750,
    availableDate: '2026-06-01',
    floorPlanName: '1BR',
    lastSeenAt: '2026-05-10T08:00:00Z',
    concessions: null,
  };
}

// ── Tests ────────────────────────────────────────────────────────────────────

describe('PropertyService — scope plumbing', () => {
  test('default scope is "today" — getProperties asks fast path for today', async () => {
    const { provider, spies } = makeStub({
      fastByScope: { today: [fastRow('P1')], all: [fastRow('P1'), fastRow('P2')] },
    });
    const svc = new PropertyService(provider);
    const result = await svc.getProperties();
    expect(result.total).toBe(1);                          // today only
    expect(spies.listSummariesFast).toHaveBeenCalledWith('today');
  });

  test('scope="all" reaches every recorded property', async () => {
    const { provider, spies } = makeStub({
      fastByScope: { today: [fastRow('P1')], all: [fastRow('P1'), fastRow('P2')] },
    });
    const svc = new PropertyService(provider);
    const result = await svc.getProperties({}, undefined, 1, 25, 'all');
    expect(result.total).toBe(2);
    expect(spies.listSummariesFast).toHaveBeenCalledWith('all');
  });

  test('per-scope cache: today and all do not evict each other', async () => {
    const { provider, spies } = makeStub({
      fastByScope: { today: [fastRow('P1')], all: [fastRow('P1'), fastRow('P2')] },
    });
    const svc = new PropertyService(provider);
    await svc.getProperties({}, undefined, 1, 25, 'today');
    await svc.getProperties({}, undefined, 1, 25, 'all');
    await svc.getProperties({}, undefined, 1, 25, 'today');         // cached
    await svc.getProperties({}, undefined, 1, 25, 'all');            // cached
    // Exactly one call per scope despite two flips back-and-forth.
    const args = spies.listSummariesFast.mock.calls.map((c) => c[0]);
    expect(args.filter((a) => a === 'today')).toHaveLength(1);
    expect(args.filter((a) => a === 'all')).toHaveLength(1);
  });
});

describe('PropertyService.getPropertyById — scope=all fallback', () => {
  test('property missing from today snapshot but present in state → returns from state (not 404)', async () => {
    // This is THE bug the review caught: under scope='all' a property
    // scraped 5 days ago should still resolve on the detail page even
    // though it isn't in today's snapshot. Before the fix this returned
    // null and the route handler 404'd.
    const { provider, spies } = makeStub({
      snapshot: [],                                          // today empty
      state: [stateRecord('P-OLD', 'Historic Building')],
      unitsByProperty: { 'P-OLD': [unitRecord('P-OLD', 'U1'), unitRecord('P-OLD', 'U2')] },
    });
    const svc = new PropertyService(provider);

    const result = await svc.getPropertyById('P-OLD', 'all');

    expect(result).not.toBeNull();
    expect(result!.id).toBe('P-OLD');
    expect(result!.name).toBe('Historic Building');
    expect(result!.units).toHaveLength(2);
    expect(result!.units[0].unitId).toBe('U1');
    // Fallback went through propertyState.get + units.listStateForProperty.
    expect(spies.propertyStateGet).toHaveBeenCalledWith('P-OLD');
    expect(spies.listStateForProperty).toHaveBeenCalledWith('P-OLD');
  });

  test('property missing from snapshot under scope=today → null (404)', async () => {
    // Same scenario, but scope='today': legitimately not in today's roster.
    const { provider } = makeStub({
      snapshot: [],
      state: [stateRecord('P-OLD')],
      unitsByProperty: { 'P-OLD': [unitRecord('P-OLD', 'U1')] },
    });
    const svc = new PropertyService(provider);

    const result = await svc.getPropertyById('P-OLD', 'today');

    expect(result).toBeNull();
  });

  test('scope=today never calls units.listStateForProperty (lazy)', async () => {
    // The pre-refactor code called listStateForProperty eagerly before
    // the property was even located. Verify the lazy path: today should
    // never touch the units store.
    const { provider, spies } = makeStub({
      snapshot: [{
        apartment_id: 123,
        proj_name: 'Foo',
        address: 'a',
        city: 'c',
        state: 's',
        zip_code: 'z',
        country: null,
        phone: null,
        email_address: null,
        website: null,
        pmc: null,
        website_design: null,
        concessions: null,
        units: [],
      }],
    });
    const svc = new PropertyService(provider);

    await svc.getPropertyById('123', 'today');

    expect(spies.listStateForProperty).not.toHaveBeenCalled();
  });

  test('scope=all calls listStateForProperty only AFTER snapshot lookup succeeds', async () => {
    const { provider, spies } = makeStub({
      snapshot: [{
        apartment_id: 123,
        proj_name: 'Foo',
        address: 'a', city: 'c', state: 's', zip_code: 'z',
        country: null, phone: null, email_address: null, website: null,
        pmc: null, website_design: null, concessions: null, units: [],
      }],
      unitsByProperty: { '123': [unitRecord('123', 'U1')] },
    });
    const svc = new PropertyService(provider);

    const result = await svc.getPropertyById('123', 'all');

    expect(result).not.toBeNull();
    expect(spies.listStateForProperty).toHaveBeenCalledTimes(1);
    expect(spies.listStateForProperty).toHaveBeenCalledWith('123');
    // Override applied: unit array comes from the units store, not the
    // empty `units: []` array in the snapshot.
    expect(result!.units).toHaveLength(1);
    expect(result!.units[0].unitId).toBe('U1');
  });

  test('scope=all with no runs on record still resolves via state fallback', async () => {
    // Cold-start scenario: state tables populated, but `runs` table empty.
    // Previously this short-circuited to null even though we had data.
    const { provider } = makeStub({
      latestDate: null,
      state: [stateRecord('P-OLD')],
      unitsByProperty: { 'P-OLD': [unitRecord('P-OLD', 'U1')] },
    });
    const svc = new PropertyService(provider);

    const result = await svc.getPropertyById('P-OLD', 'all');

    expect(result).not.toBeNull();
    expect(result!.id).toBe('P-OLD');
  });

  test('scope=all with property genuinely unknown → null', async () => {
    const { provider } = makeStub({ snapshot: [], state: [] });
    const svc = new PropertyService(provider);

    const result = await svc.getPropertyById('does-not-exist', 'all');

    expect(result).toBeNull();
  });
});
