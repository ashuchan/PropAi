# Persistence hardening — three-layer drop on empty `json_paths`

**Channel under repair:** `llm_field_mappings` (the per-API-endpoint deterministic-replay mapping; sub-tier `generic:profile_replay`).

**Symptom (observed in production):**
- DB across 5,054 profiles: **3 profiles with `llm_field_mappings`** (0.1%), 3 total mapping entries.
- Event-side: `generic:profile_replay` ran 7,766 times today; 1 successful replay (0.013%); 7,762 "no saved mappings" skips.
- Cost-side: 125+ successful LLM-API extractions per day (TIER_4_LLM_API + TIER_*_LLM_RESCUE wins) produce mappings, but virtually none survive to the next run.

**Asymmetry diagnostic (Q4):**

| Channel | Profiles with data | % | Total entries |
|---|---|---|---|
| `llm_field_mappings` | 3 | 0.1% | 3 |
| `field_patches` | 0 | 0.0% | 0 |
| `blocked_endpoints` | 618 | 12.2% | (working) |
| `known_endpoints` | 739 | 14.6% | (working) |
| `dom_hints.field_selectors` | 1,897 | 37.5% | (working) |

Same `update_profile_after_extraction` call writes all five. Three channels work; two don't. Bug is localised to those two channels' write paths.

## Root cause: three coordinated drops on `json_paths` empty

The targeted-LLM-API path produces a mapping triplet `(api_url_pattern, json_paths, response_envelope)`. When the LLM returns units but cannot articulate `json_paths` — common when the LLM extracts via semantic understanding rather than path-tracing — the mapping gets dropped at three independent layers:

### Drop 1 — Producer side (`ma_poc/services/llm_extractor.py:553-561`)

```python
json_paths = parsed.get("json_paths", {})
response_envelope = parsed.get("response_envelope", "")
hints: dict[str, Any] = {}
if isinstance(json_paths, dict) and json_paths:    # ← drop
    hints["api_url_pattern"] = api_url
    hints["json_paths"] = json_paths
    hints["response_envelope"] = ...
```

When `json_paths` is empty, `api_url_pattern` is never added to `hints`. Downstream consumers can't tell the LLM analysed this URL at all.

### Drop 2 — Surfacing site (`ma_poc/pms/adapters/generic.py:1742-1751`)

```python
mapping_subset: dict[str, Any] = {}
if isinstance(api_hints, dict):
    for k in ("api_url_pattern", "json_paths", "response_envelope"):
        if k in api_hints:
            mapping_subset[k] = api_hints[k]
if mapping_subset.get("json_paths"):    # ← drop
    llm_field_mappings.append(mapping_subset)
    llm_analysis_results[url] = mapping_subset
```

Even if Drop 1 is fixed and `api_url_pattern` reaches here, this gate would still drop the mapping when `json_paths` is empty.

### Drop 3 — Persistence (`ma_poc/services/profile_updater.py:149-154`)

```python
url_pattern = mapping_dict.get("api_url_pattern", "")
json_paths = mapping_dict.get("json_paths") or {}
if not url_pattern:
    log.warning(...)
    return False
if not json_paths:                       # ← drop
    log.warning(...)
    return False
```

The third gate — what the brief's Action 1 targets — discards the mapping at the writer.

## Why the brief's Action 2 is not needed

`_classify_llm_analysis_verdict` (`profile_updater.py:74-90`) requires `value` to be a dict with `api_url_pattern` to classify it as `kind="mapping"`. The producer (when it produces hints) and the surfacing site (when it stores them) both correctly populate `api_url_pattern`. **The shape matches when persistence is reached.** The classifier's `kind="ignored"` warning never fires for empty-`json_paths` cases because nothing gets surfaced — they're dropped earlier at Drop 1 or Drop 2.

The pinning test `tests/profile/test_profile_updater_full_capture.py::test_unrecognised_analysis_value_logged_not_silently_dropped` exercises a synthetic `int` value (line 154: `"/api/weird": 42`) which IS unrecognised — but that's a synthetic shape that the producer never emits.

## Sample evidence — pid 11327 (planoparktownhomes.com)

Today's TIER_4_LLM_API winner. Pulled `llm_report/11327.json` from `gs://jugnu-raw-production/runs/2026-05-10/shard_0/`:

```json
{
  "tier": "API_ANALYSIS",
  "raw_response": "{\"units\": [...], \"json_paths\": {...15 keys...}, \"response_envelope\": \"\"}",
  "cost_usd": 0.0093
}
```

This property had a NON-EMPTY `json_paths` and DID make it through all three gates. It's one of the 3 mapping entries in the DB. Most other LLM-API winners in today's run had `json_paths={}` (the LLM extracted units via semantic understanding without articulating per-field paths) and got dropped at Drop 1.

## Sample evidence — pids that produced mappings but lost them

From today's events.jsonl (per-shard `extract.tier_attempted` for `generic:llm_api_targeted` with `outcome="ran_units"`): **41 properties** completed LLM-API extraction. **3 survived** to the DB (~7%). The other ~38 silently lost their mappings to one of the three drops.

## What PR 1 must change

| Drop | File | Fix |
|---|---|---|
| Drop 1 (producer) | `ma_poc/services/llm_extractor.py` | When `is_noise=False` and units extracted, always populate `api_url_pattern` (and `response_envelope` if available) in `hints`, regardless of `json_paths` emptiness. |
| Drop 2 (surfacing) | `ma_poc/pms/adapters/generic.py` | Replace `mapping_subset.get("json_paths")` gate with `mapping_subset.get("api_url_pattern")` — surface degraded mappings into `llm_analysis_results`. |
| Drop 3 (persistence) | `ma_poc/services/profile_updater.py::save_llm_field_mapping` | Per the brief's Action 1: persist degraded mapping (envelope-only) when `json_paths` empty BUT `response_envelope` non-empty; record `quality_score=0.5`. Drop only when BOTH are empty. Wrap behind `ENABLE_DEGRADED_MAPPING_PERSIST` flag (default `true`) for kill-switch ability. |

## What PR 1 also adds

1. **`MAPPING_SAVE_DROPPED` event** — emitted on every `save_llm_field_mapping` False return with reason (`empty_pattern` / `empty_paths_and_envelope` / `disabled_by_flag`). Surfaces the drop count in the analyzer's named-fix table.
2. **`PROFILE_UPDATE_FAILED` event** — emitted in the runner's `try/except` around `update_profile_after_extraction` (currently swallowed at `log.debug`). Surfaces silent save failures.
3. **`STARTUP_PROBE_FAILED` event** — emitted when the sentinel round-trip probe fails at runner startup; runner exits non-zero.
4. **Sentinel round-trip probe** — at runner startup, write a `__sentinel__` profile carrying one entry in each writeable channel (mapping, blocked, known, dom_selector); read back; assert each channel round-trips. PG sync downstream is gated on the runner's exit code, so a probe failure prevents PG poisoning.
5. **Writer contract test** — `tests/services/test_profile_updater_writer_contract.py::test_full_writer_contract`. Synthesises a `scrape_result` exercising every writer in `update_profile_after_extraction`, calls the function, asserts every channel wrote. Initial scope: mappings + dom_selectors + blocked + known + explored. PR 2 extends with `field_patches`. PR 7 extends with `navigation_hint`. **Hard CI gate** — failing the test fails the build.
