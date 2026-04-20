# Jugnu Pipeline Fixes — 2026-04-20

**Source analysis:** `data/runs/2026-04-20/report.json` + `properties.json` (200 properties, 84% success vs. 95% SLO, LLM spend $4.22 vs. $1 SLO).

This document specifies seven fixes in dependency order. Implement them top-to-bottom. Each fix has its own self-contained section with files, implementation, and named tests. F2 (LLM rescue for Tier-1 adapters) is the largest and most detailed — it is the core of this pass.

---

## Workflow rules (follow for every fix)

1. **Read requirements** — the full fix section below, plus the files it names.
2. **Implement** — production code first.
3. **Write tests immediately** — same session, same fix. Never defer.
4. **Run tests** — `pytest . --ignore=data --ignore=config` from `ma_poc/`. All named tests must pass.
5. **Static analysis** — `mypy` strict on any file you created or modified; `ruff` clean.
6. **Integration gate** — run `python scripts/jugnu_runner.py --csv config/properties.csv --limit 10` after each fix group. Confirm no regression in success rate or schema.
7. **Post-completion bug hunt** — re-read your diff, check the gotchas listed per-fix.

### Non-negotiables

- **Pydantic v2 only**: `model.model_dump(mode="json")`. Never `.dict()`.
- **Hashing**: `hashlib.sha256` always. Never Python built-in `hash()`.
- **Concurrency**: `asyncio.Lock` for state-file access; `asyncio.Semaphore` for browser concurrency.
- **Playwright**: `context.close()`, not `browser.close()`.
- **46-key output schema is frozen.** No schema-shape changes without explicit approval.
- **Never-fail contract preserved.** No single property can crash the run — wrap all new code in try/except that degrades gracefully.
- **LLM as teacher, not worker.** Every LLM call must persist hints to a profile so the next run is deterministic.
- **No LLM imports in adapters.** The `no LLM in adapters` rule stands. LLM-rescue lives in `services/`, invoked by the orchestrator only.

---

## Data context — key numbers to reference

**Failure buckets (32 total):**
- `FAILED_UNREACHABLE` (15): 12 TRANSIENT, 2 HARD_FAIL, 1 BOT_BLOCKED. All short-circuited at `generic:no_body_short_circuit`.
- `FAILED_NO_DATA` (17): 9 TIER_1_API, 3 SYNDICATION_ONLY_WIX, 3 TIER_1_API_ENTRATA, 2 TIER_1_API_APPFOLIO. Repeat operators: `gscapts.com` ×3, `mark-taylor.com` ×2.
- **Silent failures inside SUCCESS (6):** Northside Place (id=280734, 14 units), Academy Place (id=221701, 14), Old Shell Lofts (id=217796, 9), Kendry (id=246591, 2), Grant Park Village I (id=61377, 2), 888 Bellevue (id=241760, 1). All TIER_1_API. 42 fully hollow units (beds/baths/rent/area/floor_plan_name all null or -1).

**Missing-field rates on 641 units from 158 non-CF successes:**

| Field             | % missing | Cause |
|-------------------|-----------|-------|
| `lease_term`      | 100%      | Never extracted anywhere |
| `move_in_date`    | 100%      | Never extracted anywhere |
| `available_date`  | 97%       | Only TIER_1_API sometimes supplies it |
| `unit_id`         | 85%       | No identity fallback when natural key absent |
| `rent_high`       | 37%       | Gaps in LLM-DOM / JSON-LD tiers |
| `floor_plan_name` | 37%       | TIER_3_DOM has 100% miss |
| `rent_low`        | 36%       | Gaps in LLM-DOM / JSON-LD tiers |
| `baths`           | 36%       | Mostly in LLM-DOM |
| `area`            | 35%       | Mostly in LLM-DOM |
| `beds`            | 30%       | Mostly in LLM-DOM and JSON-LD |

---

# F1 — Schema-gated unit validation

**Impact:** Flips the 6 silent-failure properties from `SUCCESS` to `FAILED_NO_DATA`.

## Files

- **Modify:** `ma_poc/validation/schema_gate.py`
- **Modify:** `ma_poc/validation/orchestrator.py`
- **Modify:** `ma_poc/reporting/verdict.py`
- **Add:** `tests/validation/test_schema_gate_unit_quality.py`

## Implementation

Add to `schema_gate.py`:

```python
SUBSTANTIVE_FIELDS = ("beds", "rent_low", "floor_plan_name", "area")

def _is_present(value):
    if value is None: return False
    if value == -1: return False
    if isinstance(value, str) and not value.strip(): return False
    return True

def is_substantive(unit): return any(_is_present(unit.get(k)) for k in SUBSTANTIVE_FIELDS)

def property_passes_quality_gate(units, threshold=0.5):
    if not units: return False
    good = sum(1 for u in units if is_substantive(u))
    return (good / len(units)) >= threshold
```

## Tests

```
test_is_substantive_detects_beds_present
test_is_substantive_detects_rent_low_present
test_is_substantive_detects_floor_plan_name_present
test_is_substantive_detects_area_present
test_is_substantive_rejects_area_sentinel_minus_one
test_is_substantive_rejects_empty_string_floor_plan
test_is_substantive_rejects_all_null_unit
test_property_passes_quality_gate_empty_list_fails
test_property_passes_quality_gate_all_substantive_passes
test_property_passes_quality_gate_exactly_half_substantive_passes
test_property_passes_quality_gate_one_third_substantive_fails
test_property_passes_quality_gate_all_hollow_fails
test_orchestrator_flips_next_tier_requested_on_hollow_success
test_verdict_flips_to_failed_no_data_when_all_tiers_hollow
test_regression_northside_place_fixture_now_fails_no_data
```

---

# F2 — LLM rescue for TIER_1_API / TIER_1_API_ENTRATA / TIER_1_API_APPFOLIO

**Impact:** Recovers 17 `FAILED_NO_DATA` and 6 silent-failure properties.

## Files

- **Create:** `ma_poc/services/llm_api_rescue.py`
- **Create:** `config/prompts/api_rescue.txt`
- **Create:** `ma_poc/extraction/heuristics.py`
- **Modify:** `ma_poc/pms/scraper.py`
- **Modify:** `ma_poc/models/scrape_profile.py`
- **Modify:** `ma_poc/observability/events.py`
- **Modify:** `ma_poc/services/profile_updater.py`

## Key constraints

- One body per LLM call. Never batch.
- Max 2 LLM calls per property per run.
- `SUPPORTED_ADAPTERS = frozenset({"generic", "entrata", "appfolio"})`
- Skip when: zero api_responses; consecutive_llm_rescue_failures >= 3; bot_blocked/ssl_error; quality_gate already True.
- Tier labels: `TIER_1_API_LLM_RESCUE`, `TIER_1_API_ENTRATA_LLM_RESCUE`, `TIER_1_API_APPFOLIO_LLM_RESCUE`
- Persist json_paths + envelope to profile as LlmFieldMapping for deterministic replay.

## Tests

```
test_rescue_returns_empty_when_no_api_responses
test_rescue_returns_empty_when_unsupported_adapter
test_rescue_filter_drops_blocked_endpoints_from_profile
test_rescue_filter_drops_foreign_host_responses
test_rescue_filter_keeps_known_pms_hosts
test_rescue_rank_prefers_availability_url_pattern
test_rescue_rank_prefers_unit_shaped_body
test_rescue_rank_breaks_ties_by_url_length
test_rescue_trim_preserves_unit_array_truncates_to_200
test_rescue_trim_drops_nav_marketing_keys
test_rescue_trim_sets_truncation_sentinel
test_rescue_trim_returns_deep_copy_never_mutates
test_rescue_prompt_substitutes_placeholders_preserves_json_schema_braces
test_rescue_respects_max_llm_calls_cap
test_rescue_retries_on_json_decode_error_once
test_rescue_stops_retrying_after_second_json_decode_error
test_rescue_rejects_llm_output_that_fails_quality_gate
test_rescue_tier_label_generic_returns_TIER_1_API_LLM_RESCUE
test_rescue_tier_label_entrata_returns_TIER_1_API_ENTRATA_LLM_RESCUE
test_rescue_tier_label_appfolio_returns_TIER_1_API_APPFOLIO_LLM_RESCUE
test_rescue_persists_json_paths_to_llm_field_mappings
test_rescue_persists_noise_urls_to_blocked_endpoints
test_rescue_cost_accumulates_across_retries
test_rescue_never_raises_on_internal_exception
test_rescue_url_to_pattern_replaces_numeric_ids
test_rescue_url_to_pattern_strips_query_string
test_scraper_skips_rescue_when_quality_gate_passes
test_scraper_skips_rescue_when_no_api_responses
test_scraper_skips_rescue_when_pms_is_rentcafe
test_scraper_skips_rescue_when_consecutive_failures_geq_3
test_scraper_skips_rescue_when_page_unreachable
test_scraper_invokes_rescue_for_generic_adapter_empty_units
test_scraper_invokes_rescue_for_entrata_adapter_empty_units
test_scraper_invokes_rescue_for_appfolio_adapter_empty_units
test_scraper_replaces_empty_result_with_rescue_units_on_success
test_scraper_records_cost_even_on_rescue_failure
test_scraper_increments_failure_counter_on_rescue_failure
test_scraper_resets_failure_counter_on_rescue_success
test_scraper_emits_all_three_rescue_events
```

---

# F3 — Identity fallback hash

**Impact:** Drops `unit_id` missing rate from 85% to ~15%.

## Files

- **Modify:** `ma_poc/validation/identity_fallback.py` (add `compute_fallback_unit_id`)
- **Modify:** `ma_poc/validation/orchestrator.py`

## Implementation

```python
def compute_fallback_unit_id(unit, property_id):
    parts = [
        property_id or "",
        _norm(unit.get("floor_plan_name")),
        _norm(unit.get("beds")),
        _norm(unit.get("baths")),
        _norm(unit.get("area")),
        _norm(unit.get("rent_low")),
        _norm(unit.get("available_date")),
    ]
    identifying = parts[1:]
    if sum(1 for p in identifying if p) < 2: return None
    digest = hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]
    return f"inferred_{digest}"
```

## Tests

```
test_fallback_returns_none_when_all_fields_missing
test_fallback_returns_none_when_only_one_field_present
test_fallback_returns_id_when_two_fields_present
test_fallback_id_is_stable_across_calls
test_fallback_id_differs_for_different_content
test_fallback_id_differs_for_different_property_same_content
test_fallback_id_handles_area_sentinel_minus_one
test_fallback_id_prefix_is_inferred_
test_fallback_id_length_is_25_chars_inferred_plus_16_hex
test_fallback_ignores_case_and_whitespace
test_orchestrator_fills_missing_unit_ids_via_fallback
test_orchestrator_preserves_natural_unit_ids
```

---

# F4 — Contract-level fix for unextracted fields

**Impact:** Reduces available_date missing rate from 97%.

## Decision

- Category A (`lease_term`, `move_in_date`): keep in schema, add to F2 prompt, stop flagging as missing in reports.
- Category B (`available_date`): add DOM-level extraction.
- Category C (`pmc`, `website_design`, `phone`, `email_address`, `concessions`): suppress from missing-field reports.

## DOM heuristic (available_date)

Try selectors in order: `[class*="available"]`, `[data-available-date]`, `<time>`, regex `/(available|move[- ]?in)[\s:]+.../i`. Parse with dateutil. Normalize to ISO `YYYY-MM-DD`. Skip pre-today dates unless "available now".

## Tests

```
test_available_date_extracts_from_data_available_date_attr
test_available_date_extracts_from_time_element
test_available_date_extracts_from_class_available
test_available_date_regex_handles_jan_15_2026
test_available_date_regex_handles_1_15_2026
test_available_date_regex_case_insensitive
test_available_date_skips_past_dates_unless_available_now
test_available_date_normalizes_to_iso_yyyy_mm_dd
test_run_report_annotates_non_extracted_fields
```

---

# F5 — Reclassify TIER_2_JSONLD as property-metadata only

**Impact:** Removes 36 phantom units from 10 properties.

## Rule for emitting a JSON-LD unit

The parent must be `ItemList`, `Offer` array of length >=2, or `ApartmentUnit[]`. Every emitted unit needs >=2 of: distinct `numberOfRooms`, `floorSize`, `price`, `name`. Single `Apartment` schema objects are property metadata only.

## Tests

```
test_jsonld_single_apartment_schema_returns_empty_units
test_jsonld_itemlist_with_two_distinct_units_returns_units
test_jsonld_itemlist_with_two_identical_units_returns_empty
test_jsonld_property_metadata_extracted_even_when_no_units
test_jsonld_apartment_array_with_varying_rent_returns_units
test_jsonld_parse_returns_tuple_not_list
```

---

# F6 — Cluster-retry for PMC-portal failures

**Impact:** Unblocks up to 7 properties (gscapts ×3, mark-taylor ×2).

## Files

- **Create:** `scripts/cluster_retry.py`

## Tests

```
test_cluster_analysis_groups_by_registered_domain_not_full_host
test_cluster_analysis_sorts_clusters_by_failure_count_desc
test_cluster_analysis_includes_sample_body_truncated_to_500
test_cluster_retry_filters_properties_by_domain
test_cluster_retry_preserves_full_runner_output_shape
```

---

# F7 — Report + verdict reconciliation

**Impact:** Fixes 2 reporting inconsistencies.

## Problems

1. Carry-forward properties show `verdict_reason = "all checks passed"` instead of `"carry_forward_applied"`.
2. `generic:no_body_short_circuit` appears in `tier_distribution` inflating the denominator.

## Fix 7.1

In `verdict.py`, check `carry_forward_applied` FIRST and always return `"carry_forward_applied"` reason.

## Fix 7.2

In `run_report.py`, separate pre-extraction terminations into `pre_extraction_terminations` dict; remove from `tier_distribution`.

## Tests

```
test_verdict_carry_forward_always_sets_reason_carry_forward_applied
test_verdict_carry_forward_overrides_all_checks_passed
test_verdict_non_cf_success_keeps_all_checks_passed
test_run_report_funnel_has_five_expected_keys
test_run_report_tier_distribution_excludes_no_body_short_circuit
test_run_report_pre_extraction_terminations_counts_fetch_outcomes
test_run_report_back_compat_total_succeeded_unchanged
```

---

# Final integration gate

```bash
python scripts/jugnu_runner.py --csv config/properties.csv --limit 200 --run-date 2026-04-22
```

| Metric | 04-20 | Target | SLO |
|---|---|---|---|
| Success rate | 84.0% | >= 90% | >= 95% |
| Silent failures | 6 | 0 | 0 |
| unit_id missing rate | 85% | < 20% | < 10% |
| available_date missing | 97% | < 80% | < 50% |
| Phantom JSON-LD units | 36 | 0 | 0 |
| LLM cost per run | $4.22 | $2-3 first run | < $1 |
| Verdict/CF reason consistency | 6/10 | 10/10 | 10/10 |

## Post-completion bug hunt checklist

- [ ] No `.dict()` calls in new code.
- [ ] No `hash()` — only `hashlib.sha256`.
- [ ] All new async entry points catch all exceptions.
- [ ] No adapter imports `llm_api_rescue`.
- [ ] `property_passes_quality_gate` called in exactly two places.
- [ ] `consecutive_llm_rescue_failures` defaults to 0 on all existing profiles.
- [ ] 46-key output schema unchanged.
- [ ] `pytest . --ignore=data --ignore=config` green.
- [ ] `mypy` strict on all new/modified files.
- [ ] No `print()` in new code.
