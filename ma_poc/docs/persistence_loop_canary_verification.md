# Persistence-loop canary verification

Run-once-after-each-deploy checklist for the persistence-loop fix series. Treat each milestone as a hard gate: if any "stop condition" fires, halt the rollout and read the linked failure-mechanism doc before proceeding.

## Pre-deploy baseline (captured 2026-05-10 23:56 UTC)

Archived in the repo at [`ma_poc/docs/canary_baselines/`](canary_baselines/):

- [`db_baseline_2026_05_10.json`](canary_baselines/db_baseline_2026_05_10.json) — all 4 named queries from `profile_persistence_health.sql` against the live cloud DB.
- [`analyzer_baseline_2026_05_10.json`](canary_baselines/analyzer_baseline_2026_05_10.json) — analyzer's `summary.json` against the 2026-05-10 cloud run.
- [`analyzer_summary_2026_05_10.md`](canary_baselines/analyzer_summary_2026_05_10.md) — same analyzer's markdown report (human-readable).
- [`synthetic_alert_check_events.jsonl`](canary_baselines/synthetic_alert_check_events.jsonl) + [`synthetic_alert_check_summary.md`](canary_baselines/synthetic_alert_check_summary.md) — handcrafted broken-state shard + analyzer output proving all 4 SLO alerts fire.

A walk-through of these baselines and what they prove lives in [`persistence_loop_canary_results.md`](persistence_loop_canary_results.md).

Key numbers to compare against:

| Metric | Pre-deploy value |
|---|---|
| `total_profiles` | 5054 |
| `profiles_with_mappings` | 3 |
| `profiles_with_patches` | 0 |
| `profiles_with_blocked` | 618 |
| `profiles_with_known` | 739 |
| `profiles_with_dom_selectors` | 1897 |
| `profile_replay_hits` (per run) | 1 |
| `profile_replay_miss_with_saved` (per run) | 3 |
| `profile_replay_hit_rate` (per run) | 25.0% — **ALERT** |
| `mapping_save_dropped` total (per run) | 0 (the silent-drop bug — events weren't even emitted pre-PR-1) |
| `updated_last_24h` | 0 |
| `HOT` profiles | 10 |

## Canary 1 — first run after deploy

**When**: first `jugnu_runner` cloud run after the deploy lands.

**Verify**:

```bash
# 1. Re-run Q4. Expect mappings_with > 100 and patches_with > 50.
python scripts/diagnostics/db_query.py \
  scripts/diagnostics/profile_persistence_health.sql \
  --query Q4_channel_row_counts

# 2. Re-run analyzer with --check-db so the report includes both
#    event-side and DB-side counters in the same summary.
python scripts/diagnostics/analyze_cloud_run.py \
  --date $(date -u +%Y-%m-%d) --check-db

# 3. Inspect summary.md "Persistence health" section. Expected status:
#    - mapping_save_drop_rate    < 50%     → OK
#    - profile_replay_hit_rate   ≥ 30%     → OK   (KEY: the URL-normalization fix proves out here)
#    - PROFILE_UPDATE_FAILED     ≤ 100     → OK
#    - STARTUP_PROBE_FAILED      == 0      → OK
```

**Stop conditions** (halt rollout, do not proceed to canary 2):

- `profiles_with_mappings` did NOT grow vs baseline → writer hardening regressed; read `docs/persistence_hardening.md`
- `MAPPING_SAVE_DROPPED` events absent from analyzer output → instrumentation broken, dashboard blind
- `STARTUP_PROBE_FAILED > 0` → strict zero. PG was about to be poisoned and the runner correctly aborted; investigate the probe error before any further deploy

**Recovery**: if mappings count stays flat, run the backfill dry-run to validate that the artifacts contain extractable mappings:

```bash
python scripts/diagnostics/backfill_persistence.py --run-date $(date -u +%Y-%m-%d)
```

If the dry-run shows mappings would persist but the live runner persisted 0, the writer is broken (not the backfill).

## Canary 2 — third daily run after deploy

**When**: 3 daily runs after the first canary (so ~96 hours post-deploy).

**Verify**:

```bash
# 1. Cumulative DB growth.
python scripts/diagnostics/db_query.py \
  scripts/diagnostics/profile_persistence_health.sql \
  --query Q4_channel_row_counts

# 2. Replay-winners — Q7 should have non-zero rows now (was 0 pre-deploy).
python scripts/diagnostics/db_query.py \
  scripts/diagnostics/profile_persistence_health.sql \
  --query Q7_top_replay_winners

# 3. Maturity rollover.
python scripts/diagnostics/db_query.py \
  scripts/diagnostics/profile_persistence_health.sql \
  --query Q6_maturity_distribution
```

**Pass criteria**:

- `profiles_with_mappings ≥ 500` (vs 3 pre-deploy)
- `Q7_top_replay_winners` returns > 0 rows
- `HOT` profile count grows (from 10 toward ~50+ as the cascade short-circuits more often)
- Synthetic-drop telemetry test still produces an ALERT row when run against the synthesized broken-state shard at `c:/tmp/canary_synth_run/run-2099-01-01/` (verifies the alert path didn't silently break)

**Stop conditions**:

- `profiles_with_mappings < 100` → cache isn't building, writer is regressed or the URL-normalization fix didn't take effect
- `profile_replay_hit_rate < 30%` → URL drift remains; investigate the saved-pattern shapes via Q7 and read `docs/url_pattern_normalization.md`

## Canary 3 — one week after deploy

**When**: 7 daily runs after the first canary.

**Verify**:

```bash
# 1. Final growth check.
python scripts/diagnostics/db_query.py \
  scripts/diagnostics/profile_persistence_health.sql

# 2. DOM-selector retention — count of profiles with non-empty
#    dom_hints.field_selectors.container that have NOT been evicted.
#    The PR-6 + PR-8 quality-tiered eviction policy should retain
#    ≥60% of saved selectors after a week (the rest evict naturally
#    via consecutive misses).
```

**Pass criteria**:

- `profile_replay_hit_rate ≥ 30%` sustained across the week (compute from analyzer summary.json across 7 daily runs)
- DOM-selector retention ≥ 60% (saved-vs-still-present ratio)
- `profile_update_failed ≤ 100` per run sustained
- Backfill report (if re-run for the week) shows ~50–100 new mappings/day when invoked retrospectively

**Stop conditions**:

- DOM-selector retention < 30% → quality-tiered eviction is too aggressive; consider lifting the boundary or extending the resilience tier. Read `docs/dom_hint_quality_tiered_eviction.md`
- `profile_replay_hit_rate` regresses week-over-week → URL drift returning. Investigate via the Sweetwater-FL real-data pattern in `docs/url_pattern_normalization.md`

## Verifying the alert pipeline still fires

Synthetic-broken-state shard already lives at `c:/tmp/canary_synth_run/run-2099-01-01/shard_0/events.jsonl` (50 mapping-drops, 1 startup-probe-failure, 5 hits / 20 miss-with-saved).

Re-run the analyzer against it any time the dashboard wiring changes:

```bash
python scripts/diagnostics/analyze_cloud_run.py \
  --date 2099-01-01 \
  --local-mirror c:/tmp/canary_synth_run \
  --out-dir /tmp/synth_alert_check \
  --expected-shards 1
```

Expected output: 4 `ALERT` rows in the markdown's persistence-health table. If any of the four go missing, the alert wiring regressed in the analyzer.

## Programmatic post-deploy gate

For unattended rollout — extract the single boolean from `summary.json`:

```bash
python -c "
import json, sys
data = json.load(open(sys.argv[1]))
ph = data.get('persistence_health', {})
hits = ph.get('profile_replay_hits', 0)
misses = ph.get('profile_replay_miss_with_saved', 0)
total = hits + misses
rate = hits / total if total else 0.0
ok = (
    ph.get('mapping_save_dropped', {}) is not None  # block emitted, not necessarily empty
    and rate >= 0.30
    and ph.get('profile_update_failed', 0) <= 100
    and ph.get('startup_probe_failed', 0) == 0
)
print('PASS' if ok else 'FAIL')
sys.exit(0 if ok else 1)
" /tmp/today_summary.json
```

Wire this into the rollout pipeline as a post-deploy gate that blocks promotion to subsequent canary stages until it returns 0.

## What the canary verifies vs. what it doesn't

**Verifies** (high confidence after passing all three):
- The persistence layer is writing mappings + patches at the expected rate
- The replay matcher is hitting saved entries (URL-normalization fix took effect)
- The eviction policy retains validated selectors and discards bad ones
- The telemetry pipeline emits the events the dashboard relies on
- All four SLO thresholds in the analyzer can detect a real breach

**Does NOT verify** (out of scope; needs separate rollout):
- The deferred source-tiered budget (`ENABLE_SOURCE_TIERED_BUDGET=true`) — flag-gated OFF by default, must canary independently before enabling
- Cross-property cluster-aware learning
- Vision tier behavior under the new budgets

## When something fails

Refer to the diagnostic playbook (`feedback_diagnostic_playbook.md` in the agent memory) for the step-by-step recipe: always run Q4 first, never form pipeline-level hypotheses before checking writer-side counts.
