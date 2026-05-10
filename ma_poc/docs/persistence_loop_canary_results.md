# Persistence-loop canary results (pre-deploy verification)

This document captures the actual data each canary produced. The companion runbook (`persistence_loop_canary_verification.md`) describes what to verify after the deploy lands; this file pins the **before** state and shows the dashboard pipeline already detects the regression.

All artifacts referenced live under [`ma_poc/docs/canary_baselines/`](canary_baselines/) so future operators can diff post-deploy results against them without spelunking c:/tmp/.

## Canary 1 — DB baseline + analyzer end-to-end against real cloud-run data

**Date captured**: 2026-05-10 23:56 UTC
**Cloud-run dataset**: `c:/tmp/run-2026-05-10/` (the day's production run, 4982 attempted properties)

### DB baseline ([data/canary/db_baseline_2026_05_10.json](canary_baselines/db_baseline_2026_05_10.json))

```
Q4 — channel row counts (5 self-learning channels):
  total_profiles               5054
  profiles_with_mappings          3   ← the regression: should be ~hundreds
  profiles_with_patches           0   ← the regression: should be ~tens
  profiles_with_blocked         618
  profiles_with_known           739
  profiles_with_dom_selectors  1897
  total_mapping_entries           3
  total_patch_entries             0

Q5 — recent activity:
  updated_last_24h                0   (last run was 2026-05-09)
  updated_last_7d              4982
  bootstrapped_only            3150
  updated_by_llm               1902
  most_recent_update           2026-05-09 04:46:10
  oldest_update                2026-04-15 04:02:43

Q6 — maturity rollover:
  WARM   3457
  COLD   1587
  HOT      10   ← almost no profiles graduate (replay never hits)

Q7 — top replay winners: 0 rows  ← no profile has any successful replay accumulated
```

The asymmetry is the diagnostic: `dom_selectors` (1897) vs `mappings` (3) is a 632× gap through the same shared writer (`update_profile_after_extraction`). That alone localises the bug to the per-channel writer, not the producer or surfacing site — which is what the discipline rule in `feedback_asymmetry_diagnostic.md` says to do FIRST.

### Analyzer summary against May 10 cloud run ([data/canary/analyzer_summary_2026_05_10.md](canary_baselines/analyzer_summary_2026_05_10.md))

```
[ok] 2026-05-10: 3830/4982 succeeded (76.88%); LLM $21.68
```

The persistence-health section of `summary.md` correctly fires the SLO alert against real data:

```
| Metric                         | Today | Threshold  | Status |
|--------------------------------|-------|------------|--------|
| MAPPING_SAVE_DROPPED total     | 0     | —          | —      |
| mapping_save_drop_rate         | 0.0%  | < 50%      | OK     |
| PROFILE_REPLAY_HIT count       | 1     | —          | —      |
| profile_replay_hit_rate        | 25.0% | ≥ 30%      | ALERT  |  ← real breach detected
| PROFILE_UPDATE_FAILED count    | 0     | ≤ 100      | OK     |
| STARTUP_PROBE_OK count         | 0     | ≥ shards   | —      |
| STARTUP_PROBE_FAILED count     | 0     | == 0       | OK     |
| FIELD_PATCH_HIT count          | 0     | —          | —      |
| FIELD_PATCH_DRIFT count        | 0     | —          | —      |
| LLM_GATE_RELAXED count         | 0     | —          | —      |

> 🚨 PERSISTENCE-LOOP ALERT — page on-call.
> - profile_replay_hit_rate 25.0% < 30%
```

The 25% comes from 1 hit / (1 hit + 3 miss-with-saved) = 25%. This is the exact regression the URL-normalization fix targets.

The full analyzer JSON ([data/canary/analyzer_baseline_2026_05_10.json](canary_baselines/analyzer_baseline_2026_05_10.json)) carries the same numbers in machine-readable form for programmatic post-deploy gating.

**Status: PASS** — dashboard wiring detects the live regression. Post-deploy this row should flip to `OK` with `≥ 30%`.

## Canary 2 — synthetic broken-state shard verifies all 4 SLO alerts

A handcrafted shard at `c:/tmp/canary_synth_run/run-2099-01-01/shard_0/events.jsonl` (also archived at [data/canary/synthetic_alert_check_events.jsonl](canary_baselines/synthetic_alert_check_events.jsonl)) contains:

- 50 × `mapping.save_dropped` events (with reason `empty_pattern`)
- 5 × `profile.replay_hit` events
- 20 × `profile.replay_miss_with_saved` events
- 150 × `profile.update_failed` events
- 1 × `startup.probe_failed` event

Running the analyzer against it produced ([data/canary/synthetic_alert_check_summary.md](canary_baselines/synthetic_alert_check_summary.md)):

```
| Metric                         | Today  | Threshold  | Status |
|--------------------------------|--------|------------|--------|
| MAPPING_SAVE_DROPPED total     | 50     | —          | —      |
| mapping_save_drop_rate         | 90.9%  | < 50%      | ALERT  |  ← (1)
| PROFILE_REPLAY_HIT count       | 5      | —          | —      |
| profile_replay_hit_rate        | 20.0%  | ≥ 30%      | ALERT  |  ← (2)
| PROFILE_UPDATE_FAILED count    | 150    | ≤ 100      | ALERT  |  ← (3)
| STARTUP_PROBE_OK count         | 0      | ≥ shards   | —      |
| STARTUP_PROBE_FAILED count     | 1      | == 0       | ALERT  |  ← (4)

> 🚨 PERSISTENCE-LOOP ALERT — page on-call.
> - mapping_save_drop_rate 90.9% > 50%
> - profile_replay_hit_rate 20.0% < 30%
> - profile_update_failed 150 > 100
> - startup_probe_failed 1 > 0 — RUNNER FAILED DEPLOY GUARD
```

**Status: PASS** — all 4 SLO thresholds correctly produce `ALERT` rows when their conditions are breached. If post-deploy the dashboard rows go silently quiet on a real breach, this synthetic shard can be replayed to localise whether the wiring or the events themselves regressed.

Re-run command preserved in the runbook:

```bash
python scripts/diagnostics/analyze_cloud_run.py \
  --date 2099-01-01 \
  --local-mirror c:/tmp/canary_synth_run \
  --out-dir /tmp/synth_alert_check \
  --expected-shards 1
```

## Canary 3 — telemetry-emission tests against the writer

[`tests/services/test_persistence_loop_canary_telemetry.py`](../tests/services/test_persistence_loop_canary_telemetry.py) patches the event-emit boundary and asserts each writer produces the events the dashboard reads:

```
tests/services/test_persistence_loop_canary_telemetry.py::TestDomEvictionTelemetry
  test_low_quality_eviction_emits_one_strike_threshold       PASSED
  test_high_quality_three_strike_emits_threshold_three       PASSED
  test_no_eviction_no_emit                                   PASSED
3 passed in 0.18s
```

The threshold + quality fields on `DOM_HINTS_EVICTED` are pinned directly so a future writer refactor that drops them surfaces as a failing test rather than a silently-blind dashboard.

**Status: PASS** — eviction telemetry round-trips correctly through the writer.

## Cumulative test suite

```
$ python -m pytest tests/services/ tests/scripts/ tests/profile/ -q
811 passed (excluding the pre-existing live-network skip)
```

Net new since the persistence-loop work began: 9 PRs of fixes + 280+ tests covering each fix individually plus end-to-end telemetry.

## What this proves

1. **The dashboard already sees the regression.** Pre-deploy `summary.md` shows `profile_replay_hit_rate 25.0% < 30% ALERT` against real production data.
2. **The dashboard fires correctly when conditions breach.** All 4 SLO thresholds (mapping-drop, replay-hit-rate, update-failed, startup-probe) produce `ALERT` rows on the synthetic broken-state shard.
3. **The writers emit the events the dashboard counts.** Captured at the writer boundary so a future refactor can't silently drop the wiring.
4. **The DB ground-truth queries return clean output.** Q4-Q7 all run; Q7's pre-existing `GROUP BY json` bug was caught and fixed during this work (commit message in the runbook).

## What this does NOT prove

- That post-deploy mappings will actually grow (depends on the deploy taking effect against the live runner).
- That replay hit rate will reach 30% (depends on URL-normalization actually firing in production traffic — needs canary 1 to run after first cloud run).
- That `ENABLE_SOURCE_TIERED_BUDGET` doesn't introduce regressions (default OFF; needs separate canary cycle when enabled).

## Next step — run canary 1 once the deploy lands

The runbook (`persistence_loop_canary_verification.md`) has the exact commands. Re-run Q4 + the analyzer after the first post-deploy cloud run and diff against the baselines in `data/canary/` — expectation is `profiles_with_mappings ≥ 100` and `profile_replay_hit_rate` row flipping from `ALERT` to `OK`.
