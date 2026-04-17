# Bug Hunt Checklist — PMS-First Refactor

## Detection & routing
- [x] `detect_pms` never raises on any input. Fuzz with: `None`, `""`, `"not-a-url"`, `"javascript:alert(1)"`, binary bytes decoded as latin-1
- [x] `detect_pms` is deterministic — same inputs produce same `DetectedPMS` including same `evidence` ordering
- [x] `get_adapter` never returns `None` — unknown PMS returns generic
- [x] Resolver does not navigate more than 5 hops on any input (check with a cycle: A→B→A)
- [x] Resolver cancels in-flight navigation on exception (no zombie browser tabs)

## Orchestrator
- [x] On SSL error, no adapter is called. Grep for any adapter `extract` call in a code path reachable after the error check
- [x] Legacy return keys enumerated in the result dict are all present on every scrape result
- [x] `_property_id` is stamped on every result dict, even when scrape fails early
- [x] Context/browser are closed on every exit path (scraper does not own browser; caller manages lifecycle)

## Adapters
- [x] No adapter directly mutates `profile` — all profile updates go through `profile_updater` after scrape
- [x] No adapter has a broader blocklist entry than the URL pattern it matches
- [x] Every adapter's `extract` returns an `AdapterResult` — not `None`, not a bare list, not a dict
- [x] Adapter `confidence` field in `AdapterResult` is 0.0 on empty units, not an arbitrary default

## Profile
- [x] Every v1 profile in `config/profiles/` loads as v2 without warnings (ConfigDict extra="ignore" on all models)
- [x] `stats.total_llm_cost_usd` monotonically increases — never decreases across runs (additive in integration_helpers)
- [x] `consecutive_unreachable` and `consecutive_failures` are separate fields with distinct semantics
- [x] LRU cap on `explored_links` evicts oldest, not newest (Pydantic validator takes last 50)

## Report
- [x] Report markdown renders valid markdown (tables are properly formatted)
- [x] No report has a section that's present-but-empty (empty sections are omitted)
- [x] Verdict is always one of the 4 literal values — SUCCESS, FAILED_UNREACHABLE, FAILED_NO_DATA, CARRY_FORWARD

## Integration
- [x] Profile update only overwrites api_provider when confidence >= 0.80
- [x] Missing `_detected_pms` key in scrape result does not crash
- [x] Run report includes `properties_by_pms` and `llm_cost_by_pms` metrics

## Note
Models in `models/extraction_result.py`, `models/scrape_event.py`, and `models/unit_record.py` do not have
`ConfigDict(extra="ignore")`. These are from the Phase A BRD-spec pipeline (not the refactor scope) and are
flagged as a follow-up item.
