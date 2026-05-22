/**
 * @file concession.test.ts
 * @description Vitest mirror of the Python ``test_concession_enrich``
 * suite. Pins the TS port against the same canonical samples so any
 * future divergence between the two implementations fails CI loudly.
 *
 * If you add a pattern to ``ma_poc/core/concession_enrich.py``, add
 * the equivalent test HERE too — the two modules are kept in
 * lock-step intentionally.
 */
import { describe, expect, it } from 'vitest';

import {
  buildConcessionFields,
  concessionBanner,
  enrichConcession,
} from '../../src/utils/concession.js';

describe('enrichConcession — contract', () => {
  it('returns empty enrichment for empty / null / non-string input', () => {
    for (const input of ['', null, undefined, '   ', 123 as unknown as string]) {
      const e = enrichConcession(input);
      expect(e.atoms).toEqual([]);
      expect(e.primaryAtom).toBeNull();
      expect(e.conditions).toEqual([]);
      expect(e.banner).toBe('');
    }
  });

  it('is idempotent — running twice yields identical output', () => {
    const t = 'Get 2 months FREE rent on select homes by 5/31/26!';
    expect(enrichConcession(t)).toEqual(enrichConcession(t));
  });
});

describe('enrichConcession — HTML entity decoding', () => {
  it('decodes &amp; / &nbsp; / numeric entities', () => {
    const e1 = enrichConcession('Get 2 Months Free&nbsp; On select homes!');
    expect(e1.banner).not.toContain('&nbsp;');
    expect(e1.primaryAtom?.offerType).toBe('free_rent');

    const e2 = enrichConcession('Lease today &amp; save $500 off rent!');
    expect(e2.banner).not.toContain('&amp;');

    const e3 = enrichConcession('Get 1 Month Free&#33; Limited time.');
    expect(e3.banner).not.toContain('&#');
  });
});

describe('enrichConcession — free_rent', () => {
  it.each([
    ['Get 1 month free!',             '1 month'],
    ['Receive 2 months FREE!',        '2 months'],
    ['Up to 3 months FREE',           '3 months'],
    ['Get 6 WEEKS of FREE RENT',      '6 weeks'],
    ['Receive up to TEN WEEKS FREE',  '10 weeks'],
    ['One Month FREE + Waived App Fee', '1 month'],
    ['Two Months Free Rent',          '2 months'],
  ])('extracts free_rent value from %s', (raw, expected) => {
    const e = enrichConcession(raw);
    expect(e.primaryAtom?.offerType).toBe('free_rent');
    expect(e.primaryAtom?.target).toBe('rent');
    expect(e.primaryAtom?.value).toBe(expected);
  });

  it('handles inverted form ("free rent for 2 months")', () => {
    const e = enrichConcession('Move in by May 30th and receive FREE rent for 2 months');
    expect(e.primaryAtom?.offerType).toBe('free_rent');
    expect(e.primaryAtom?.value).toBe('2 months');
  });

  it('handles ordinal form ("first month\'s rent FREE")', () => {
    const e = enrichConcession(
      "Receive your first month's rent FREE when you sign a 12 month lease!",
    );
    expect(e.primaryAtom?.offerType).toBe('free_rent');
    expect(e.primaryAtom?.value).toContain('first');
  });

  it('handles article form ("a month of free rent")', () => {
    const e = enrichConcession(
      ') Get a month of FREE RENT when you sign a 12 month lease with us!',
    );
    expect(e.primaryAtom?.offerType).toBe('free_rent');
    expect(e.primaryAtom?.value).toBe('1 month');
  });

  it('rejects impossible counts (>24)', () => {
    expect(enrichConcession('99 months free!').primaryAtom).toBeNull();
  });
});

describe('enrichConcession — dollar / percent / gift', () => {
  it('extracts save $N (cue prefix, no tail required)', () => {
    const e = enrichConcession('Save up to $1000 on select homes!');
    expect(e.primaryAtom?.offerType).toBe('dollar_off');
    expect(e.primaryAtom?.value).toBe('$1,000');
  });

  it('routes "$200 gift card" to gift_card', () => {
    const e = enrichConcession('Apply within 24 hours and get a $200 gift card!');
    expect(e.primaryAtom?.offerType).toBe('gift_card');
    expect(e.primaryAtom?.target).toBe('gift_card');
  });

  it('classifies "$1000 off move-in cost"', () => {
    const e = enrichConcession('$1000 off total move-in cost with move-in by 11/07');
    expect(e.primaryAtom?.target).toBe('move_in_cost');
  });

  it('does NOT promote "$95 Late Fee" (no concession context)', () => {
    const e = enrichConcession('Late Fee $95 Per occurrence Water and Sewer varies Monthly');
    expect(e.primaryAtom).toBeNull();
  });

  it('extracts percent_off', () => {
    const e = enrichConcession('Limited Time Offer: 50% off rent for 3 Months.');
    expect(e.primaryAtom?.offerType).toBe('percent_off');
    expect(e.primaryAtom?.value).toBe('50%');
  });

  it('rejects out-of-range percentages (>99)', () => {
    expect(enrichConcession('150% off').primaryAtom).toBeNull();
  });
});

describe('enrichConcession — waived fee', () => {
  it('classifies waived application fee', () => {
    const e = enrichConcession('Waived application fee + 1 month free!');
    const waived = e.atoms.find((a) => a.offerType === 'waived_fee');
    expect(waived?.target).toBe('app_fee');
  });

  it('classifies $0 administrative fees as waived', () => {
    const e = enrichConcession('Now $0 Administrative Fees + Move in by May 31st');
    const waived = e.atoms.find((a) => a.offerType === 'waived_fee');
    expect(waived?.target).toBe('admin_fee');
  });
});

describe('enrichConcession — conditions', () => {
  it('extracts deadline (named month)', () => {
    const e = enrichConcession('1 month free; move-in by July 15th');
    const d = e.conditions.find((c) => c.kind === 'deadline');
    expect(d?.value).toContain('July');
  });

  it('extracts lease length range', () => {
    const e = enrichConcession('Get 6 WEEKS of FREE RENT when you sign a 13-15 month lease!');
    const l = e.conditions.find((c) => c.kind === 'lease_length');
    expect(l?.value).toBe('13-15 months');
  });

  it('extracts apply_within window', () => {
    const e = enrichConcession(
      'Get 4 WEEKS FREE base rent if you lease within 24 hours of your tour',
    );
    const a = e.conditions.find((c) => c.kind === 'apply_within');
    expect(a?.value).toBe('24h');
  });

  it('extracts unit_scope=select', () => {
    const e = enrichConcession('Receive up to 2 MONTHS FREE on select homes');
    const s = e.conditions.find((c) => c.kind === 'unit_scope');
    expect(s?.value).toBe('select');
  });

  it('extracts specific-bedroom unit_scope', () => {
    const e = enrichConcession('Up to 4 weeks free on three bedrooms!');
    const s = e.conditions.find((c) => c.kind === 'unit_scope');
    expect(s?.value).toBe('3-bedroom');
  });

  it('extracts audience (student / healthcare)', () => {
    const e = enrichConcession('Student and Healthcare Special: Receive $500 off');
    const a = e.conditions.find((c) => c.kind === 'audience');
    expect(['student', 'healthcare']).toContain(a?.value);
  });

  it('extracts restrictions sentinel without leaking into banner', () => {
    const e = enrichConcession('1 month free! *Restrictions apply');
    const r = e.conditions.find((c) => c.kind === 'restrictions');
    expect(r).toBeDefined();
    expect(e.banner.toLowerCase()).not.toContain('restrictions');
  });
});

describe('enrichConcession — banner', () => {
  it('renders offer + deadline + scope', () => {
    const e = enrichConcession(
      'Get 2 MONTHS FREE on select homes! Must move-in by 5/31/2026.',
    );
    expect(e.banner.toLowerCase()).toContain('2 months');
    expect(e.banner).toContain('FREE rent');
    expect(e.banner).toContain('5/31/2026');
    expect(e.banner).toContain('select units');
  });

  it('renders dollar + lease length', () => {
    const e = enrichConcession(
      'Lease Now & Save Get $500 off when you sign a 13-15 month lease!',
    );
    expect(e.banner).toContain('$500');
    expect(e.banner).toContain('13');
  });

  it('falls back to raw text when no atom matched', () => {
    const e = enrichConcession('Limited Time Offer!');
    expect(e.primaryAtom).toBeNull();
    expect(e.banner).toBe('Limited Time Offer!');
  });

  it('bounds banner to 140 chars', () => {
    const e = enrichConcession('X '.repeat(500));
    expect(e.banner.length).toBeLessThanOrEqual(140);
  });
});

describe('concessionBanner / buildConcessionFields', () => {
  it('concessionBanner returns just the banner string', () => {
    expect(concessionBanner('Get 1 month free!')).toContain('1 month FREE rent');
    expect(concessionBanner(null)).toBe('');
    expect(concessionBanner('')).toBe('');
  });

  it('buildConcessionFields returns all four fields from raw text', () => {
    const f = buildConcessionFields(
      'Get 2 months FREE on select homes by 5/31!',
    );
    expect(f.activeConcession).toContain('2 months FREE');
    expect(f.concessionBanner).toContain('2 months FREE rent');
    expect(f.concessionOfferType).toBe('free_rent');
    expect(f.concessionTarget).toBe('rent');
  });

  it('buildConcessionFields returns all-nulls for empty input', () => {
    expect(buildConcessionFields(null)).toEqual({
      activeConcession: null,
      concessionBanner: null,
      concessionOfferType: null,
      concessionTarget: null,
    });
    expect(buildConcessionFields('   ')).toEqual({
      activeConcession: null,
      concessionBanner: null,
      concessionOfferType: null,
      concessionTarget: null,
    });
  });

  it('preserves raw activeConcession when enricher could not parse', () => {
    // Marketing prose with no recognised offer phrase — banner is null
    // (NOT the cleaned raw) so the frontend explicitly falls back.
    const f = buildConcessionFields('Welcome to our community!');
    expect(f.activeConcession).toBe('Welcome to our community!');
    expect(f.concessionBanner).toBeNull();
    expect(f.concessionOfferType).toBeNull();
  });

  it('surfaces banner when the enricher parsed an offer', () => {
    const f = buildConcessionFields('Get 1 month free with a 12 month lease!');
    expect(f.concessionBanner).toContain('1 month FREE rent');
    expect(f.concessionBanner).toContain('12+ months lease');
  });
});

describe('multi-offer handling', () => {
  it('records every atom but picks free_rent as primary', () => {
    const e = enrichConcession('One Month FREE + Waived App Fee + $500 gift card!');
    const types = e.atoms.map((a) => a.offerType);
    expect(types).toContain('free_rent');
    expect(types).toContain('waived_fee');
    expect(types).toContain('gift_card');
    expect(e.primaryAtom?.offerType).toBe('free_rent');
  });

  it('deduplicates overlapping matches', () => {
    const e = enrichConcession('Look and Lease - 1 Month FREE');
    const lookLeaseCount = e.atoms.filter((a) => a.offerType === 'look_and_lease').length;
    const freeRentCount = e.atoms.filter((a) => a.offerType === 'free_rent').length;
    expect(lookLeaseCount).toBe(1);
    expect(freeRentCount).toBe(1);
  });
});
