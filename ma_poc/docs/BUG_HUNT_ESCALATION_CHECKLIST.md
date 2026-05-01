# BUG_HUNT_ESCALATION_CHECKLIST.md — Phase E6

All items verified as of Phase E5 implementation.

## Data Model

- [x] `FetchTier` is an `IntEnum` — arithmetic (`int(tier)`, `tier >= floor`) works correctly
- [x] `STEALTH_LOCAL` (value=1) is reserved and never appears in the active ladder
- [x] `DLQ_PARK` (value=5) is never added to the active ladder; only stamped on exhaustion
- [x] `FetchProfile` defaults: `tier_floor=DIRECT`, all counters start at 0
- [x] `FetchProfile` JSON round-trip: `tier_floor` serializes as int, deserializes back to `FetchTier`
- [x] `FetchResult` new fields (`fetch_tier_used`, `fetch_tier_attempts`, `block_signature`) default
      to `0`, `[]`, `None` — backward compatible with all existing callers

## Feature Flags

- [x] `ENABLE_TIER_ESCALATION=false` (default): `Fetcher.fetch()` ignores profile; `update_fetch_profile_after_fetch()` is a no-op
- [x] Sub-flags (`ENABLE_DC_PROXY_TIER`, `ENABLE_RESIDENTIAL_TIER`, `ENABLE_UNLOCKER_TIER`) are short-circuited to False when master flag is off
- [x] `_build_ladder()` only includes tiers whose flag is True — no phantom tier entries

## Block Signature Detection

- [x] `match_block_signature()` scans only first 64KB — prevents OOM on large bodies
- [x] `match_block_signature()` never raises — handles None body, binary garbage, empty bytes
- [x] All 9 known signatures detected: cf_turnstile, cf_challenge, px_block, datadome, akamai_bm, hcaptcha, recaptcha, imperva, generic_403
- [x] `generic_403` fallback only triggers on status=403 (not on 200 challenge pages)
- [x] Header-based detection (CF-RAY → cf_challenge, X-DataDome → datadome) is case-insensitive
- [x] `response_classifier.classify()` returns 2-tuple (backward compatible); `block_signature` populated separately

## Escalation Logic

- [x] Only `BOT_BLOCKED` triggers escalation; `HARD_FAIL` stops the ladder immediately
- [x] `MAX_ESCALATIONS_PER_RUN=3` hard cap prevents infinite cost on misconfigured properties
- [x] `profile.fetch.tier_floor` is respected — ladder starts at floor, not DIRECT
- [x] Provider `NotImplementedError` (stub tiers) is caught and escalation continues
- [x] Provider construction failure (missing env vars) is caught and escalation continues
- [x] Demotion probe: `_should_probe_lower()` returns highest *enabled* tier below floor (skips STEALTH_LOCAL)
- [x] Demotion probe interval guard (24h) works with both tz-aware and tz-naive datetimes
- [x] Probe result short-circuits normal ladder on success
- [x] `last_demotion_probe_at` updated before probe runs (not after), preventing double-probe on same run

## Profile Updater

- [x] Promotion: `tier_used > floor` → floor raised, `total_escalations += 1`, `FETCH_TIER_PERSISTED` emitted
- [x] Same-floor success: `consecutive_successes_at_floor += 1`
- [x] Demotion: `tier_used < floor` → floor lowered, `FETCH_TIER_DEMOTED` emitted
- [x] BOT_BLOCKED: `consecutive_failures_at_floor += 1`, `last_block_signature` stored
- [x] Any exception inside the try block is caught and logged — never raises to caller

## Providers

- [x] `DirectProvider`: no proxy, retries TRANSIENT up to 3 times, returns BOT_BLOCKED immediately
- [x] `DcProxyProvider`: BrightData DC zone, retries TRANSIENT up to 2 times
- [x] `ResidentialProvider`: BrightData residential, sticky session via property_id, 2s inter-request sleep (0.5 RPS cap), single attempt on BOT_BLOCKED
- [x] `UnlockerProvider`: BrightData Web Unlocker, single attempt, credentials redacted from `proxy_used`
- [x] All providers stamp `fetch_tier_used` and `fetch_tier_attempts` correctly
- [x] All providers populate `block_signature` on BOT_BLOCKED outcomes
- [x] `FetchProvider` Protocol is `@runtime_checkable` — `isinstance(DirectProvider(), FetchProvider)` returns True

## Event System

- [x] All 7 new `EventKind` values present in `events.py`
- [x] `emit()` never raises — tested with no ledger configured (logs to `INFO`)
- [x] `FETCH_TIER_ESCALATED`: emitted at start of each tier attempt
- [x] `FETCH_TIER_PERSISTED`: emitted on floor promotion
- [x] `FETCH_TIER_DEMOTED`: emitted on floor demotion
- [x] `FETCH_LADDER_EXHAUSTED`: emitted when all tiers are exhausted → DLQ_PARK
- [x] `FETCH_LADDER_BUDGET_EXHAUSTED`: emitted when MAX_ESCALATIONS_PER_RUN hit mid-ladder
- [x] `FETCH_TIER_PROBE_SUCCESS` / `FETCH_TIER_PROBE_FAILED`: emitted on demotion probe results

## Cost Ledger

- [x] `record_fetch_tier()` stores `proxy_fetch_tier` category entries with tier name
- [x] `rollup_by_fetch_tier()` groups by `tier_used` column

## Escalation Report

- [x] `escalation_report.run()` handles missing `events.jsonl` gracefully (returns empty summary)
- [x] Counts promotions, demotions, DLQ parks, probe success/failure correctly
- [x] Can be called programmatically (returns dict) or from CLI

## Migration

- [x] `migrate_profiles_add_fetch.py` is idempotent — running twice doesn't double-add `fetch` key
- [x] Migration preserves all other profile fields

## Integration Path

- [x] `Fetcher.fetch(task, profile=profile)` dispatches to `fetch_with_escalation()` when flag is True
- [x] `Fetcher.fetch(task)` (no profile) falls through to existing logic unchanged
- [x] All 198 existing unit tests pass with new code (1 pre-existing playwright test skipped)
