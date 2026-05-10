# Cloud run analysis — 2099-01-01

**Generated:** 2026-05-10T18:27:03+00:00
**Source:** `gs://jugnu-raw-production/runs/2099-01-01/`
**Shards seen:** 1 / 1

## Top-line numbers

- **Properties processed:** 0
- **SUCCESS:** 0 (0.0%)
- **FAILED_NO_DATA:** 0
- **FAILED_UNREACHABLE:** 0
- **FAILED (no `output.property_emitted` — likely killed by per-property timeout):** 0
- **LLM cost:** $0.00
- **SLO breaches:** 0 across all shards

## Persistence health (self-learning loop SLO)

Counted from per-shard `events.jsonl`. Cross-references the channel-by-channel DB row counts in `scripts/diagnostics/profile_persistence_health.sql` (run via `db_query.py`). When a row is **ALERT**, the runner is silently dropping or the loop has regressed — page someone.

| Metric | Today | Threshold | Status |
|---|---|---|---|
| `MAPPING_SAVE_DROPPED` total | 50 | — | — |
| `mapping_save_drop_rate` | 90.9% | < 50% | ALERT |
| `PROFILE_REPLAY_HIT` count | 5 | — | — |
| `profile_replay_hit_rate` | 20.0% | ≥ 30% | ALERT |
| `PROFILE_UPDATE_FAILED` count | 150 | ≤ 100 | ALERT |
| `STARTUP_PROBE_OK` count | 0 | ≥ shards_seen | — |
| `STARTUP_PROBE_FAILED` count | 1 | == 0 | ALERT |
| `FIELD_PATCH_HIT` count | 0 | — | — |
| `FIELD_PATCH_DRIFT` count | 0 | — | — |
| `LLM_GATE_RELAXED` count | 0 | — | — |

### MAPPING_SAVE_DROPPED reasons

| Reason | Count |
|---|---|
| `unknown` | 50 |

> **🚨 PERSISTENCE-LOOP ALERT — page on-call.**
> - mapping_save_drop_rate 90.9% > 50%
> - profile_replay_hit_rate 20.0% < 30%
> - profile_update_failed 150 > 100
> - startup_probe_failed 1 > 0 — RUNNER FAILED DEPLOY GUARD

## Failure breakdown by terminal tier

| # | Terminal tier | Count | % of failures |
|---|---|---|---|

## Fetch-side error signatures (first attempt per property)

| Outcome | Signature | Count |
|---|---|---|

## Tier distribution (succeed + fail)

| Tier | Count |
|---|---|

## Failure pattern distribution

| Pattern | Failures | What it means |
|---|---|---|
| P2 — Cloudflare on Entrata-style sites | 0 | CF challenge / captcha; rescue path doesn't fire |
| P3 — Generic `TIER_1_API` (no PMS adapter) | 0 | Cluster by management-company domain |
| P4 — Entrata adapter failure (non-CF) | 0 | Real adapter bug, not a fetch problem |
| P6 — Platform-specific adapter zero | 0 | AppFolio / OneSite / AMLI / Squarespace / Wix |
| P7 — Pure unreachable | 0 | `FAILED_UNREACHABLE` not already in P2 |
| P8 — LLM gate refused body | 0 | `LLM_GATE_NO_BODY` terminal |
| Pother | 0 | Anything else |

