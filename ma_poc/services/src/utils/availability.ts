/**
 * @file availability.ts
 * @description Status + raw-date resolution helpers shared across every
 * service-layer transform that maps a stored unit row into the Unit
 * shape the API contract emits.
 *
 * Why this file exists
 * --------------------
 * Pre-2026-05-20 every service-layer transform did:
 *
 *     availabilityStatus: u.available_date ? 'AVAILABLE' : 'UNKNOWN'
 *
 * — i.e. *derived* the status from the typed date column instead of
 * reading the explicit ``availability_status`` field the Python
 * pipeline stamps on every unit. With the typed date column null on
 * 94 % of units (the Python ``_format_v2_unit`` key-alias bug fixed
 * in the same release), the UI shipped ``UNKNOWN`` for every unit
 * even when the underlying scrape had ``AVAILABLE``. This helper is
 * the single chokepoint that picks the right value.
 */

import type { AvailabilityStatus } from '../types/property.js';

/** Producer-emitted status values we accept verbatim (after uppercasing).
 *  Anything outside this set falls through to date-based inference so an
 *  unknown producer literal doesn't leak into the API contract. */
const _KNOWN_STATUSES: ReadonlySet<AvailabilityStatus> = new Set([
  'AVAILABLE',
  'UNAVAILABLE',
  'WAITLIST',
  'COMING_SOON',
  'UNKNOWN',
]);

/**
 * Resolve the canonical availability status for a unit.
 *
 * Priority order (first match wins):
 *   1. Explicit producer status (``raw``) when it normalises to one of
 *      the canonical {@link AvailabilityStatus} values.
 *   2. Date-based inference: when the typed date is set, the unit is
 *      ``AVAILABLE``.
 *   3. ``UNKNOWN`` when neither signal is present.
 *
 * @param raw            The stored ``availability_status`` value (any case).
 * @param availableDate  The typed ``available_date`` ISO string (or null).
 * @returns A canonical {@link AvailabilityStatus} literal.
 */
export function resolveAvailabilityStatus(
  raw: string | null | undefined,
  availableDate: string | null | undefined,
): AvailabilityStatus {
  if (raw) {
    const upper = raw.trim().toUpperCase() as AvailabilityStatus;
    if (_KNOWN_STATUSES.has(upper)) {
      return upper;
    }
    // Unknown producer literal — fall through to date inference rather
    // than shipping an unrecognised string. The Python normaliser
    // already collapses long-tail values like ``AVAILABLE_READY`` and
    // ``https://schema.org/InStock`` to canonical values upstream, so
    // hitting this branch usually means the row was written by a
    // legacy code path that didn't go through the normaliser.
  }
  if (availableDate) {
    return 'AVAILABLE';
  }
  return 'UNKNOWN';
}

/**
 * Choose the best display string for the "available date" column.
 *
 * Returns the typed ISO date when set, otherwise falls back to the
 * producer literal (``availableDateRaw``) so the UI never ships ``null``
 * for a unit where the producer DID emit a date string we couldn't
 * normalise. Callers that want strict ISO-only semantics should read
 * ``availableDate`` directly.
 */
export function pickDisplayDate(
  availableDate: string | null | undefined,
  availableDateRaw: string | null | undefined,
): string | null {
  if (availableDate) return availableDate;
  if (availableDateRaw) return availableDateRaw;
  return null;
}
