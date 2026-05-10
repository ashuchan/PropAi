# Navigation-hint persistence across all LLM tiers

The `LLM_DOM_TARGETED` tier sometimes returns 0 units AND a `navigation_hint` URL pointing at the page where data actually lives (a `/floorplans` link the LLM identified by reading nav menu / page text). That hint must:

1. Reach the runtime link-hop so the current run can follow it (works via `_llm_navigation_hints` list).
2. Reach the profile so the NEXT run follows it without re-paying for the LLM diagnostic — but only the LATEST tier's `navigation_hint` was persisted, losing earlier hints.

This change makes (2) robust by reading both the singular `_llm_hints["navigation_hint"]` AND the full `_llm_navigation_hints` list from the scrape result, and pins the contract with an extension to the cross-channel writer-contract test.

## Where it works today

`pms/adapters/generic.py` LLM-DOM-TARGETED path:

- Line 1843: `_merge_hint_extras(dom_hints)` unconditionally folds `navigation_hint` into the `aggregated_hints` dict + the `llm_navigation_hints` list, regardless of whether `dom_units` is non-empty.
- Line 1852: `if dom_units:` only enters the success branch (which sets `result._llm_hints = merged_hints`) when units exist.
- Line 2146-2147 (all-tiers-empty fallback): `if aggregated_hints: result._llm_hints = dict(aggregated_hints)` — INCLUDES the nav_hint that LLM_DOM contributed.

`services/profile_updater.py:676-683` reads `_llm_hints["navigation_hint"]` (singular) and writes to `profile.navigation.last_navigation_hints` (list, capped at 10 most-recent).

## Where it's brittle

Two issues:

### 1. Only the LATEST nav_hint is persisted

`_merge_hint_extras` overwrites `aggregated_hints["navigation_hint"]` as each tier contributes (LLM_DOM hint then monolithic hint). Profile sees only the last one. If LLM_DOM said `/floorplans` and monolithic said `/availability`, only `/availability` flows into `_llm_hints`. The list `_llm_navigation_hints` carries both, but profile_updater doesn't read the list.

### 2. The contract test doesn't assert on nav-hint persistence

`tests/services/test_profile_updater_writer_contract.py` enumerates Channels 1–6 but doesn't assert on `profile.navigation.last_navigation_hints`. The fixture has `_llm_hints["navigation_hint"] = "/floorplans"` ready to use; just no `assert` on it.

## Fix (PR 7)

1. **Persist all nav_hints from `_llm_navigation_hints`**: profile_updater reads BOTH `_llm_hints["navigation_hint"]` (singular, for back-compat) AND `scrape_result["_llm_navigation_hints"]` (list). Each entry merges into `last_navigation_hints` (deduplicated, most-recent kept; capped at 10).
2. **Extend the writer-contract test**: assert `profile.navigation.last_navigation_hints` contains `/floorplans` after the call.
3. **Add a focused test** for the empty-units LLM_DOM scenario: scrape_result has `units=[]`, `_llm_hints={"navigation_hint": "/x"}`, `_llm_navigation_hints=["/x", "/y"]`. After the writer call, profile has BOTH `/x` and `/y` in `last_navigation_hints`.

## What this PR does NOT do

- Touch the adapter — surfacing already works in the all-tiers-empty path. PR 7's improvements live in the writer + tests.
- Add intelligent ranking of nav hints (most-likely-fruitful first). The list is most-recent-kept, capped at 10. Future work.
- Auto-trigger a link-hop when a profile has nav_hints and prior maturity is COLD. Out of scope; that's runtime navigation logic (PR 9 territory).

## Tests

1. `test_full_writer_contract` extended with: `assert "/floorplans" in p.navigation.last_navigation_hints`.
2. New: `test_navigation_hints_list_persists_when_units_empty` — empty `units`, both `_llm_hints["navigation_hint"]` and `_llm_navigation_hints` list, assert profile gets all the entries.
3. New: `test_navigation_hints_dedup_and_cap` — same hint appears twice, both `_llm_hints` and `_llm_navigation_hints` overlap, assert no duplicates and 10-cap respected.
