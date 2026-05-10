# Quality promotion on replay-hit + null-field-recovery preconditions

Two independent self-learning loop improvements that touch the runtime extraction cascade. A third (source-confidence-tiered LLM budget) is documented here as deferred work.

## Promotion on replay hit (`ENABLE_PROMOTE_ON_HINT`)

After every successful replay hit, bump the cached `quality_score` by `+0.05`, clamped at `1.0`, rounded to 2 decimals to avoid float drift. Applied to both `LlmFieldMapping.quality_score` and `dom_hints.field_selectors_quality`.

Combined with the quality-tiered eviction policy (1-strike for `<0.8`, 3-strike for `>=0.8`), a degraded save (starting at 0.4) graduates exactly at 8 successful hits — moving it from the aggressive-prune bucket into the resilient bucket.

## F2 (null-field-recovery) preconditions

`scripts/runners/jugnu.py::_run_null_field_recovery` previously gated only on tier (`TIER_1_*`), `raw_apis` presence, and at least one null unit. Two checks added:

1. `_f2_has_recoverable_body(raw_apis)` — at least one entry has a non-empty `body`. Empty bodies have nothing to recover from.
2. `all_units_total_null` — when EVERY unit has BOTH `rent_low` AND `unit_id` null, this is parser-tier failure, not field-recovery territory; F2 won't help.

Both log + return without paying for any LLM call.

## Deferred: source-confidence-tiered LLM budget

`pms/scraper.py:565-571` hardcodes `{"llm_api_calls": 3, "llm_dom_calls": 1, "llm_monolithic": 1, "link_hop": 3}`. Every property gets the same budget regardless of how confident we already are in its sources.

A property with `source_observations` showing `llm_api_targeted` at `avg_confidence_when_won = 0.95` over 100 contributions doesn't need 3 fresh LLM API analyses. One should suffice; the saved hint should fire. Conversely, a flapping property with `avg_confidence_when_won = 0.4` needs more LLM budget to find a new source.

When the source-tiered budget eventually ships, the recipe is: read `source_observations` from profile; if any source has `avg_confidence_when_won >= 0.85` and `contribution_count >= N`, halve that source's LLM budget; cap by original total. Behind `ENABLE_SOURCE_TIERED_BUDGET` flag, default OFF for canary safety. Hookpoint: `pms/scraper.py:565-571`. Tests would cover the default-on-no-high-confidence path, the high-confidence-halve path, and the budget-cap behavior.

## Tests in this change

**Promotion** (`tests/services/test_quality_promote_on_replay_hit.py`):
- Quality 0.4 → 0.45 on a single hit.
- Quality 0.98 → 1.0 on a hit (clamped, no overflow).
- 0.4 + 8 hits = exactly 0.8 (graduates into the 3-strike resilience tier).
- Flag off: quality unchanged on hit; consecutive_misses still resets.

**F2 preconditions** (`tests/services/test_null_field_recovery_preconditions.py`):
- Empty list, all-None bodies, all-empty-dict bodies, all-empty-list bodies → not recoverable.
- One non-empty dict body, one non-empty list body → recoverable.
- Defensive against malformed `_raw_api_responses` (non-dict entries, missing `body` key).

## Out of scope

- Cross-property source-confidence sharing (clusters).
- Replacing `quality_score` with a Bayesian probability model.
