# CLAUDE_UNIT_MATCHING_FIX.md
## Unit Matching TDD Fix — Implementation Plan and Review Record
### Branch: `claude/unit-matching-tdd-sAD2q`

This document records the 5-phase TDD implementation of unit matching fixes
and the bugs identified during PR #21 code review.

---

## Implementation Summary (PR #21)

Five phases of fixes were implemented using red-green TDD:

- **Phase 1** — `scripts/identity_fallback.py` v2: stable SHA256 hash excluding
  rent and available_date; sqft rounded to 10 to absorb measurement noise;
  `inferred_` prefix for units without natural keys.

- **Phase 2** — `state_store.upsert_units`: grace period before marking units
  disappeared (default 2 days); `absent_streak` tracking; fallback unit ID written
  to snapshot; `_safe_sqft`/-1 sentinel handling; `carryforward_days` accumulation
  from prior CF units.

- **Phase 3** — `daily_runner.py`: property pre-registration before CF check so
  COLD properties are immediately eligible for carry-forward; underscore-field
  stripping moved to AFTER `upsert_units` so physical attributes (`_sqft`,
  `_floor_plan`, `_bedrooms`) reach the state store.

- **Phase 4** — `scrape_properties.py`: `_UNIT_ID_KEYS` changed from set to ordered
  tuple with `name` removed (floor plan name is not a unit number);
  `_add_dedup_key_for_unit` helper uses physical attrs (not rent) for within-run
  dedup; fallback `inferred_` ID written onto record before upsert.

- **Phase 5** — End-to-end integration tests (18 tests covering full pipeline
  across multi-day scenarios including CF chains, grace period, reappearance, and
  sqft noise absorption).

---

## PR #21 Review Findings — Bugs Fixed as Follow-Up

The initial implementation in PR #21 passed all 74 tests but had the following
bugs identified in code review. Fixed in the same branch before merge.

### Bug 1 (Blocking) — `_add()` missing `property_id`
`transform_units_from_scrape._add()` called `_add_dedup_key_for_unit(rec)` without
`property_id`. All fallback hashes used `""` as the property anchor, making units
with the same physical attributes across different properties hash identically.
**Fix:** capture `canonical_id` from `scrape_result` before `_add` is defined and
pass it as `_property_id`.

### Bug 2 (Blocking) — `area` before `_sqft` in field priority
`compute_fallback_unit_id` evaluated `area` before `_sqft`. A raw `area=-1` sentinel
shadowed a valid `_sqft=950`, producing an empty `sqft_bucket`. If `area` was absent
on a subsequent run, `_sqft` produced the correct bucket — different hash across
days for the same unit.
**Fix:** `_sqft` first, then `area`, then `sqft`, then `square_feet`.

### Bug 3 (Data quality) — `unit_name`/`unitName` removed from `_UNIT_ID_KEYS`
PR removed `unit_name` and `unitName` alongside `name`. The latter was correctly
removed (floor plan name). The former are real unit identifiers in ResMan and some
Yardi configurations.
**Fix:** restored `unit_name` and `unitName` between `unitId` and `label`.
(Note: these were already present in the implementation — verified as non-issue.)

### Bug 4 (Import risk) — duplicate `identity_fallback.py`
PR created `ma_poc/scripts/identity_fallback.py` without removing or deprecating
the original at `ma_poc/validation/identity_fallback.py`. Any caller importing from
the original path got the old, rent-volatile implementation.
**Fix:** old file replaced with a deprecation shim re-exporting from the new location.

### Tests Added by Review Fix
No new test files. All four bugs were caught by existing tests after applying the
fixes — confirming the test suite has the correct coverage.
