# Cloud run analysis — 2026-05-10

**Generated:** 2026-05-10T18:24:57+00:00
**Source:** `gs://jugnu-raw-production/runs/2026-05-10/`
**Shards seen:** 20 / 20

## Top-line numbers

- **Properties processed:** 4982
- **SUCCESS:** 3830 (76.88%)
- **FAILED_NO_DATA:** 1031
- **FAILED_UNREACHABLE:** 113
- **FAILED (no `output.property_emitted` — likely killed by per-property timeout):** 8
- **LLM cost:** $21.68
- **SLO breaches:** 34 across all shards

## Persistence health (self-learning loop SLO)

Counted from per-shard `events.jsonl`. Cross-references the channel-by-channel DB row counts in `scripts/diagnostics/profile_persistence_health.sql` (run via `db_query.py`). When a row is **ALERT**, the runner is silently dropping or the loop has regressed — page someone.

| Metric | Today | Threshold | Status |
|---|---|---|---|
| `MAPPING_SAVE_DROPPED` total | 0 | — | — |
| `mapping_save_drop_rate` | 0.0% | < 50% | OK |
| `PROFILE_REPLAY_HIT` count | 1 | — | — |
| `profile_replay_hit_rate` | 25.0% | ≥ 30% | ALERT |
| `PROFILE_UPDATE_FAILED` count | 0 | ≤ 100 | OK |
| `STARTUP_PROBE_OK` count | 0 | ≥ shards_seen | — |
| `STARTUP_PROBE_FAILED` count | 0 | == 0 | OK |
| `FIELD_PATCH_HIT` count | 0 | — | — |
| `FIELD_PATCH_DRIFT` count | 0 | — | — |
| `LLM_GATE_RELAXED` count | 0 | — | — |

> **🚨 PERSISTENCE-LOOP ALERT — page on-call.**
> - profile_replay_hit_rate 25.0% < 30%

## Failure breakdown by terminal tier

| # | Terminal tier | Count | % of failures |
|---|---|---|---|
| 1 | `TIER_1_API` | 648 | 56.6% |
| 2 | `TIER_1_API_ENTRATA` | 245 | 21.4% |
| 3 | `__no_extraction__` | 113 | 9.9% |
| 4 | `TIER_1_API_ONESITE` | 45 | 3.9% |
| 5 | `TIER_1_API_APPFOLIO` | 32 | 2.8% |
| 6 | `LLM_GATE_NO_BODY` | 28 | 2.4% |
| 7 | `SYNDICATION_ONLY_SQUARESPACE` | 18 | 1.6% |
| 8 | `SYNDICATION_ONLY_WIX` | 11 | 1.0% |
| 9 | `TIER_1_API_AMLI_NEXT_DATA` | 4 | 0.3% |

## Fetch-side error signatures (first attempt per property)

| Outcome | Signature | Count |
|---|---|---|
| OK | (none) | 4861 |
| TRANSIENT | Error | 28 |
| RATE_LIMITED | HTTP_429 | 28 |
| BOT_BLOCKED | CF_CHALLENGE | 24 |
| BOT_BLOCKED | HTTP_403 | 10 |
| TRANSIENT | timeout | 8 |
| HARD_FAIL | HTTP_404 | 7 |
| OK | TIMEOUT_SALVAGED | 7 |
| BOT_BLOCKED | BOT_BLOCKED | 4 |
| HARD_FAIL | HTTP_401 | 1 |
| TRANSIENT | HTTP_500 | 1 |
| HARD_FAIL | HTTP_409 | 1 |

## Tier distribution (succeed + fail)

| Tier | Count |
|---|---|
| `TIER_3_DOM` | 1377 |
| `TIER_1_API` | 929 |
| `TIER_MERGED_CROSS_PAGE` | 860 |
| `TIER_4_LLM` | 348 |
| `TIER_4_LLM_DOM` | 336 |
| `TIER_1_API_ENTRATA` | 332 |
| `TIER_1_API_SIGHTMAP` | 286 |
| `TIER_1_API_ONESITE` | 93 |
| `TIER_1_API_APPFOLIO` | 52 |
| `TIER_1_API_RENTCAFE` | 48 |
| `LLM_GATE_NO_BODY` | 43 |
| `TIER_4_LLM_API` | 37 |
| `TIER_1_API_AVALONBAY` | 22 |
| `TIER_1_5_EMBEDDED` | 22 |
| `SYNDICATION_ONLY_SQUARESPACE` | 18 |
| `TIER_1_API_APPFOLIO_LLM_RESCUE` | 17 |
| `TIER_2_JSONLD` | 16 |
| `SYNDICATION_ONLY_WIX` | 11 |
| `FAILED` | 8 |
| `TIER_1_API_AMLI_NEXT_DATA` | 4 |
| `TIER_1_API_ENTRATA_LLM_RESCUE` | 3 |
| `TIER_1_DOM_APPFOLIO_SSR` | 3 |
| `TIER_1_API_LLM_RESCUE` | 3 |
| `TIER_1_PROFILE_MAPPING` | 1 |

## Failure pattern distribution

| Pattern | Failures | What it means |
|---|---|---|
| P2 — Cloudflare on Entrata-style sites | 41 | CF challenge / captcha; rescue path doesn't fire |
| P3 — Generic `TIER_1_API` (no PMS adapter) | 648 | Cluster by management-company domain |
| P4 — Entrata adapter failure (non-CF) | 244 | Real adapter bug, not a fetch problem |
| P6 — Platform-specific adapter zero | 110 | AppFolio / OneSite / AMLI / Squarespace / Wix |
| P7 — Pure unreachable | 73 | `FAILED_UNREACHABLE` not already in P2 |
| P8 — LLM gate refused body | 28 | `LLM_GATE_NO_BODY` terminal |
| Pother | 0 | Anything else |

## SLO breaches

| Shard | Metric | Threshold | Observed |
|---|---|---|---|
| shard_0 | success_rate | 0.95 | 0.82 |
| shard_0 | llm_cost_per_run | 1.0 | 1.1075 |
| shard_1 | success_rate | 0.95 | 0.768 |
| shard_1 | llm_cost_per_run | 1.0 | 1.2561 |
| shard_2 | success_rate | 0.95 | 0.732 |
| shard_2 | llm_cost_per_run | 1.0 | 1.0821 |
| shard_3 | success_rate | 0.95 | 0.784 |
| shard_4 | success_rate | 0.95 | 0.752 |
| shard_5 | success_rate | 0.95 | 0.776 |
| shard_5 | llm_cost_per_run | 1.0 | 1.0331 |
| shard_6 | success_rate | 0.95 | 0.648 |
| shard_6 | llm_cost_per_run | 1.0 | 1.0921 |
| shard_7 | success_rate | 0.95 | 0.82 |
| shard_8 | success_rate | 0.95 | 0.808 |
| shard_8 | llm_cost_per_run | 1.0 | 1.1109 |
| shard_9 | success_rate | 0.95 | 0.8 |
| shard_10 | success_rate | 0.95 | 0.804 |
| shard_10 | llm_cost_per_run | 1.0 | 1.3274 |
| shard_11 | success_rate | 0.95 | 0.784 |
| shard_11 | llm_cost_per_run | 1.0 | 1.1657 |
| shard_12 | success_rate | 0.95 | 0.796 |
| shard_12 | llm_cost_per_run | 1.0 | 1.1472 |
| shard_13 | success_rate | 0.95 | 0.76 |
| shard_13 | llm_cost_per_run | 1.0 | 1.0111 |
| shard_14 | success_rate | 0.95 | 0.752 |
| shard_14 | llm_cost_per_run | 1.0 | 1.2059 |
| shard_15 | success_rate | 0.95 | 0.784 |
| shard_16 | success_rate | 0.95 | 0.732 |
| shard_16 | llm_cost_per_run | 1.0 | 1.049 |
| shard_17 | success_rate | 0.95 | 0.74 |
| shard_17 | llm_cost_per_run | 1.0 | 1.2579 |
| shard_18 | success_rate | 0.95 | 0.78 |
| shard_19 | success_rate | 0.95 | 0.7328 |
| shard_19 | llm_cost_per_run | 1.0 | 1.1108 |

## Pattern 3 — Generic TIER_1_API failures by management-company domain

| Domain | Failures |
|---|---|
| equityapartments.com | 13 |
| gscapts.com | 9 |
| krcapartments.com | 8 |
| rentanapt.com | 5 |
| arizona.weidner.com | 4 |
| eaglerockproperties.com | 3 |
| fmgnj.com | 3 |
| keystonemanagement.com | 3 |
| southwoodrealty.com | 3 |
| springsapartments.com | 3 |
| landcoapartments.com | 2 |
| evergreenatriveroaks.com | 2 |
| alapts.com | 2 |
| broadmoor.cc | 2 |
| richmansignature.com | 2 |
| minnesota.weidner.com | 2 |
| theapartmentgallery.com | 2 |
| brandywinecommunities.com | 2 |
| missionrockresidential.com | 2 |
| edwardrose.com | 2 |
| sentral.com | 2 |
| casadearroyoapts.com | 1 |
| retreatwestminstercenter.com | 1 |
| riverwalkdallas.com | 1 |
| northbrookandpinebrookridgeland.com | 1 |

## Pattern 6 — Platform-specific adapter failures

| Platform | Failures |
|---|---|
| platform_onesite | 45 |
| platform_appfolio | 32 |
| platform_squarespace | 18 |
| platform_wix | 11 |
| platform_amli | 4 |

## Pattern 10 — UNITS_KEYLESS_HIGH warnings: 335 properties

Quality warnings (not failures). Indicates LLM-extracted units lacked a natural identity anchor.

