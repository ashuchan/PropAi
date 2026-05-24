# 2026-05-24 Session 2 handover — HAR-driven cluster grind

**Branch:** `claude/portal-hop-may19` (worktree `~/PropAi-main/.claude/worktrees/angry-murdock-c19e06/`)
**Baseline state at session start:** 78.0% strict on canary focused-3886351 (commit `3886351`)
**6 commits shipped this session** — all green, all HAR-validated where possible.

## Commits shipped (in chronological order)

| Commit  | Subject                                                      | Tests | HAR-validated lift                  |
|---------|--------------------------------------------------------------|-------|-------------------------------------|
| `5a14a8d` | Entrata PP-SSR parser (Templates A `.fp-card`, B `.fp-group-item`) | 13   | 35/47 ENTRATA_EMPTY HARs (74%)      |
| `6f1a974` | Fix Entrata→SightMap fp-subpage splice (FetchResult frozen)  |  5   | Silent fail × 7+ in prior canary    |
| `efa0d0e` | GenericPlanText static-body fallback (no live-page bail)     |  9   | 10/15 GENERIC_PLAN_TEXT HARs (66%)  |
| `302149b` | Web Unlocker URL encoding (`[` `]` → `%5B` `%5D`)            |  8   | Fixes 78 HTTP 400s in Unlocker test |
| `64d313c` | Web Unlocker per-process call cap (`WEB_UNLOCKER_MAX_CALLS_PER_JOB`) |  9   | Budget guard (prevents $9 burn)   |
| `d8ee2c9` | Entrata PP-SSR Template C `.unit-item` (HAR-driven)          |  4   | +5/47 ENTRATA_EMPTY → 40/47 (85%)   |

**Total new tests:** 48. **No regressions** in 1896 pms-suite tests.

## Validation methodology

Two HAR-capture archives provided this session (313 HARs total):
- `/Users/ankur/Downloads/HAR FILES (2).zip` — 148 HARs
- `/Users/ankur/Downloads/HAR_201-400.zip` — 165 HARs

Cross-referenced with the focused-3886351 canary's 953 failed-strict properties:
- **213 failed-strict props have matching HARs** (host-name match)
- This subset is what I locally validated against

## Cohort coverage (HAR-validated, %)

| Cluster | Total in baseline | HARs matched | HAR-validated lift % |
|---|---:|---:|---:|
| **TIER_1_API_ENTRATA_EMPTY** | 103 | 47 | **85%** (40/47) — A/B/C |
| **TIER_1_DOM_GENERIC_PLAN_TEXT** | 67 | 15 | **66%** (10/15) |
| **TIER_1_API_ENTRATA_SHAPE_REJECTED** | 41 | 6 | **50%** (3/6) — bonus from PP-SSR |
| generic:no_body_short_circuit | 136 | 33 | not re-validated (a303462 + 302149b shipped pre-session) |
| TIER_1_API_RENTCAFE_SHAPE_REJECTED | 105 | 10 | not re-validated (ef75170 shipped pre-session) |

**Projected combined lift** (extrapolating HAR-validated % to full cohort sizes):
- ENTRATA_EMPTY: ~88 props (103 × 0.85)
- GENERIC_PLAN_TEXT: ~44 props (67 × 0.66)
- ENTRATA_SHAPE_REJECTED: ~21 props (41 × 0.50)
- Subtotal from this session's 3 parser fixes: **~150 strict-pass lift**
- Plus prior-session shipped (a303462 + ef75170): another **~100-150 props**
- **Combined: 250-300 strict-pass lift = +5-6pp**
- Baseline 78% + 5-6pp = **83-84% strict** projected

⚠️ This is an upper bound — assumes HAR-matched success rate generalizes to non-HAR-matched props, which is optimistic. Realistic projection: 80-82% post-canary.

## Open clusters (not yet fixed this session)

### Queued chip tasks (well-defined, ready to pick up)
1. **TIER_3_DOM plan-only → subpage rent merge** (~13 props lift)
   - Full probe data: `/tmp/tier3_dom_full_probe.json`
   - 9 of 33 confirmed have rent at `/floorplans` subpage; 4 have rent on homepage but parser missed it
2. **OneSite SPA: find rendered API endpoint** (~27-36 props lift)
   - Found endpoints in HARs: `leasing.realpage.com/RP.Leasing.AppService.WebHost/InitialAppSettings/v1?SiteId={ID}` (config, 62KB) + `/workflowstartup/v1/{ID}/English` (workflow + units)
   - SiteId discovery: `{prefix}.onlineleasing.realpage.com/` HTML has `widgetLoader.js?siteId=([0-9]+)`
   - For G5-managed sites: `marketing-center-data.g5devops.com/summary/.../*.json` body has `partnerpropertyId`
   - Fix not shipped — endpoint structure needs deeper inspection to find the unit-list payload
3. **Investigate test_resman.py import error** (`_move_in_date` symbol missing — pre-existing, blocks the pms test sweep file)

### Other cohorts identified but not investigated
| Cluster | HAR-matched count | Notes |
|---|---:|---|
| **TIER_1_API_SIGHTMAP_SHAPE_REJECTED** | **7** | **5/7 have rich JSON at `sightmap.com/app/api/v1/{TOKEN}/sightmaps/{ID}` (48-77KB, units + floor_plans + total_display_full_price). Adapter uses `u.get("price")` — needs `total_display_full_price` fallback. Chip task spawned.** |
| TIER_1_API_RENTMANAGER | 6 | Not investigated |
| TIER_1_KNOCK_API | 6 | 3/6 have rich HAR responses (`/v1/property/{ID}/units`); 3/6 are Knock-detector false-positives. Splittable into 2 sub-fixes. |
| SYNDICATION_ONLY_WIX | 5 | Not investigated |
| TIER_1_API generic | 18 | 1/18 has extractable JSON (Knock-related). Most are operator-doesn't-publish-via-API. |
| ENTRATA_EMPTY residue | 7 | The 7/47 untemplated still failing — likely Template D (different operator) or operator-data-gap |
| GENERIC_PLAN_TEXT residue | 5 | 1/5 (livealexanderpointefl) has a per-unit leasing table pattern: `"Apt # 1603 $1,478.00 /mo* | 10 months $1,415.00 Base rent"`. The other 4 (rooftop252, morrowapartments, olivboulder, autumnaugust) have substantial body but no rent visible — likely JS-rendered widget or true operator gap. |

### no_body_short_circuit (136 cohort, 33 HAR-matched)
Did NOT re-validate this session. HAR sweep showed:
- 24/33 first-HTML response = 200 OK with ≥100KB body
- 8/33 = 404 (truly dead URLs — should be DEAD_URL not no_body)
- 1/33 = 301 redirect
- **18/33 have floorplan + rent data somewhere in their HARs** — recoverable

The 8 404s suggest a possible improvement to the DEAD_URL classifier. The shipped `a303462` (curl_cffi direct-first) should catch many of the 18 recoverable ones; the 302149b URL-encoding fix removes another failure mode. Worth re-validating in a fresh canary.

## Cost note — Web Unlocker test

- **Cancelled** `jugnu-unlocker-test-3886351-fl9gv` mid-flight after burning **3,180 calls** = **~$4.65-$9.30** (depending on BD tier rate)
- User's ceiling was $2-3; the cancellation saved another ~$2-5 from the 2 remaining shards
- **Two production bugs caught** by that test run and fixed this session:
  - `cannot assign to field 'body'` Pydantic immutability error (commit `6f1a974`)
  - HTTP 400 on bracketed URLs (commit `302149b`)
- **New safeguard** for future runs: set `WEB_UNLOCKER_MAX_CALLS_PER_JOB=500` (recommended) — caps each shard at ~$0.75-$1.50

## How to resume

### 1. Push the branch
```bash
git push origin claude/portal-hop-may19
```

### 2. Re-measure on canary (highest ROI)
Kick a focused canary with all 6 shipped commits on the union of failing-cohort PIDs:

```bash
# Use the existing failing-1580 CSV from prior session as the focused cohort
# (gs://jugnu-canary/property-list/failing_1580_2026-05-23.csv)
# Set env vars: ENABLE_UNLOCKER_TIER=true, WEB_UNLOCKER_MAX_CALLS_PER_JOB=500

# See SESSION_STATE.md → "How to resume next session" for the full runbook
```

Expected outcome: 80-82% strict (from 78% baseline).

### 3. If still below 80%, attack remaining cohorts
Priority order based on lift potential:
1. **OneSite InitialAppSettings adapter** (~27-36 props) — queued chip task
2. **TIER_3_DOM subpage rent merge** (~13 props) — queued chip task
3. **Knock detector false-positive fix** (~3-6 props) — needs investigation
4. **ENTRATA_EMPTY Template D** for the residual 7 — needs HAR review

### 4. Key files
- HAR archives: `/tmp/har_analysis/batch1/HAR FILES/`, `/tmp/har_analysis/batch2/HAR_201-400/` (will be cleared on /tmp prune)
- HAR extracts: `/tmp/har_analysis/extracts/all_unit_endpoints.json`, `json_endpoints_per_host.json`
- Failed-prop ↔ HAR mapping: `/tmp/har_analysis/matched_failed_props.json`
- TIER_3_DOM full probe results: `/tmp/tier3_dom_full_probe.json`
- ENTRATA_EMPTY cohort: `/tmp/entrata_empty_cohort.json`

## Test discipline this session

Every commit includes:
- Tests pinning the behavior (regression-proof)
- Live fixtures (HAR-captured HTML where applicable)
- `ruff` clean
- `pytest ma_poc/tests/pms/` sweep no-regression (1896/1896)
- No new `mypy` errors

## Pitfalls / lessons learned

1. **Frozen Pydantic dataclasses bite silently**: `FetchResult.body = x` raises `FrozenInstanceError` and the surrounding try/except swallows it as a WARNING. Always use `dataclasses.replace`.
2. **HTTP entities without `;` survive BS4**: `&nbspSqFt` in greenwoodsapts.com tripped the sqft regex. Solution: tolerate `[\s&\w]{0,8}?` window in the regex.
3. **CF rate-limits per-IP burst**: probing 6 OneSite sites in a row from my Mac IP triggered 403s; back off + spread probes.
4. **Static-body fallbacks need orchestrator awareness**: `GenericPlanTextAdapter` returning units pre-empts the LLM cascade. When patching adapters that previously no-op'd in stub-page cases, also extend any `_suppress_deterministic_tiers` test helpers.
5. **OneSite SPA SiteId discovery has at least 3 paths**: `widgetLoader.js?siteId=...` (direct portal), `marketing-center-data.g5devops.com/summary` (G5-managed), `CmsSiteManager/callback.aspx?act=Proxy/GetFloorPlans` (legacy). A complete OneSite adapter needs to try all three.
