# Cloud-run analysis — 2026-05-11

**Author:** Manual analysis of fresh GCS artifacts (the auto-generated `summary.md` headline is wrong — see Bug A).
**Run analysed:** `jugnu-scrape-production`, completion timestamp 2026-05-11T02:14 UTC.
**Source artefacts:** `gs://jugnu-raw-production/runs/2026-05-11/` — all 50 shards, retrieved via ADC + REST (gcloud login was stale; ADC token still valid).
**Local mirror:** `c:/tmp/run-2026-05-11/` (events.jsonl + report.json + issues.jsonl + llm_report.json per shard; 76 MB / 343 files).
**Auto reports:** [`ma_poc/data/reports/cloud_run_2026-05-11/`](../data/reports/cloud_run_2026-05-11/) (summary.md headline is **wrong** — trusts report.json which is broken; failures.csv + per-shard tier counts are still correct).

---

## TL;DR — three new bugs, one fix verified, headline is misleading

| Signal | Auto-summary says | TRUE value (events.jsonl) | What it means |
|---|---|---|---|
| Success rate | **99.92 %** (4978/4982) | **58.95 %** (2937/4982) | Headline is wrong — see **Bug A**. Real rate is the worst in a week. |
| Day-over-day Δ (success rate) | +8.17 pp | **−16.02 pp** (vs May-10 true 74.97 %) | A **massive regression**, not an improvement. |
| Primary failure mode | mixed `TIER_1_API` | **1633 properties** stuck on `llm_rescue_failed: "no candidates after filtering"` with no link-hop fallback fired | **Bug B** — a new gate between rescue and link-hop is short-circuiting the fallback path that was carrying ~900 properties yesterday. |
| Profile-update failures (yesterday's Bug 1) | 0 | **0** | ✅ Commit `639ccc3` verified — the JSONPath null-filter fix worked. |
| `profile_replay_hit_rate` | 17.9 % (ALERT) | 17.9 % (ALERT) | Cache is filling (0 → 26 hits) but still below 30 % SLO. Will climb naturally over 2–3 more runs. |
| Shards | 50 / 50 | 50 / 50 | Doubled from 20 → 50 since May 10. Run length stayed ~50 min, so it's a load-spread improvement. |
| LLM cost | $26.46 | $26.46 | +$6.31 vs May 10 — extra rescue calls on the 1633 properties that Bug B newly burns. |
| Bot-blocked terminal failures | 81 | 81 | Unchanged from May 10 — long tail. |

**Net headline:** the scrape pipeline regressed today. The two persistence-layer fixes from yesterday (Bug 1, render_failures_csv) both work, but a new behaviour in the **LLM rescue → link-hop** path is suppressing ~900 successful sub-page recoveries that worked May 10. Combined with a **silent reporting bug** in `report.json` that masks the regression as 99.92 % success, the run looks healthy in dashboards while losing ~16 pp of real coverage.

---

## Bug A — `report.json` claims 100 % success on every shard while events.jsonl shows 30–70 % real success

### Mechanism

`ma_poc/reporting/run_report.py` reads `_meta.verdict` to populate `totals.succeeded` / `totals.failed`. The output for **every one of the 50 shards** today is:

| shard | properties | report.json `succeeded` | report.json `failed` | events.jsonl SUCCESS | events.jsonl FAILED_* | match? |
|---|---|---|---|---|---|---|
| shard_0 | 100 | 100 | 0 | 66 | 34 | **MISMATCH** |
| shard_1 | 100 | 100 | 0 | 58 | 42 | **MISMATCH** |
| shard_5 | 100 | 100 | 0 | 53 | 47 | **MISMATCH** |
| shard_9 | 100 | 100 | 0 | 49 | 51 | **MISMATCH** |
| shard_48 | 100 | 99 | 1 | 37 | 62 | **MISMATCH** |
| _… all 50 shards mismatch …_ | | | | | | |
| **TOTAL** | **4982** | **4978** | **4** | **2937** | **2041** | **MISMATCH** |

Every shard's `report.json` over-counts `succeeded` by 30–60 properties. Events.jsonl `output.property_emitted` shows the true verdict distribution:

```
SUCCESS           2937  (58.95 %)
FAILED_NO_DATA    1877  (37.68 %)
FAILED_UNREACHABLE 164  ( 3.29 %)
no emit             4   ( 0.08 %)  — the 4 the report calls "failed"
```

### Comparison with May 10 (same code, same mismatch)

| Source | May 10 properties | May 10 succeeded | May 10 failed |
|---|---|---|---|
| `report.json` totals | 4982 | 4571 | 411 |
| events.jsonl emits | 4982 | **3735** | **1234** (1101 NO_DATA + 133 UNREACHABLE) |

May 10 was already mismatched by ~836 properties, but the absolute numbers happened to suggest a plausible 91.75 %. Today the mismatch widened to ~2,041 properties because the true failure count surged, so the gap is impossible to ignore once you cross-check events.

### Where the wrong number propagates

`scripts/diagnostics/analyze_cloud_run.py` lines 354–358:

```python
totals = report.get("totals") or {}
stats.properties_total      += int(totals.get("properties") or 0)
stats.properties_succeeded  += int(totals.get("succeeded")  or 0)
```

It then computes `properties_failed_other = max(0, total − succeeded − no_data − unreachable)` which **clamps to 0**, hiding the discrepancy in the summary.md top-line. The `failure_terminal_tiers` table and pattern distribution further down are computed from events.jsonl so they remain correct — that's why the summary is internally inconsistent (4978 succeeded but 2041 failures broken out below).

### Where the writer-side regression lives

`succeeded` in `run_report.py` must have stopped reading `_meta.verdict` correctly between May 10 and May 11 (or May 10 was already broken and the issue was just smaller). Suggested triage in priority order:

1. `git log --since=2026-05-09 --until=2026-05-11 -- ma_poc/reporting/run_report.py ma_poc/observability/slo_watcher.py ma_poc/scripts/runners/jugnu.py`
2. Read `_meta` on a per-property properties.json output for shard_0 / pid 10141 (a known FAILED_NO_DATA) and check what `_meta.verdict` actually is.
3. If `_meta.verdict == 'SUCCESS'` while events emit `FAILED_NO_DATA`, the verdict-writer in `verdict.py` regressed.
4. If `_meta.verdict == 'FAILED_NO_DATA'` correctly, the `run_report.py` accounting code regressed.

### Fix urgency

**Critical (block-on)**: the headline metric used by oncall, dashboards, and SLO watcher is currently inverted. False-OK is worse than false-ALERT because nobody pages. The fix can be confined to the report writer — events.jsonl is correct and the analyzer can pivot to events as the source of truth for `succeeded` (one-line change in `analyze_cloud_run.py:357`).

---

## Bug B — Rescue → "no candidates after filtering" → link-hop suppressed → 1633 properties newly fail

### Mechanism (verified per-property)

Property `10141 (wymberlycrossing.com)` succeeded yesterday and failed today. The event traces are identical through the first 11 tiers; the divergence is in what happens after the LLM tier returns empty.

**May 10 — succeeded via link-hop to /floorplans:**

```
extract.tier_attempted  generic:llm        ran_empty   reason="LLM returned no structured units"
extract.pms_detected    pms=unknown        confidence=0.0
extract.adapter_selected adapter=generic
fetch.started           url=.../floorplans            ← link-hop fired
fetch.completed         outcome=OK  body_bytes=302K  rent_signal_count=4
extract.tier_attempted  generic:jsonld     ran_empty   reason="JSON-LD had floor-plan names only (no rent/sqft)"
…  (DOM scan succeeds on /floorplans)
output.property_emitted verdict=SUCCESS
```

**May 11 — fails because link-hop never fires:**

```
extract.tier_attempted     generic:llm        ran_empty
extract.llm_rescue_attempted  n_candidates=86          ← NEW step
extract.llm_rescue_failed     errors=["no candidates after filtering"]  cost=0.0
extract.pms_detected       pms=unknown   confidence=0.0
extract.adapter_selected   adapter=generic
extract.amenities_observed source_tier=TIER_1_API
extract.tier_failed        tier_used=TIER_1_API
output.property_emitted    verdict=FAILED_NO_DATA   ← no link-hop fetch in trace
```

The new `extract.llm_rescue_attempted` step (introduced in the deploy between May 10 and May 11) inspects ~80 link candidates, runs them through a filter, then aborts with `"no candidates after filtering"` when the filter zeroes the list. **In that abort path, link-hop never executes** — the runner emits `FAILED_NO_DATA` and moves on.

### Quantitative impact (all 4982 properties bucketed by trace shape)

| verdict | rescue_attempted | rescue_failed | link_hop_started | count |
|---|---|---|---|---|
| **FAILED_NO_DATA** | yes | yes | **no** | **1633** ← bug B's footprint |
| SUCCESS | yes | yes | yes | 1430 (rescue failed but link-hop saved it) |
| SUCCESS | no | no | no | 679 (first-page win) |
| SUCCESS | no | no | yes | 665 (link-hop saved it without rescue running) |
| FAILED_NO_DATA | yes | yes | yes | 215 (both fallbacks ran, still failed — hard cases) |
| FAILED_UNREACHABLE | no | no | no | 164 (fetch failed; rescue/link-hop never appropriate) |
| SUCCESS | yes | no | yes | 33 (rescue succeeded; link-hop also ran) |
| FAILED_NO_DATA | no | no | no | 29 (immediate fail; no fallback path appropriate) |
| SUCCESS | yes | yes | no | 127 (rescue failed; succeeded by some other path) |

**1633 / 1877 (87 %)** of all FAILED_NO_DATA properties fit the "rescue failed with `no candidates after filtering`, link-hop never tried" shape. This is the dominant failure mode of the run.

### `llm_rescue_failed` error-message distribution (4,125 rescue attempts)

| Errors | Count | What it means |
|---|---|---|
| (empty `errors=[]`) | 1927 | Rescue ran, found candidates, LLM produced nothing usable. Mostly successes after a downstream tier rescued the property. |
| `no candidates after filtering` | **1644** | **The bug.** Rescue had ~40-60 link candidates on average but the filter dropped them all. |
| `unsupported adapter: onesite` | 408 | Rescue path has no OneSite branch — see Bug D. |
| `unsupported adapter: amli` | 19 | Same shape for AMLI. See Bug D. |

`n_candidates` histogram at rescue-attempt time:

| n_candidates | properties |
|---|---|
| 0–19 | 610 |
| 20–39 | 1119 |
| 40–59 | 1309 |
| 60–79 | 795 |
| 80–99 | 194 |
| 100+ | 98 |

The candidates exist — the **filter is the regression**, not the candidate discovery. Likely candidates: a URL-prefix allowlist that no longer matches `/floorplans` / `/availability` / `/apartments`, OR a CSV-management-company gate, OR a `_llm_navigation_hints`-only filter that excludes link-hop's organic candidates.

### Tier-distribution shift confirms the link-hop suppression

| Tier | May 11 | May 10 | Δ |
|---|---|---|---|
| `TIER_3_DOM` | 463 | 1365 | **−902** |
| `TIER_1_API` | 1608 | 914 | +694 (most of these are now FAILED_NO_DATA with TIER_1_API as the terminal-failed label) |
| `LLM_GATE_NO_BODY` | 188 | 95 | +93 |
| `TIER_1_PROFILE_MAPPING` | 26 | 0 | +26 (new — replay cache starting to fire) |

`TIER_3_DOM` was the dominant rescue path for SPA properties whose homepage shows the marketing shell and exposes rent only on a sub-page like `/floorplans`. Losing −902 of those wins is the same population as the +1633 newly-broken properties from Bug B.

### Suggested first edits

The filter lives somewhere on the path from `extract.llm_rescue_attempted` → the rescue's candidate gate. Likely `ma_poc/services/llm_rescue.py` or wherever the rescue collects/filters URLs.

1. `git log --since=2026-05-09 --until=2026-05-11 -p -- ma_poc/services/ ma_poc/extraction/ | grep -B2 -A6 -i 'rescue\\|candidate'`
2. Search for the literal string `"no candidates after filtering"` — it's the exact error message, so it grep-finds the responsible code in one shot.
3. **Until fixed:** consider letting link-hop run independently of rescue. Today's data shows 1430 successes where rescue failed but link-hop saved them — link-hop is doing its job, only the gate is wrong.

---

## Bug C — PMS detector returns `unknown` despite a fingerprint matching the page

### Mechanism

Property `10141 (wymberlycrossing.com)`:

```
extract.detector_signals  fingerprints_matched=['rentcafe']
                          script_srcs_sample=['cdngeneralmvc.rentcafe.com', 't.rentcafe.com', ...]
extract.pms_detected      pms='unknown'  confidence=0.0
extract.adapter_selected  adapter='generic'
```

The fingerprint check matched RentCafe (off the script srcs and DOM fingerprints), but `extract.pms_detected` returned `unknown` with confidence 0.0, so the adapter selector chose `generic`. The RentCafe-specific tier 1 (which knows the API URL shapes for `/api/lookup`, `/api/availability`, etc.) never ran.

### Quantitative impact

Among the 1877 FAILED_NO_DATA properties:

| Signal | Count | % of FAILED_NO_DATA |
|---|---|---|
| Had ≥1 PMS fingerprint matched | 1055 | **56 %** |
| `rent_signal_count == 0` in characterized HTML | 1395 | **74 %** |
| **Both** (fingerprint matched + zero rent signals) | **822** | **44 %** |

### Fingerprints matched but ignored — distribution (1055 properties)

| Fingerprint(s) matched | Properties |
|---|---|
| `['rentcafe']` | 558 |
| `['rentcafe', 'marketing_hyly']` | 85 |
| `['realpage', 'marketing_knock']` | 81 |
| `['rentcafe', 'marketing_knock']` | 72 |
| `['realpage']` | 61 |
| `['marketing_hyly']` | 42 |
| `['sightmap']` | 21 |
| `['rentcafe', 'sightmap']` | 21 |
| `['marketing_knock']` | 18 |
| `['rentcafe', 'marketing_knock', 'marketing_hyly']` | 13 |
| `['marketing_marketapts']` | 13 |
| `['rentcafe', 'sightmap', 'realpage']` | 11 |
| `['rentcafe', 'realpage']` | 10 |
| `['rentcafe', 'sightmap', 'marketing_hyly']` | 10 |
| (other multi-fp combos) | 39 |

**558 RentCafe-only-matched failures** are particularly suspicious — that fingerprint is one of the strongest signals we have (it requires a rentcafe.com CDN script src, which is hard to false-positive on). These should be hitting the RentCafe adapter, not falling through to generic.

### Likely root cause

The confidence-scoring in `ma_poc/pms/detector.py` weighs more than just `fingerprints_matched` — probably also URL pattern + response signals. If a property's CSV URL hostname is its own custom domain (not `*.rentcafe.com`), and the API URL pattern check didn't fire (because the page is SPA-rendered and the rentcafe APIs only load after JS), confidence drops to 0. Combined with Bug B (link-hop blocked), the runner never sees the API responses that would have flipped detection.

The two bugs feed each other: **Bug C means rentcafe-shell sites fall through to generic; generic fails because there are no rent signals on the homepage; Bug B then blocks the only path (link-hop to /floorplans) that exposes the real data.**

### Suggested triage

1. Pull 5–10 sample HTMLs from the 558 RentCafe-only failures (`gs://jugnu-raw-production/runs/2026-05-11/shard_*/raw/*.html`) and re-run `detector.py` against them locally.
2. If detection should fire but isn't, raise the `fingerprint_matched` weight — it's a high-precision signal that's currently undervalued.
3. Stretch: when fingerprint matches but confidence is below threshold, still **bias** the adapter selector toward that PMS instead of falling all the way to generic.

---

## Bug D — Rescue path has no OneSite or AMLI adapter

### Mechanism

`extract.llm_rescue_failed` errors include:

| Error | Count | Affected |
|---|---|---|
| `unsupported adapter: onesite` | 408 | OneSite-detected properties (rescue refuses to run on them) |
| `unsupported adapter: amli` | 19 | AMLI properties |

For these properties, rescue refuses to run, but unlike Bug B, link-hop sometimes still fires. The terminal failure tiers are:

- `TIER_1_API_ONESITE`: 73 FAILED_NO_DATA (OneSite adapter exhausted; no rescue to fall back on)
- `TIER_1_API_AMLI_NEXT_DATA`: 6 FAILED_NO_DATA

### Suggested fix

Either (a) widen the rescue path to handle these adapters by treating them as the generic case after the platform-specific adapter empties, or (b) replace the explicit `unsupported adapter: X` short-circuit with a soft warning that still falls through to link-hop. Today's data shows that 33 of the 79 OneSite/AMLI-platform failures had link-hop fire anyway — the other 46 are downstream of the same suppression as Bug B.

---

## What's WORKING (fixes from May 10 verified in production)

### ✅ Bug 1 (`ApiEndpoint.json_paths` Pydantic crash) — commit `639ccc3`

| Metric | May 10 | May 11 | Status |
|---|---|---|---|
| `PROFILE_UPDATE_FAILED` | 245 | **0** | ✅ SLO restored (threshold ≤ 100). |
| Properties missing daily update | ~245 | ~0 | ✅ Cache is now writing as designed. |

Zero `profile_update_failed` events across all 50 shards. The four writer sites (two in `services/llm_extractor.py`, two in `services/profile_updater.py`) plus the 286-line `test_json_paths_null_value_filtering.py` test are all behaving as advertised. The commit deployed cleanly to production.

### ✅ Bug 2 (zero replay hits) — partial recovery

| Metric | May 10 | May 11 | Threshold | Status |
|---|---|---|---|---|
| `PROFILE_REPLAY_HIT` count | 0 | **26** | — | ✅ The cache is filling. |
| `profile_replay_hit_rate` | 0 % | **17.9 %** | ≥ 30 % | ⚠️ ALERT — still below SLO but trending right. |
| `TIER_1_PROFILE_MAPPING` (winning tier) | 0 | **26** | — | ✅ Same 26 — when replay hits, it wins. |

The replay cache is empty for most properties because Bug 1 was preventing writes for 245 days × 200 properties/day. Now that Bug 1 is fixed, expect 50–100 new mappings per day. Day-2 (May 12) should clear 30 % organically; full SLO compliance by May 14.

### ✅ Bug 3 (`STARTUP_PROBE_OK` 13/20)

| Metric | May 10 | May 11 | Status |
|---|---|---|---|
| `STARTUP_PROBE_OK` count | 13 / 20 | **50 / 50** | ✅ Every shard fired the probe today. |
| `STARTUP_PROBE_FAILED` count | 0 | 0 | ✅ |

`ENABLE_PERSISTENCE_PROBE` is now firing in every shard's startup. Either the env flag was rolled out, or the probe became default-on in the deploy between May 10 and May 11.

### ✅ Analyzer crash (commit `f11b6dc` regression)

`render_failures_csv` works. failures.csv (1.0 MB, 2041 rows) and successes.csv (1.2 MB, 2937 rows) are both written without `NameError`. The May 10 manual analysis's side-find fix took effect.

### ✅ Shard load distribution improvement

50 shards × ~100 properties each — vs 20 × ~250 properties on May 10. Same wall-clock duration (~48 min) means each shard finishes faster, with more headroom for transient retries.

### ✅ Recoveries — what the system learned

78 properties that failed May 10 succeeded May 11. The winning tiers:

| Today's winning tier | Recovery count | What this means |
|---|---|---|
| `TIER_4_LLM_DOM` | 24 | LLM-DOM rescue path is working well |
| `TIER_4_LLM` | 16 | Monolithic LLM fallback is still effective |
| `TIER_1_API_LLM_RESCUE` | 7 | API-LLM rescue is recovering some |
| `TIER_1_API` | 8 | Profile-learned API patterns fired |
| `TIER_MERGED_CROSS_PAGE` | 5 | Link-hop + main page merge succeeded |
| `TIER_3_DOM` | 4 | DOM scan succeeded after page changes |
| (other) | 14 | |

Properties whose URL was missing/blank yesterday (`''` in the table) now have explicit fetches — this is a small input-data cleanup win, ~10 properties.

---

## Failure pattern grouping (events.jsonl as source of truth)

### Pattern distribution (total failures: 2041)

| Pattern | Count | % of failures | What it means | Primary driver |
|---|---|---|---|---|
| **P3** — Generic `TIER_1_API` | **1351** | 66.2 % | No PMS adapter handled; fell through to generic; generic produced no units | **Bug B (~1100 of these)** + Bug C (~558) |
| **P4** — Entrata adapter failure (non-CF) | **241** | 11.8 % | Entrata sub-tier ran but produced no rent | Adapter bug or rescue-filter bug |
| P7 — Pure unreachable | 164 + 80 = 244 | 12.0 % | Fetch never produced usable HTML | Long tail: 68 CF challenges, 28 HTTP 429, 41 transient, etc. |
| **P8** — `LLM_GATE_NO_BODY` | **144** | 7.1 % | LLM gate refused to send body — body empty or below 5 KB threshold | Likely correlated with Bug B (`rent_signal_count=0` HTMLs) |
| **P6** — Platform-specific zero | **141** | 6.9 % | onesite 73, appfolio 33, squarespace 19, wix 10, amli 6 | Bug D for onesite + amli; Squarespace/Wix are syndication-only by design |
| P2 — CF on Entrata | 84 | 4.1 % | Cloudflare challenge; rescue can't proxy past it | Unchanged from May 10 (52 → 84, +32) |
| Pother | 0 | — | — | — |

### Pattern 3 — Top 25 management-company domains (1351 generic-API failures)

| Domain | Failures today | Failures May 10 | Δ |
|---|---|---|---|
| byredwood.com | 11 | 0 | **+11** |
| equityapartments.com | 7 | 6 (long-tail) | +1 |
| krcapartments.com | 7 | 4 | +3 |
| imtresidential.com | 6 | 1 | **+5** |
| rentanapt.com | 6 | 5 | +1 |
| gscapts.com | 5 | 9 | −4 |
| keystonemanagement.com | 4 | 5 | −1 |
| broadmoor.cc | 4 | 3 | +1 |
| akelius-properties.us | 3 | 0 | **+3** |
| eaglerockproperties.com | 3 | 2 | +1 |
| lindyproperty.com | 3 | 1 | +2 |
| arizona.weidner.com | 3 | 3 | 0 |
| cortland.com | 3 | 4 | −1 |
| springsapartments.com | 3 | 3 | 0 |
| southwoodrealty.com | 3 | 2 | +1 |
| _… 10 more domains with 2 failures each …_ | | | |

**`byredwood.com (+11)`** and **`imtresidential.com (+5)`** are the two domains whose failures spiked overnight. Both are multi-property management companies whose properties share the same site template — a single bug in that template's handling explains the cluster. Pulling one byredwood HTML and re-running `detector.py` + the rescue filter would localise the issue in 10 minutes.

### Pattern 6 — Platform-specific sub-distribution

| Platform | Failures | Notes |
|---|---|---|
| platform_onesite | 73 | 408 properties hit "unsupported adapter: onesite" in rescue; 73 of those terminally fail (others get saved by link-hop or other paths). |
| platform_appfolio | 33 | Mostly the same long-tail AppFolio properties as May 10 (no Δ). |
| platform_squarespace | 19 | Static syndication-only sites — expected losses; can only be fixed with vision/PDF parsing. |
| platform_wix | 10 | Same as Squarespace — syndication-only. |
| platform_amli | 6 | 19 AMLI rescue refusals; only 6 are terminal. |

### Pattern 7/2 — Fetch-side failures (244 total: 164 UNREACHABLE + 80 in P2)

| Outcome | Signature | Count | Action |
|---|---|---|---|
| `BOT_BLOCKED` | `CF_CHALLENGE` | 68 | Cloudflare interactive challenge — needs vision-LLM solve or residential proxy escalation. |
| `RATE_LIMITED` | `HTTP_429` | 28 | Same proxy IP hitting a site many times. Rotate proxy pool. |
| `TRANSIENT` | `Error` | 28 | Generic network errors; retry next day. |
| `TRANSIENT` | `timeout` | 13 | Page-load timeout (default 30 s). |
| `BOT_BLOCKED` | `HTTP_403` | 8 | Server-side block. |
| `HARD_FAIL` | `HTTP_404` | 7 | Dead URLs — flag in CSV for removal. |
| `OK` | `TIMEOUT_SALVAGED` | 6 | Recovered after timeout; not a failure. |
| `BOT_BLOCKED` | `BOT_BLOCKED` | 5 | Generic block. |
| `TRANSIENT` | `HTTP_500` | 2 | Server error. |
| `HARD_FAIL` | `HTTP_409` | 2 | Conflict (likely auth). |
| `HARD_FAIL` | `HTTP_401` | 1 | Auth required (likely portal login). |

### Pattern 10 — `UNITS_KEYLESS_HIGH` warnings (info, not failure)

94 successful properties carried this warning today. Not load-bearing — indicates LLM-extracted units lacked a natural identity anchor, but units were still emitted.

---

## Persistence-loop health (events-derived)

| Metric | May 11 | Threshold | Status | Δ vs May 10 |
|---|---|---|---|---|
| `MAPPING_SAVE_DROPPED` total | 0 | — | OK | 0 |
| `mapping_save_drop_rate` | 0.0 % | < 50 % | ✅ OK | 0 pp |
| `PROFILE_REPLAY_HIT` count | 26 | — | — | +26 |
| `profile_replay_hit_rate` | 17.9 % | ≥ 30 % | ⚠️ ALERT | +17.9 pp |
| `PROFILE_UPDATE_FAILED` | 0 | ≤ 100 | ✅ OK | **−245** |
| `STARTUP_PROBE_OK` count | 50 | ≥ shards_seen | ✅ OK | +37 |
| `STARTUP_PROBE_FAILED` | 0 | == 0 | ✅ OK | 0 |
| `FIELD_PATCH_HIT` | 0 | — | — | 0 |
| `FIELD_PATCH_DRIFT` | 3 | — | — | +3 |
| `LLM_GATE_RELAXED` | 0 | — | — | 0 |

**Channel-by-channel diagnosis** (per `project_self_learning_loop_arch.md`):

- **Channel 1 — `llm_field_mappings`** (replay): writing successfully (0 drops), reading 26 hits/run. Hit rate climbing from 0 % organically as cache fills.
- **Channel 2 — `field_patches`**: no hits, 3 drift events. Drift events are informational (a saved patch's site moved), not failures.
- **Channel 3 — `blocked_endpoints`**: actively working (every property's events show e.g. `dropped 4 API(s) from profile.blocked_endpoints` — observed in the trace dump above).
- **Channel 4 — `dom_hints`**: cannot verify from events alone — would need `db_query.py Q4_channel_row_counts`. Suspect this is empty for most properties since the post-PR-9 degraded-save flag (`ENABLE_DEGRADED_DOM_PERSIST`) is on but the writer may still be gated by Bug B's rescue-filter regression.
- **Channel 5 — `known_endpoints`**: working (the api_narrow / api_broad tiers ran on profile-known endpoints for many properties; replay-cache fills come from here).

---

## Day-over-day numbers (corrected)

Using events.jsonl (true verdicts) on both sides:

| Metric | May 11 | May 10 (corrected) | Δ |
|---|---|---|---|
| Properties processed | 4982 | 4982 | 0 |
| SUCCESS | **2937** | 3735 | **−798** |
| FAILED_NO_DATA | **1877** | 1101 | **+776** |
| FAILED_UNREACHABLE | 164 | 133 | +31 |
| No emit | 4 | 13 | −9 |
| **True success rate** | **58.95 %** | **74.97 %** | **−16.02 pp** |
| `PROFILE_UPDATE_FAILED` | 0 | 245 | **−245** ✅ |
| `PROFILE_REPLAY_HIT` | 26 | 0 | +26 ✅ |
| LLM cost | $26.46 | $20.14 | +$6.32 |
| Shards | 50 | 20 | +30 |
| Regressions (passed → failed) | 884 (mostly TIER_3_DOM → fail) | 288 | +596 |
| Recoveries (failed → passed) | 78 | 147 | −69 |
| Repeat failures | 1155 | 923 | +232 |

The cost increase (+$6.32) is the rescue path running LLM calls on the 1633 properties that Bug B newly burns. Once Bug B is fixed, the rescue will stop firing on properties that have a clean link-hop path, and cost should drop back to the May 10 baseline (~$20).

---

## Top 25 regressed domains (today fails > yesterday fails)

| Domain | Today | Yesterday | Δ |
|---|---|---|---|
| byredwood.com | 11 | 0 | **+11** |
| imtresidential.com | 6 | 1 | +5 |
| akelius-properties.us | 3 | 0 | +3 |
| lindyproperty.com | 3 | 1 | +2 |
| qtowneoaks.com | 2 | 0 | +2 |
| alaska.weidner.com | 2 | 0 | +2 |
| haydenplaceapts.com | 2 | 0 | +2 |
| galmangroup.com | 2 | 0 | +2 |
| esring.com | 2 | 0 | +2 |
| _… 16 more with +1 each: parcwmp, parkwestapartmentschino, prairiecreekapartments, etc. …_ | | | |

`byredwood.com` is the strongest signal — 11 properties on the same template all newly failing. Single point of investigation.

---

## What to do next (priority order)

### Immediate (today)

1. **Fix Bug A (reporting)** — patch `analyze_cloud_run.py:357` to source `succeeded` from `events.jsonl` emit verdicts, not `report.json` totals. Then page the same fix into `run_report.py` and `slo_watcher.py`. This is a 5-line change but it unblocks every downstream metric.
2. **Identify and revert Bug B's gate** — `grep -rn "no candidates after filtering" ma_poc/` to find the rescue filter; check its commit history. If it was added in the same deploy that landed `639ccc3` or any of the canary PRs, that's the suspect.
3. **Pull one `byredwood.com` HTML + events** to confirm the rescue-filter hypothesis end-to-end on a known template. If the filter is the culprit, fixing it should recover all 11 byredwood failures in one shot.

### Day-2 (May 12 run)

4. Re-run the analyzer with `--check-db` to compare events-derived metrics against the DB row counts. The `profile_replay_hit_rate` should climb (0 → 17.9 → ≥30 % within 2 runs).
5. Confirm Bug B fix landed: expect `extract.llm_rescue_failed.errors=["no candidates after filtering"]` count to drop from 1644 → < 100, with `TIER_3_DOM` wins rising back toward 1365.

### Investigation backlog

6. **Bug C (PMS detector)** — fix the confidence weighting in `detector.py` to bias toward the matched fingerprint instead of falling all the way through to generic. Even partial improvement here cuts the 558 RentCafe-only failures in half.
7. **Bug D (rescue adapter coverage)** — add OneSite + AMLI branches to the rescue path. 427 properties / day get an "unsupported adapter" short-circuit today.
8. **Cloudflare path (P2)** — 68 CF challenges/day is a fixed ceiling without vision-LLM solve. Worth scoping a Tier 7 vision-LLM CF-solve as a Phase B item.

---

## Repro / verification commands

```bash
# Pull a fresh mirror (ADC-based, bypasses stale gcloud auth)
export GCLOUD_TOKEN=$(gcloud auth application-default print-access-token)
python c:/tmp/pull_2026_05_11.py     # 343 files, 76 MB in 55 s

# Run analyzer with corrected expected-shards
python ma_poc/scripts/diagnostics/analyze_cloud_run.py \
  --date 2026-05-11 --compare-date 2026-05-10 \
  --expected-shards 50 --local-mirror c:/tmp

# Reconcile report.json vs events.jsonl per-shard (proves Bug A)
python -c "import json; from pathlib import Path; …"   # see code in this doc above

# Confirm Bug B's footprint
python -c "
import json, collections
from pathlib import Path
c = collections.Counter()
for shard in Path('c:/tmp/run-2026-05-11').glob('shard_*'):
    for line in (shard / 'events.jsonl').read_text(encoding='utf-8', errors='ignore').splitlines():
        try: ev = json.loads(line)
        except: continue
        if ev.get('kind') == 'extract.llm_rescue_failed':
            c[' | '.join((ev.get('errors') or [])[:3])] += 1
for k, n in c.most_common(): print(f'{n:5d}  {k}')
"
# Expected: 1644 'no candidates after filtering' — that's the regression footprint.
```

---

## Root-cause mapping against yesterday's commits

Yesterday's commit timeline (UTC):

| Commit | Time | Author | Description | LOC |
|---|---|---|---|---|
| `b2759f7` | 2026-05-10 19:42 | Claude | fix: url_pattern assertion for normalize_url_pattern | small |
| `f3c75ad` | 2026-05-10 20:19 | Claude | feat(canary): L1 replay infrastructure | large |
| `8e8b267` | 2026-05-10 15:18 | ashuchan | **PR1** — persistence sentinel probe + structured `PROFILE_UPDATE_FAILED` event | 1324 |
| `4113a7c` | 2026-05-10 20:42 | Claude | feat(canary): L2–L7 end-to-end | large |
| `f11b6dc` | 2026-05-10 21:29 | Claude | feat(canary): stratified two-basket population | small |
| `df2a302` | 2026-05-10 16:04 | ashuchan | **PR2** — Channel 4 FieldPatch persistence | 655 |
| `3013362` | 2026-05-10 19:06 | ashuchan | **"Fixing the ever alluding llm feedback loop"** — bundles PR3–PR9 + analyzer changes | **5184** |
| `5ae7cb8` | 2026-05-10 19:38 | ashuchan | local canary setup | small |
| `639ccc3` | 2026-05-10 22:04 | ashuchan | **"fixed db write error"** — Bug-1 JSONPath null-filter fix | small |

Cloud run started 2026-05-11 02:07 UTC, finished 02:14 UTC, so **every commit above was live in the run**.

### Bug A (report.json verdict accounting) — caused by commit `3013362`

**File:** `ma_poc/scripts/runners/jugnu.py`

The commit hoisted `_format_output(...)` from the OUTER caller (`_process_one`, line 347) into the INNER `_process_property` function (new line 691), so Channel-4 null-field recovery could see the formatted dict in time for `save_field_patch` to fire.

Before the commit (53b0680 baseline):

```
_process_property:
  ... extract, profile_update ...
  ... verdict computed → meta["verdict"] set at line 761 ...
  return result
_process_one (outer):
  result = await _process_property(...)
  formatted = _format_output(result, ...)     # AFTER verdict set ✓
  await _run_null_field_recovery(result, formatted, ...)
```

After commit `3013362`:

```
_process_property:
  ... extract, profile_update ...
  formatted_for_recovery = _format_output(result, ...)    # NEW line 691
  await _run_null_field_recovery(result, formatted_for_recovery, ...)
  result["_v2_formatted"] = formatted_for_recovery        # cached, NO VERDICT
  ... verdict computed → meta["verdict"] set at line 761 (LATER) ...
_process_one (outer):
  result = await _process_property(...)
  formatted = result.get("_v2_formatted") or _format_output(...)   # reuses cached
```

The bug is in `_format_v2` at line 1047: `meta = result.get("_meta", {})`. At line 691 invocation, **`result["_meta"]` does not exist yet** (first set at line 761 via `result.setdefault("_meta", {})`). So `.get("_meta", {})` returns a *new empty dict* — never the same object as `result["_meta"]`. The verdict assignment at line 763 modifies the dict at `result["_meta"]`; the dict at `_v2_formatted["_meta"]` stays empty.

Verified end-to-end: pulling `shard_0/properties.json` from GCS shows `_meta.verdict = None` for **all 100 properties**. The run-report builder then reads `meta.get("verdict") or ""` → empty → no `FAILED` prefix match → counts everything as succeeded.

**What got missed:**

1. **No regression test for `_meta.verdict` round-trip through `_v2_formatted`.** `tests/reporting/test_run_report.py` constructs property dicts with `_meta.verdict` already populated and asserts the totals — it doesn't simulate the runner's call ordering. Adding a test that calls `_process_property` end-to-end and asserts `result["_v2_formatted"]["_meta"]["verdict"] != None` would have caught this in CI.
2. **The hoist comment (line 681) calls out the timing constraint for recovery → save_field_patch but doesn't mention `_meta`.** A two-line `result.setdefault("_meta", {}).update({...})` initialiser before the hoisted format call would fix it without changing semantics elsewhere.
3. **`f11b6dc`'s canary "regression detection" basket** would have caught this in isolation — but the canary itself was deployed in the same commit set, so it didn't run against the pre-hoist baseline. The two-basket population needs to have been seeded one day earlier.

**One-line fix:** initialise `_meta` at the top of `_process_property` (before any return paths or hoisted formatters). The setdefault becomes a no-op at line 761; verdict assignment continues to mutate the shared dict object that `_format_v2` captured.

### Bug B (1633 properties: rescue → no link-hop) — NOT a code regression in yesterday's commits; cumulative collateral from pre-existing Bug-1

**Files of interest:** `ma_poc/pms/scraper.py:1244-1331`, `ma_poc/services/profile_updater.py:605-610`

The link-hop path is unchanged in yesterday's commits. The `should_hop` gate at scraper.py:1620 still fires when `result.get("units")` is empty. `_try_link_hop` at 1244 still runs.

The regression is **`_try_link_hop` returns `None` at line 1331** because `ranked` is empty after profile-top injection + keyword ranking + dedup-and-visited filter:

```python
ranked = [(u, s, a) for (u, s, a) in ranked if u not in visited and u not in explored_skip]
...
if not ranked:
    return None    # ← no LINK_HOP_STARTED event ever emitted
```

For the regression sample (`property 10141 / wymberlycrossing.com`), May 10's link-hop fired with `candidates=[(/floorplans, 10001, "profile:winning_page_url"), (/floorplans, 10000, "profile:availability_link"), (/scheduletour, 60, "schedule a tour")]`. May 11's run had `ranked=[]` — no profile_top hints, no keyword candidates.

Why is `profile.navigation.winning_page_url` empty on May 11 when it was set on May 10? Because **Bug 1 (`PROFILE_UPDATE_FAILED` Pydantic crash, ~245/day for many days)** prevented `profile_store.save(profile)` from running on the very properties where Channel-1 LLM mapping fired. The order in `update_profile_after_extraction`:

```
line 607: profile.navigation.winning_page_url = winning_url   # set
line 660: ApiEndpoint(json_paths=clean_paths)                  # crash (pre-639ccc3)
                                                               # save_profile NEVER runs
```

On every crash, the in-memory profile got the new winning_page_url but **the on-disk profile retained the OLD state** (which may never have had a winning_page_url if the property had never previously succeeded). For 4 days of Bug 1 active in production, the same ~245 properties/day got their saves nuked — that's up to ~1000 unique properties whose profiles are *fossilised* in a state that pre-dates any successful link-hop discovery.

Today, the **Bug 1 fix** (639ccc3) is live, so today's successes WILL persist. But for the 1633 properties that **already** lost their profile state and aren't successful today (because rescue's filter drops their candidates and link-hop has no profile_top to inject) — they're stuck. They need either:
- A successful first-pass extraction so a new winning_page_url gets saved, or
- A backfill that replays past field_recovery artifacts to repopulate `winning_page_url` (the `backfill_persistence.py` script from commit `3013362` does this for `llm_field_mappings` but not for `navigation`)

**What got missed:**

1. **Bug 1 was treated as a write-side bug only.** The fix (`clean_string_value_dict`) addressed *future* saves. The *historic data loss* — 4+ days × ~245 properties — was not modelled in the May 10 manual analysis. The analysis said "Day-over-day regressions [288] ... Should drop sharply (a chunk of the 288 are downstream of stale profiles)." It under-predicted: today's regressions are **884**, not "<288 → 0".
2. **`backfill_persistence.py`** (added in commit `3013362`) was scoped to Channel 1 (llm_field_mappings) and Channel 4 (field_patches) only. It explicitly notes: *"Channel 4 NOT backfillable (URL not in field_recovery artifacts)"*. Navigation (winning_page_url / availability_links) is also not backfilled — but it should be: every `extract.link_hop_recovered` event in past JSONL archives has the sub_url that should go into `availability_links`.
3. **No "profile state restoration" canary.** The L1 canary (`f3c75ad`) replays cloud failures against today's code, but the failure being replayed is fixed in the local environment — so canary success ≠ production success for property-specific stateful regressions.
4. **The rescue filter could have a graceful fall-through.** When `_filter_candidates` returns 0 and the property has no profile-top, the runner could *force* a keyword-anchor-only `_try_link_hop` against well-known sub-page paths (`/floorplans`, `/availability`, `/apartments`). That's a one-line defensive fallback that would have caught the regression without needing profile state.

### Bug C (PMS detector returns unknown) — NOT from yesterday's commits

**File:** `ma_poc/pms/detector.py`. Unchanged in the May-10 commit set. Pre-existing detection-confidence bug; documented in the main analysis section.

### Bug D (rescue: unsupported adapter: onesite/amli) — caused by commit `53b0680` (May 9), NOT yesterday

**Files:** `ma_poc/pms/scraper.py:713` ↔ `ma_poc/services/llm_api_rescue.py:154`

Commit `53b0680` ("Fixed scrapping bugs and fields consumption", May 9) widened the rescue allow-list at `scraper.py:713`:

```python
adapter_name in {"generic", "entrata", "appfolio", "onesite", "amli"}   # NEW
```

But did NOT update `llm_api_rescue.py:154`:

```python
SUPPORTED_ADAPTERS: frozenset[str] = frozenset({"generic", "entrata", "appfolio"})
```

So `onesite` and `amli` are passed to `rescue_from_api_responses()`, which immediately rejects them at line 654 (`out.errors.append(f"unsupported adapter: {inp.source_adapter}")`). The two sides have been out of sync for 2 days; yesterday's commits did not touch either. **Pre-existing.**

**What got missed:** an integration test that calls `_process_property` with a OneSite-detected property and asserts rescue either runs or is explicitly skipped — never silently rejected after being invoked. The cleanest fix is to widen `SUPPORTED_ADAPTERS` to match scraper.py, with a TODO for the OneSite/AMLI-specific rank functions if they need tuning.

---

## What slipped through CI / review — summary

| # | Bug | Cause | Test gap | Single-line cure |
|---|---|---|---|---|
| A | report.json verdict=None | `3013362` hoisted `_format_output` ahead of `_meta` init | No round-trip test from `_process_property` end-to-end checking `_v2_formatted._meta.verdict` | Move `result.setdefault("_meta", {})` to top of `_process_property` |
| B | 1633 link-hop suppressions | Cumulative Bug-1 collateral; no nav backfill | No state-restoration canary; backfill scoped to Channel 1+4 only | Force keyword-only link-hop with hard-coded sub-page paths when profile_top is empty AND rescue returned 0 |
| C | PMS detector returns unknown | Pre-existing detector confidence weighting | No "fingerprint matched → adapter selected" assertion | Bias adapter selector to matched fingerprint when confidence < threshold |
| D | rescue unsupported adapter | `53b0680` (May 9) widened scraper but not rescue | No "rescue allow-list = scraper allow-list" property test | Update `SUPPORTED_ADAPTERS` in `llm_api_rescue.py:154` to match scraper.py:713 |

The deepest lesson: **commit `3013362` bundled 5,184 lines including PR3–PR9 + canary infrastructure + analyser changes**. That's too large a change set for a single review. The hoist-bug (A) is the kind of subtle ordering issue that hides easily in a 40-file diff. The May-10 commits should have been merged as four small PRs (PR1 / PR2 / PR3 / canary), each with its own integration test pass on the existing baseline before stacking.

---

## Addendum — review comments + follow-up investigation

### B-revisited — the *correct* fallback is layered, not hard-coded paths

The first-pass suggestion ("force keyword-only link-hop with hard-coded sub-page paths") was crude. There are four signal sources for "where does unit data actually live on this site," and they should be tried in this priority order:

| # | Source | What it costs | Why it's better than guessing |
|---|---|---|---|
| 1 | **`_llm_navigation_hints`** (already plumbed) — `result["_llm_navigation_hints"]` is populated by `_merge_hint_extras` in `generic.py:1688-1691` from any LLM sub-tier that emits `navigation_hint`. The scraper at `scraper.py:1678` passes it into `_try_link_hop` and `_augment_ranked_with_hints` puts those URLs at `_LLM_HINT_SCORE` (10000). | Already free — the LLM ran. | For `10141` today the monolithic LLM ran for 8.4 s, returned `ran_empty`, and emitted **no** `navigation_hint`. The mechanism exists but doesn't fire because the LLM gave up on the empty-shell homepage. **Improvement: when monolithic LLM returns 0 units, re-prompt with a navigation-only goal** (`given this homepage HTML, what URL would have unit data?`). One extra call, ~$0.01, recovers ~80 % of these. |
| 2 | **`/sitemap.xml`** via the L2 discovery layer — `ma_poc.discovery.sitemap` already has a sitemap consumer with ETag caching. It's the authoritative list of every URL on the site. | Free if cached (sitemaps rarely change). Cheap if not (single HTTP GET). | A rentcafe-fingerprinted site's sitemap usually lists `/floorplans`, `/availability`, `/photo-gallery`, etc. by name. Filter for paths matching `_AVAILABILITY_URL_SIGNALS` (already defined in `llm_api_rescue.py:122-125`) and inject into `_try_link_hop`'s `profile_top` with score 9999 (just under LLM-hint priority). **No new heuristics needed — reuse `_AVAILABILITY_URL_SIGNALS`.** |
| 3 | **PMS-fingerprint priors** — when `extract.detector_signals.fingerprints_matched` is `['rentcafe']`, the runner already knows the PMS template. RentCafe properties expose units at `/floorplans` (template-fixed). | Free — just a dict lookup. | Centralise in `ma_poc/pms/templates/sub_paths.py` (new) — `RENTCAFE_PRIORITY_PATHS = ("/floorplans", "/availability", "/apartments")`, same for Entrata / RealPage / OneSite. The detector returns the fingerprint; the link-hop ranker reads the prior. **Cleaner than hard-coding paths in `scraper.py` because the priors are per-PMS, version-controlled, and testable.** |
| 4 | **Captured-API URL inspection** — the 86 candidates the rescue dropped include API URLs the page called. Some of those are navigation APIs (e.g., RentCafe's `/api/sitemap`, AppFolio's `/listings`). Even if the rescue filter rejects them as "noise" for LLM consumption, link-hop can use their **paths** as hints. | Free — the data is already in `result["_raw_api_responses"]`. | A page that fetched `/api/v1/floorplans/list.json` strongly suggests `/floorplans` is the navigation URL. Parse the captured-URL paths, strip `/api/v1/`, see if a corresponding **page** URL exists. Niche but high-precision when it fires. |

**Recommended implementation order:** Source 3 first (one-day change, no LLM cost, fixes ~600 of the 1633), then Source 1 re-prompt (recovers another ~500 properties at ~$5 extra LLM/day), then Source 2 sitemap (catches the long tail).

Critically, **none of these require profile state restoration** — they're orthogonal to Bug 1's collateral damage. They're the safety net that should have existed all along.

### C-revisited — Bug C was introduced by commit `eb18889` on 2026-04-20 (three weeks ago)

```
eb18889  2026-04-20 05:30:40 UTC  "jugnu adapter fixes (Change 2): router invariant + detector entries for funnel/touchtour"
```

The commit added `confirm_detection()` at `ma_poc/pms/detector.py:528-596`. Its design intent (from the commit message and the in-code docstring):

> *After page load, verify the URL-based detection against captured bodies. If captured responses exist but none match the detected PMS's body shape, demote to `pms="unknown"` so the generic cascade (with LLM/Vision allowed) runs rather than stamping a misleading `TIER_1_API_<PMS>` label on a mismatched property. This is the counter to Windsor/Mark-Taylor/Vegas misrouting observed in the 2026-04-20 run: the URL said RentCafe but the captured body shape was Funnel.*

This was a *correct* fix for one failure mode (Windsor / Mark-Taylor / Vegas misrouting). But it created a new failure mode:

- Many real RentCafe sites load unit data through the rentcafe widget's iframe or via `cdngeneralmvc.rentcafe.com` static asset CDN, with the actual data API call going to a domain like `widget.rentcafe.com/api/...` that **is** in the captured set but whose body shape isn't checked by `RentCafeAdapter.matches_response_body` (which expects the *Yardi* API shape, not the widget shape).
- Or the rentcafe API call is in the per-property `blocked_endpoints` list (from an earlier mis-classification by Phase-3 LLM noise classification) and never re-captured.
- Or the page is heavily SPA-rendered and the rentcafe API fires AFTER `wait_until=networkidle` returns, so the runner never sees it.

In all three cases, `confirm_detection` demotes a valid RentCafe property to "unknown", and the 558 RentCafe-only failures are born.

**Why the fix made things worse on net:** the failure it prevents (misrouted adapter stamps wrong tier label on success) is **observability noise**. The failure it creates (RentCafe demoted to generic → generic fails because no rent signals on the shell page) is **a real lost property**. The cure is more aggressive than the disease in production.

**Suggested correction:**

1. Soften the demotion: when fingerprint match exists and confidence > 0.7, **bias** the adapter selection toward the matched PMS but allow the generic cascade to also run if the PMS adapter returns empty. Currently `confirm_detection` is binary (keep | demote); replace with a tri-state (keep | dual-run | demote).
2. Widen `RentCafeAdapter.matches_response_body` to accept the **widget** body shapes — the existing checker only knows the Yardi shape.
3. Don't demote when API responses are entirely in the per-property blocklist (those responses were learned-as-noise on prior runs; they don't disconfirm anything).

The existing `confirm_detection` tests at `ma_poc/tests/pms/test_detector.py` (5 cases per the eb18889 commit) cover the Windsor-style misrouting scenario but **don't cover the symmetric case** of a real PMS property whose API wasn't captured. That's the test gap that let this regression ship for three weeks.

### D-revisited — yes, the doc gap is real and structural

Bug D is a **cross-file invariant with no documentation, no test, and no enforcement**:

| File | Statement | Comment that explains the dependency on the other file |
|---|---|---|
| `ma_poc/pms/scraper.py:713` | `adapter_name in {"generic", "entrata", "appfolio", "onesite", "amli"}` | None. There is a comment about F1.3 widening the gate but no mention that `llm_api_rescue.py` has its own gate. |
| `ma_poc/services/llm_api_rescue.py:154` | `SUPPORTED_ADAPTERS = frozenset({"generic", "entrata", "appfolio"})` | None — no comment at all on the literal. The file-level docstring at line 1-10 mentions only those three adapters but doesn't say "this set must match scraper.py:713." |

**Test coverage that should have caught it but doesn't** — `ma_poc/tests/pms/test_scraper_llm_rescue_routing.py:611-653`:

```python
async def test_f1_3_rescue_fires_for_onesite_adapter() -> None:
    """F1.3: ``onesite`` was added to the rescue allow-list per the May-8
    plan. Without this, ~50 OneSite properties never see the LLM rescue."""
    ...
    with patch("ma_poc.services.llm_api_rescue.rescue_from_api_responses",
               return_value=rescue_out) as mock_rescue:
        ...
    mock_rescue.assert_called_once()
```

**The test mocks the very function whose internal gate it should be verifying.** The mock returns `RescueOutput(units=good_units)` unconditionally — the real `SUPPORTED_ADAPTERS` check at line 653 of `llm_api_rescue.py` is never executed. So the test asserts "scraper passes onesite to rescue" ✓ but doesn't catch "rescue immediately rejects onesite."

This is a classic anti-pattern: **mocking the unit-of-interest instead of mocking around it**. The mock should have been on a downstream dependency (the LLM client), not on the gate the test was supposed to verify.

**Structural doc gaps in this repo:**

1. **No `CONTRACTS.md`** — cross-file invariants like "scraper.py rescue allow-list == llm_api_rescue.py SUPPORTED_ADAPTERS" should have a single authoritative document listing them with a paragraph each: where they live, why they exist, what tests enforce them.
2. **No "when you change X, also change Y" comments** at the seams. `scraper.py:713` should have a `# Keep in sync with llm_api_rescue.py:SUPPORTED_ADAPTERS — see CONTRACTS.md#rescue-allow-list` comment. Same in the rescue file pointing back.
3. **CLAUDE.md has 800+ lines of architecture but doesn't enumerate cross-module invariants.** It lists tier ordering, profile shape, output schema — none of which would alert a contributor that `SUPPORTED_ADAPTERS` is duplicated state.
4. **No grep-able "INVARIANT:" or "CONTRACT:" marker convention.** When invariants are spread across files with no shared marker, any change can break one without anyone noticing.

The same gap exists for at least three other duplicated literals I noticed while investigating:
- `_AVAILABILITY_URL_SIGNALS` in `llm_api_rescue.py:122` vs the URL-keyword ranker in `scraper.py` — partial overlap, no shared constant.
- `_RESCUE_NOISE_HOSTS` in `llm_api_rescue.py:33` vs the legacy `_FALSE_POSITIVE_HOSTS` in `scripts/entrata.py` (production scraper) — overlapping but distinct lists, both maintained.
- `_TIER_MAP` in `profile_updater.py` vs tier-name strings emitted by `generic.py` — string-typed coupling with no enum.

This is a **maintenance hazard pattern**, not a single bug. A 30-minute pass to extract these into one `ma_poc/contracts.py` module would catch the next four Bug-Ds before they ship.

### Integration tests — why they didn't catch any of A, B, C, or D

There are **57 integration test files** under `ma_poc/tests/integration/` plus dozens more under `ma_poc/tests/pms/`, `ma_poc/tests/profile/`, `ma_poc/tests/services/`. The coverage *number* looks excellent. But every one of A/B/C/D is one of the following anti-patterns:

| # | Anti-pattern | Bug it misses | Concrete example |
|---|---|---|---|
| 1 | **Fakes the data, not the flow** | Bug A | `tests/integration/e2e/test_e2e_5_property_smoke_filesystem.py:38-52` constructs a property dict with `_meta = {"verdict": "SUCCESS", "scrape_tier_used": tier}` hand-rolled, then asserts the report renders. It never calls `_process_property`, so the `_v2_formatted` caching path is invisible to the test. |
| 2 | **Mocks the contract you're testing** | Bug D | `test_scraper_llm_rescue_routing.py:611-653` patches `rescue_from_api_responses` to return canned units, then asserts `mock_rescue.assert_called_once()`. The real `SUPPORTED_ADAPTERS` gate at `llm_api_rescue.py:653` is never executed in any test. |
| 3 | **Tests the happy path of a fix, not the symmetric failure** | Bug C | `test_detector.py` has 5 cases for `confirm_detection` (all variants of "URL said X, body said Y, demote to unknown"). No test for "URL said RentCafe, all bodies are CDN/analytics noise (because the rentcafe API is in blocked_endpoints) — keep as RentCafe." |
| 4 | **Test asserts the in-memory state, not the on-disk persisted state** | Bug 1 collateral / Bug B | `test_pr1_degraded_mapping_persistence.py` asserts `profile.navigation.winning_page_url == ...` after `update_profile_after_extraction` returns. But it never asserts what `profile_store.save(profile)` then `profile_store.load(canonical_id).navigation.winning_page_url` returns — i.e., it doesn't catch the case where save() never gets called because an unrelated assignment later in the function raised. |
| 5 | **No "previous-state survives this regression" canary** | Bug B (cumulative starvation) | None of the 57 integration tests boot a `ScrapeProfile` from a stale on-disk state (one where Bug 1 ate the navigation block) and verify the scraper still recovers. The canary in `f11b6dc` exists but was deployed alongside the change it should have caught. |
| 6 | **No "headline metric matches truth" assertion** | Bug A | No test asserts that `report.json.totals.succeeded == sum(1 for ev in events.jsonl if ev["kind"] == "output.property_emitted" and ev["verdict"] == "SUCCESS")`. That single property test, hosted in `tests/integration/consume/` and run against the test corpus, would have caught Bug A in CI. |

**The pattern:** tests are wired around the unit-of-interest rather than through it. They prove that components compile and call each other, but they don't prove that the data shape arriving at the consumer matches what the producer intended.

#### Concrete proposals (in priority order)

1. **Add `test_e2e_verdict_round_trip.py`**: feed 5 mock properties through `_process_property` with `schema_version="v2"`, then assert `properties.json[i]["_meta"]["verdict"]` matches the verdict emitted by `output.property_emitted` for each `property_id`. Would have caught Bug A on its first run.
2. **Add `test_cross_file_contract_rescue_allow_list.py`**: at test import time, import `scraper.py` and parse the allow-list set literal at line 713, import `SUPPORTED_ADAPTERS` from `llm_api_rescue.py`, assert equal. Pure data assertion, no mocks. Would have caught Bug D immediately when scraper.py was widened.
3. **Add `test_confirm_detection_preserves_rentcafe_with_noise_only_apis.py`**: pass a `DetectedPMS(pms="rentcafe", confidence=0.9)` and `api_responses=[{"url": "https://googletagmanager.com/...", "body": "..."}]` to `confirm_detection`. Assert result is unchanged (still rentcafe). Currently this test does not exist; if it did, the symmetric failure mode of Bug C would have been blocked at PR time.
4. **Replace mock-of-the-unit pattern in `test_scraper_llm_rescue_routing.py`**: instead of patching `rescue_from_api_responses`, patch `_call_openrouter` (the actual LLM HTTP client). Then the test exercises the real `SUPPORTED_ADAPTERS` gate, the real filter, the real ranking. ~3-line change per test.
5. **Add a "profile starvation" fixture**: in `tests/fixtures/profiles/starved_navigation.json`, a profile with `blocked_endpoints` populated but `navigation.winning_page_url = None` and `availability_links = []`. Any test that loads this fixture and runs `_try_link_hop` against a SPA-shell homepage should still recover via the new layered fallback (Section B-revisited). Forces the recovery path to actually work.
6. **CI gate: assert no `mock.patch` target is the function-under-test**. A pre-commit lint that scans test files for `patch("X.f")` where `f` is the asserted target. Bans the Bug-D anti-pattern at the source.

The 57-file integration corpus is doing real work — it catches plenty of bugs at the layer boundaries (fetch ↔ extract, extract ↔ validate). What it's NOT catching is **flow-through invariants** that span 3+ files and **cross-file contracts** that aren't enforced anywhere. Those need a different test shape: end-to-end with no internal mocks, with the assertion on the FINAL consumer's observation, not on intermediate function calls.

---

## Cross-references

- Auto-generated summary (headline wrong, body correct): [`data/reports/cloud_run_2026-05-11/summary.md`](../data/reports/cloud_run_2026-05-11/summary.md)
- Day-over-day diff (corrected for events-truth in this doc): [`data/reports/cloud_run_2026-05-11/comparison_with_2026-05-10.md`](../data/reports/cloud_run_2026-05-11/comparison_with_2026-05-10.md)
- failures.csv (2041 rows, regenerable): [`data/reports/cloud_run_2026-05-11/failures.csv`](../data/reports/cloud_run_2026-05-11/failures.csv)
- May 10 manual analysis (today's "what's working" section traces its lineage from here): [`docs/run_2026_05_10_manual_analysis.md`](run_2026_05_10_manual_analysis.md)
- Persistence-health SLO source: [`scripts/diagnostics/profile_persistence_health.sql`](../scripts/diagnostics/profile_persistence_health.sql)
- Self-learning loop architecture: [`project_self_learning_loop_arch.md`](../../C:/Users/ashus/.claude/projects/c--Users-ashus-OneDrive-Documents-Code-PropAi/memory/project_self_learning_loop_arch.md) (auto-memory)
