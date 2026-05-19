/**
 * @file availability.test.ts
 * @description Contract tests for the canonical status resolver. Pins the
 * 2026-05-20 behaviour change: explicit producer status wins over
 * date-based inference, which previously shipped UNKNOWN for 77 % of
 * AVAILABLE units because the typed date column was null.
 */

import { describe, expect, it } from 'vitest';

import {
  pickDisplayDate,
  resolveAvailabilityStatus,
} from '../../src/utils/availability.js';

describe('resolveAvailabilityStatus', () => {
  // ── Explicit producer status wins (the 2026-05-20 fix) ────────────────────

  it('returns AVAILABLE when the explicit status is AVAILABLE, even with null date', () => {
    expect(resolveAvailabilityStatus('AVAILABLE', null)).toBe('AVAILABLE');
  });

  it('returns UNAVAILABLE when the explicit status is UNAVAILABLE, even with a future date', () => {
    expect(resolveAvailabilityStatus('UNAVAILABLE', '2026-07-04')).toBe('UNAVAILABLE');
  });

  it('preserves WAITLIST verbatim', () => {
    expect(resolveAvailabilityStatus('WAITLIST', null)).toBe('WAITLIST');
  });

  it('preserves COMING_SOON verbatim', () => {
    expect(resolveAvailabilityStatus('COMING_SOON', null)).toBe('COMING_SOON');
  });

  it('preserves UNKNOWN verbatim', () => {
    expect(resolveAvailabilityStatus('UNKNOWN', null)).toBe('UNKNOWN');
  });

  it('is case-insensitive on the explicit status', () => {
    expect(resolveAvailabilityStatus('available', null)).toBe('AVAILABLE');
    expect(resolveAvailabilityStatus('Unavailable', null)).toBe('UNAVAILABLE');
    expect(resolveAvailabilityStatus('  waitlist  ', null)).toBe('WAITLIST');
  });

  // ── Date-inference fallback (the legacy behaviour, preserved) ─────────────

  it('falls back to AVAILABLE when status is null but date is set', () => {
    expect(resolveAvailabilityStatus(null, '2026-07-04')).toBe('AVAILABLE');
  });

  it('falls back to UNKNOWN when both status and date are null', () => {
    expect(resolveAvailabilityStatus(null, null)).toBe('UNKNOWN');
  });

  it('falls back to UNKNOWN on empty/undefined inputs', () => {
    expect(resolveAvailabilityStatus(undefined, undefined)).toBe('UNKNOWN');
    expect(resolveAvailabilityStatus('', '')).toBe('UNKNOWN');
  });

  // ── Unknown producer strings ──────────────────────────────────────────────

  it('falls through to date inference when status is an unrecognised producer literal', () => {
    // The Python normaliser collapses long-tail values upstream — anything
    // reaching us with an unknown literal means a legacy code path is
    // bypassing the normaliser. Don't surface garbage to the UI.
    expect(resolveAvailabilityStatus('PENDING_INSPECTION', '2026-07-04')).toBe('AVAILABLE');
    expect(resolveAvailabilityStatus('XYZ', null)).toBe('UNKNOWN');
  });
});

describe('pickDisplayDate', () => {
  it('prefers the typed ISO date when set', () => {
    expect(pickDisplayDate('2026-07-04', 'Available 7/4/26')).toBe('2026-07-04');
  });

  it('falls back to the producer literal when the typed date is null', () => {
    expect(pickDisplayDate(null, 'Available 7/24')).toBe('Available 7/24');
  });

  it('returns null when both are absent', () => {
    expect(pickDisplayDate(null, null)).toBeNull();
    expect(pickDisplayDate(undefined, undefined)).toBeNull();
    expect(pickDisplayDate('', '')).toBeNull();
  });
});
