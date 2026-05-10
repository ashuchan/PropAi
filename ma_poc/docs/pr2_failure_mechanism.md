# PR 2 — Failure Mechanism (Channel 4 / FieldPatch)

**Channel under repair:** `field_patches` (per-field deterministic-replay patches; sub-tier `generic:_apply_field_patches` at `pms/adapters/generic.py:1028`).

**Symptom:** DB across 5,054 profiles: **0 profiles with `field_patches`**. Same single function (`update_profile_after_extraction`) writes 5 channels; 3 work (blocked, known, dom_selectors), Channel 1 (mappings) is in repair via PR 1, Channel 4 has never persisted a single row.

## What I expected to find vs what's actually there

The brief named 4 gaps. Pre-impl diagnostic found that **only 2 of the 4 are real**.

### Gap 1 — `_run_null_field_recovery` discards its output

**Brief's claim:** function returns nothing; recovered patches are discarded as soon as it exits.

**Reality:** function mutates `scrape_result["_field_patches"]` via `scrape_result.setdefault("_field_patches", []).append(patch_dict)` at `scripts/runners/jugnu.py:1360, 1393-1400`. Patches DO survive past the function's exit on the same `scrape_result` dict.

**Status:** functionally not a gap on its own. It IS a gap in combination with Gap 2 (timing): patches accumulate on a dict that the consumer already finished processing.

### Gap 2 — `_field_patches` is unwired in `update_profile_after_extraction`

**Brief's claim:** the consumer doesn't read `_field_patches`.

**Reality:** **partially wrong**. The consumer DOES read `_field_patches` at `services/profile_updater.py:642-645`:

```python
patches_payload = scrape_result.get("_field_patches", []) or []
for patch_dict in patches_payload:
    if isinstance(patch_dict, dict):
        save_field_patch(profile, patch_dict)
```

The actual bug is **order of operations** in `scripts/runners/jugnu.py`:

| Line | Function | What runs |
|---|---|---|
| 670 | `_process_property` | `update_profile_after_extraction(profile, result, …)` — reads `result["_field_patches"]` (which is empty at this point) and persists patches (none) |
| 679 | `_process_property` | `profile_store.save(profile)` — saves the profile with no patches |
| 343 | `_process_one` (outer) | `_run_null_field_recovery(result, formatted, …)` — NOW mutates `result["_field_patches"]` with the recovered patches, but the profile was already saved |

The patches accumulate on `result["_field_patches"]` AFTER the consumer has already run. Net: zero patches ever reach `save_field_patch`.

**Status:** real bug. This is the dominant cause of `field_patches=0` in the DB.

### Gap 3 — JSONPath format mismatch between writer and reader

**Brief's claim:** writer strips `$.` (correct) but reader walks dot-segments without bracket-traversal.

**Reality:** **confirmed**. `pms/adapters/generic.py::_apply_field_patches::_get_path` (line 207-218):

```python
def _get_path(obj, path):
    for part in path.split("."):
        if isinstance(obj, dict):
            obj = obj.get(part)
        elif isinstance(obj, list) and part.isdigit():
            idx = int(part)
            obj = obj[idx] if idx < len(obj) else None
        else:
            return None
```

Only handles dot-and-numeric-segment notation: `data.units.0.rent`. Cannot resolve:
- `units[0].rent` (bracket index)
- `units[*].rent` (wildcard)
- `data.units[0].rent` (mixed)

The LLM produces canonical JSONPath syntax: `$.units[*].pricing.amount`. After the producer's `lstrip("$").lstrip(".")` at `jugnu.py:1384`, the path becomes `units[*].pricing.amount` — unresolvable by the reader.

**Status:** real bug. This is a REPLAY-side issue, not a save-side issue (save still succeeds with the LLM's path; replay silently fails). Fix needed in `_get_path` plus a shared `_normalize_json_path` helper used by writer (when stripping `$`) and reader (when traversing).

### Gap 4 — `raw_apis[0]` hardcoded in `_run_null_field_recovery`

**Brief's claim:** the LLM is asked with `_raw_api_responses[0]` regardless of which response actually contains the unit's data.

**Reality:** **already fixed**. `scripts/runners/jugnu.py:1333` calls `_resolve_source_url(raw_apis, unit)` per-unit, which finds the API response whose body contains the unit's identity tokens. The hardcode is gone. `source_url` and `source_body` reflect the per-unit lookup result.

**Status:** no work needed. Probably fixed in a prior PR.

## Sample evidence — pid 12727 (riverway / 3CM Multifamily)

Pulled `gs://jugnu-raw-production/runs/2026-05-10/shard_0/llm_diagnostics/12727_field_recovery.json`:

```json
{
  "property_id": "Riverway",
  "tier_used": "TIER_1_API",
  "recovered_fields": [
    {"field_name": "rent_low",   "recovered_value": null, "confidence": 0.0,  "source_path": "not_present"},
    {"field_name": "unit_id",    "recovered_value": "9a70cc18-9dec-…", "confidence": 0.95, "source_path": "$.uuid", "parser_fix": "item_lc.get('uuid')"},
    {"field_name": "beds",       "recovered_value": null, "confidence": 0.0,  "source_path": "not_present"},
    {"field_name": "area",       "recovered_value": -1,   "confidence": 0.0,  "source_path": "not_present"},
    {"field_name": "available_date", "recovered_value": null, "confidence": 0.0, "source_path": "not_present"}
  ],
  "cost_usd": 0.0005943
}
```

The `unit_id` recovery passes the producer's `confidence >= 0.85` gate AND has a non-empty `source_path` AND `unit_id` is in `_PATCH_FIELDS`. After `lstrip("$").lstrip(".")` the path becomes `uuid` (single segment — no bracket issue). save_field_patch SHOULD persist this.

DB query for pid 12727 returns `version=3, updated_by='BOOTSTRAP', updated_at='2026-05-09 04:19'` — **today's run did not save the profile at all**. PR 1's `PROFILE_UPDATE_FAILED` telemetry (deploying with this same branch) will surface why on the next cloud run.

But even if the profile WAS saved, the patch wouldn't have made it through because of Gap 2 (order of operations).

## What PR 2 must change

| Gap | File | Fix |
|---|---|---|
| Gap 2 (order of operations) | `scripts/runners/jugnu.py` | Move `_run_null_field_recovery` to run BEFORE `update_profile_after_extraction` inside `_process_property` (or re-run the update after recovery). Cleanest: hoist recovery into `_process_property` itself, immediately after `scrape_jugnu` returns and before the profile-update block. |
| Gap 3 (JSONPath bracket support) | `pms/adapters/generic.py::_apply_field_patches::_get_path` + new shared helper | Add `_normalize_json_path` that converts canonical JSONPath (`$.units[*].pricing.amount`) into a tokenisable form. Update `_get_path` to handle `[N]` (numeric index) and `[*]` (each-element). Replace producer's `lstrip("$").lstrip(".")` and writer's same call with the shared helper for consistency. |
| Sentinel probe extension | `services/profile_persistence_probe.py` | Add a `FieldPatch` to the sentinel + an assertion in `_assert_round_trip` so future Pydantic drift on FieldPatch is caught at deploy. |
| Writer contract test extension | `tests/services/test_profile_updater_writer_contract.py` | Add `_field_patches` to the synthesized fixture + assert `len(p.api_hints.field_patches) > 0`. Future channel-removal regressions fail CI. |

## What PR 2 will NOT do

- No FieldPatch model schema changes — the `Literal[...]` field_name list already covers everything `_PATCH_FIELDS` produces.
- No changes to the producer `_run_null_field_recovery` body — the function works correctly within its current scope. Only its call site moves.
- No backfill — saved patches start accumulating from PR 2's deploy onward. PR 4 (backfill) covers older `_llm_analysis_results` / `raw_api/` captures retroactively.
- No `_envelope_hash` re-design — the existing import + propagation works.

## Tests

1. Extend `test_full_writer_contract` to cover `field_patches`.
2. New `test_pr2_field_patch_persistence.py`:
   - Producer-side: simulating a `_run_null_field_recovery` call with a high-confidence recovery produces the expected patch_dict shape.
   - JSONPath normaliser: verify `[N]`, `[*]`, and `data.units.0.rent` all walk correctly.
   - Reader: `_apply_field_patches` resolves bracket-notation paths.
3. Order-of-operations regression: simulate the production call sequence and assert patches reach `save_field_patch`.

## Canary-1 success criterion (revised for PR 2)

`n_field_patches`: 0 → ≥50 across the 5,054 profiles after the next cloud run.

If `n_field_patches=0` post-deploy: profile-update is silently failing for the recovery-eligible properties. PR 1's `PROFILE_UPDATE_FAILED` telemetry will be the diagnostic.
