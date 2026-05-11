# Cloud-run analysis — 2026-05-10

**Author:** Hand-written report based on GCS artifacts (not the auto-generated `summary.md`).
**Run analysed:** `jugnu-scrape-production-vnl6m`, started 2026-05-10 19:55 UTC, finished 20:41 UTC.
**Source artefacts:** `gs://jugnu-raw-production/runs/2026-05-10/` (all 20 shards, retrieved via ADC + REST).
**Auto reports for the same run:** [`ma_poc/data/reports/cloud_run_2026-05-10/`](../data/reports/cloud_run_2026-05-10/) (gitignored locally; regenerable any time with `analyze_cloud_run --date 2026-05-10`).

---

## TL;DR

| Signal | Value | What it tells us |
|---|---|---|
| Success rate | **90.43 %** (4505 / 4982) | +12.13 pp vs 2026-05-09 (78.30 %). Highest in 3 days. |
| `PROFILE_UPDATE_FAILED` | **245** | SLO threshold 100 — **ALERT**. The "DB sync had failed" symptom. |
| `profile_replay_hit_rate` | **0.0 %** | SLO threshold 30 % — **ALERT**. No saved-mapping replays fired. |
| `STARTUP_PROBE_OK` shards | 13 / 20 | 7 shards didn't emit the deploy-guard OK. Worth tracking. |
| Regressions vs yesterday | 288 | Properties that passed 2026-05-09 and failed 2026-05-10. |
| Recoveries vs yesterday | 147 | Failed yesterday → passed today. |
| LLM spend | $20.25 | +$2.73 vs 2026-05-09 (cache miss tax — see Bug 1 below). |

Headline: **the scrape side recovered strongly**, but **the persistence side is still bleeding** at a known, fixed code path. The fix is already on `origin/main` (commit `639ccc3`) and waiting for the next cloud-run to execute.

---

## Bug 1 — `ApiEndpoint.json_paths` Pydantic validation crashes 245 profile updates

### Mechanism

Cloud Logging trace (1 of 245 identical occurrences):

```
2026-05-10 20:33:04 task=5 jugnu_runner WARNING: profile update failed for 244274:
  2 validation errors for ApiEndpoint
  json_paths.rent
    Input should be a valid string [type=string_type, input_value=None, input_type=NoneType]
  json_paths.unit_id
    Input should be a valid string [type=string_type, input_value=None, input_type=NoneType]
Traceback:
  /app/ma_poc/scripts/runners/jugnu.py:667  in _process_property
  /app/ma_poc/services/profile_updater.py:639  in update_profile_after_extraction
  ApiEndpoint(json_paths={"rent": None, ...})  →  ValidationError
```

The LLM emits `null` for any field it could not map (a documented contract in `config/prompts/api_analysis.txt`). `ApiEndpoint.json_paths` is typed `dict[str, str]` strict; a single `null` crashes the Pydantic constructor. The runner's `_process_property` catches the exception as a *warning* and continues, so the property's scrape result is still emitted, but its **profile state is never written to FS**. `sync_run_to_pg::_copy_profiles` then ships only the profiles that DID write — silently missing 245 daily updates in Cloud SQL.

This is exactly the same Pydantic shape the earlier persistence-loop PR series caught for `LlmFieldMapping.json_paths` (filtered in `backfill_persistence.py` and the writer); the `ApiEndpoint` writer site was not covered.

### Per-shard distribution

All 20 shards had failures, range 11–24, mean ~17 — uniform random across shards (this is a data-driven bug, not infra).

```
shard_0:  19   shard_5:  13   shard_10: 22   shard_15: 11
shard_1:  24   shard_6:  12   shard_11: 17   shard_16: 19
shard_2:  18   shard_7:  11   shard_12: 20   shard_17: 17
shard_3:  16   shard_8:  21   shard_13: 14   shard_18: 16
shard_4:  15   shard_9:  19   shard_14: 16   shard_19: 17
                                                       Total 337 traceback lines / 245 distinct properties
```

(Cloud Logging counts log *lines*, not distinct events; the analyzer's `PROFILE_UPDATE_FAILED` = 245 is the true per-property count.)

### Fix landing state

| Commit | Description | Status |
|---|---|---|
| `639ccc3` | `fixed db write error, bumped resources` — adds `clean_string_value_dict` filter at four writer sites + 286-line test in `test_json_paths_null_value_filtering.py` | **On `origin/main` as of 2026-05-11 03:34 IST.** Not yet executed in a cloud run. |

Code changes the fix applies (compact):

- `services/llm_extractor.py` — `clean_string_value_dict(d) -> dict[str, str]` helper; applied inside `extract_with_llm` and `analyze_api_with_llm` so cached on-disk hints are clean.
- `services/profile_updater.py` — applied at `save_llm_field_mapping` (line 348) AND at the crashing call site (line 646) before the `ApiEndpoint(...)` construction.

### Expected impact next run

| Metric | Now | After fix executes once |
|---|---|---|
| `PROFILE_UPDATE_FAILED` | 245 | ≤ 30 (residual transient flake; SLO is ≤ 100) |
| Profiles missing daily update | 245 | ~0 |
| Day-over-day regressions | 288 | Should drop sharply (a chunk of the 288 are downstream of stale profiles) |

---

## Bug 2 — `profile_replay_hit_rate` is 0 %, with only 5 attempts

The analyzer counted 0 `PROFILE_REPLAY_HIT` and 5 `PROFILE_REPLAY_MISS_WITH_SAVED` events across the whole run. The replay path is barely *attempted*, let alone hitting.

Two known contributors here (both addressed by prior PRs that have already landed):

1. **URL-pattern drift kills substring matching.** Saved patterns contain rotated query params (`?api_key=…`, session tokens). The post-PR-5 normaliser collapses both sides to `host/path`. If the persisted patterns predate `639ccc3`, they're still in raw form; the matcher normalises at read time, so they DO match, but the cache only has ~3 saved mappings DB-wide (per pre-deploy `Q4_channel_row_counts`) — the cache is thin because Bug 1 has been preventing writes.
2. **Bug 1 has been silently emptying the cache.** Each daily run that should have added ~50–100 new mappings was instead adding ≤5, because the writer crashed on the LLM's `null` values mid-call.

These are linked: fix Bug 1 → cache fills → `profile_replay_hit_rate` climbs over the next 2–3 daily runs.

---

## Bug 3 — `STARTUP_PROBE_OK` count is 13 / 20

The sentinel round-trip probe should emit once per shard at runner startup. Only 13 of 20 shards emitted it. Three plausible causes:

- `ENABLE_PERSISTENCE_PROBE` flag is unset / `false` in 7 shards' env (most likely — Cloud Run env is per-task identical so this would be all-or-nothing; rule out).
- 7 shards started, hit a fast crash *before* the probe site (unlikely — Cloud Run reported all 20 succeeded).
- Probe code path silently swallowed an exception in 7 shards (defensive `except Exception: pass` somewhere).

Worth investigating if the count doesn't reach 20 on the next run. Not load-bearing for today's data.

---

## Day-over-day movement

Full diff in [`comparison_with_2026-05-09.md`](../data/reports/cloud_run_2026-05-10/comparison_with_2026-05-09.md). Headline numbers:

|  | 2026-05-10 | 2026-05-09 | Δ |
|---|---|---|---|
| Succeeded | 4505 | 3901 | **+604** |
| Failed (no data) | 1082 | 962 | +120 |
| Failed (unreachable) | 132 | 109 | +23 |
| Failed (no emit / timeout-kill) | 0 | 10 | −10 |
| Success rate | 90.43 % | 78.30 % | **+12.13 pp** |
| LLM cost | $20.25 | $17.52 | +$2.73 |
| Shards | 20 | 20 | 0 |

The +12.13 pp lift looks like the persistence-loop PR series (1–9) producing real effect at the scrape layer — DOM-hint persistence + URL normalization + JSONPath bracket walker landing. The cost increase is from re-LLMing the 245 properties whose persistence is broken (Bug 1).

Failure-membership flow:
- **Regressions (passed yesterday → failed today): 288** — most likely candidates: the 245 PROFILE_UPDATE_FAILED properties whose stale profile state caused replay misses today.
- **Recoveries (failed yesterday → passed today): 147**.
- **Repeat failures: 923** — the long tail (Squarespace / Wix syndication-only, captcha-blocked, etc.).
- New / dropped: 0 / 0 (input list unchanged).

---

## Side find — analyzer was crashing every invocation

`scripts/diagnostics/analyze_cloud_run.py::write_outputs` calls `render_failures_csv(...)`, but commit `f11b6dc` (the canary regression-basket feature) had **renamed** the function to `render_successes_csv` without updating the caller. Every analyzer invocation crashed with `NameError: render_failures_csv is not defined` after writing `summary.md`.

**Restored** as part of this analysis — `render_failures_csv` definition added back at line 722, both helpers now coexist. failures.csv + successes.csv are both written. Commit message suggestion: `analyze_cloud_run: restore render_failures_csv (regression from f11b6dc)`.

---

## What to verify after the next cloud run executes

The fix is in place but the next run hasn't fired yet (no 2026-05-11 prefix on GCS at time of writing — 2026-05-11 04:00 IST). When it does:

```bash
# 1. Re-run the analyzer day-over-day
python scripts/diagnostics/analyze_cloud_run.py \
  --date 2026-05-11 --compare-date 2026-05-10

# 2. Inspect the persistence-health row in the new summary
open data/reports/cloud_run_2026-05-11/summary.md
# Expected: PROFILE_UPDATE_FAILED row flips from ALERT (245) → OK (≤100, ideally near 0)

# 3. Cross-check DB via cloud-sql-proxy + diagnostic SQL
"C:/Users/ashus/bin/cloud-sql-proxy.exe" --port 5433 --auto-iam-authn \
  jugnu-494013:us-central1:jugnu-db-production &
DATABASE_URL='postgresql+pg8000://ashu%40surgexdigital.com@127.0.0.1:5433/jugnu' \
  python scripts/diagnostics/db_query.py \
    scripts/diagnostics/profile_persistence_health.sql \
    --query Q4_channel_row_counts
# Expected: profiles_with_mappings climbs from baseline 3 toward 50-100
```

If `PROFILE_UPDATE_FAILED` is still > 100 next run, the fix didn't deploy or there's a second crash path the JSONPath null-filter didn't cover. The Cloud Logging trace at `services/profile_updater.py:639` would still be there; pull a fresh sample and triage against `639ccc3`.

---

## Cross-references

- Auto-generated summary: [`data/reports/cloud_run_2026-05-10/summary.md`](../data/reports/cloud_run_2026-05-10/summary.md)
- Day-over-day diff: [`data/reports/cloud_run_2026-05-10/comparison_with_2026-05-09.md`](../data/reports/cloud_run_2026-05-10/comparison_with_2026-05-09.md)
- Earlier self-learning-loop analysis (now superseded by this run's improvements): [`data/reports/cloud_run_2026-05-10/SELF_LEARNING_LOOP_REGRESSION.md`](../data/reports/cloud_run_2026-05-10/SELF_LEARNING_LOOP_REGRESSION.md)
- Persistence-health SLO source: [`scripts/diagnostics/profile_persistence_health.sql`](../scripts/diagnostics/profile_persistence_health.sql)
- Cloud-side canary verification runbook: [`docs/persistence_loop_canary_verification.md`](persistence_loop_canary_verification.md)
- The fix commit: `git show 639ccc3`
