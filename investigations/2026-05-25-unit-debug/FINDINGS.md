# Unit-data deep probe — findings (2026-05-25)

## Setup

- 40 props sampled from `n_full_post_fix=0` (934 props / 18.7% of canary 1ef1060)
- 40 props sampled from `low_strict` (n_units>0 but <80% strict-pass)
- Stratified across 14+ extraction tiers (skipping tiers already owned by chips #96 #98 + already-shipped fixes)
- 10-step probe contract per prop (steps 1-5, 7, 9 via curl_cffi chrome120; steps 6, 8 reserved for Chrome MCP residue)
- Total: 80 props × 9 fetches = ~720 HTTP requests in <2 minutes

Artifacts under `investigations/2026-05-25-unit-debug/`:
- `build_worklist.py` → `artifacts/probe/{cohort}_worklist.jsonl`
- `probe_runner.py` → `artifacts/probe/{cohort}_results.jsonl` (resumable, append-only)
- `cluster.py` → `artifacts/clusters/{cohort}_clusters.{md,json}`

---

## High-value clusters (cross-cohort)

### A. WordPress-backed apartment sites — operator-data-gap + 1045-on-the-Park signature
**Cohort:** 4 props in n_full=0, likely ~30-50 across full canary if extrapolated by `api.w.org/` JS hint.

**Finding:** All 4 sites expose `wp-json` but have ONLY stock CPTs (`post, page, attachment, nav_menu_item, wp_block`) — NO custom property/floorplan/unit CPT. Unit data is either:
- (a) absent entirely (operator-data-gap, e.g. 3 of 4 props), OR
- (b) baked into Elementor widget text inside the home page body, e.g. **1045 on the Park** has:
  ```
  Residences starting at $2,127
  Starting at $2,865 2 Bed | 2 Bath
  Starting at $2,509 2 Bed | 2 Bath
  Starting at $2,127 1 Bed | 1 Bath
  ```

**Why generic_plan_text misses it today:**
1. **Routing:** 3 of 4 WP sites were routed to TIER_1_API (probably empty JSON response) — generic_plan_text never tried.
2. **Regex:** Even when tried, `_PLAN_LINE_RE` only looks FORWARD 100 chars for rent. The "Starting at $X" precedes the bed/bath token — adapter misses it. (Backward-lookup exists ONLY for the property-level "from $X" fifth-pass fallback, not for per-plan rows.)
3. **Boundary regex bug:** `_NEXT_PLAN_BOUNDARY_RE` matches `bedroom|bdrm|bd|br` but NOT bare `bed`. When forward-window crosses into the next plan (`"$2,509 2 Bed | 2 Bath"`), the boundary check doesn't fire → rent gets cross-assigned to the wrong plan.

**Suggested fix:** add backwards-lookup (~60 chars) for "Starting at $X" / "From $X" / standalone "$X" in `generic_plan_text.py`; extend `_NEXT_PLAN_BOUNDARY_RE` to include `bed`. Estimated lift: 1045 on the Park + likely 20-40 other Elementor-style sites in the GENERIC_PLAN_TEXT cohort.

### B. New platform — ThinkRESIDE (`thinkresite.dev`)
**Cohort:** 1 in sample (`Orchard Ridge`), needs full-canary sizing.

**Signature:** `https://api.thinkresite.dev/neighborhoods/{id}` + `https://forms.thinkresite.dev/api/submit/...`. Vanity host `liveatorchardridge.com`. Tier was TIER_MERGED_CROSS_PAGE (zero full rows).

**Action:** Size cohort by grepping the canary properties for "thinkresite" or "thinkreside" markers; if ≥3 props, ship dedicated adapter.

### C. AppFolio `/availability` sub-page carries unit markers
**Cohort:** 1 in n_full=0 (`SCS Athens`), 2 in low_strict.

**Signature:** AppFolio vanity sites where `/availability` static curl returns unit markers but the existing AppFolio adapter only visits `/listings` via the embed-JS host. Suggests SCS Athens has a custom availability page **outside** the AppFolio embed widget.

**Action:** Probe whether `/availability` carries the canonical unit list (so the AppFolio adapter should add `/availability` as a fallback path) OR is a DIFFERENT CMS the detector is misrouting.

### D. MarketApts `/floorplans` index but no per-unit drill
**Cohort:** 2 in sample (`Brookstone`, `Hill Country Villas`); 15 total in n_full=0 cohort.

**Signature:** TIER_1_DOM_MARKETAPTS adapter routes correctly but emits 0 rows. The /floorplans index page has plan tiles but the per-unit pages aren't being walked. Needs Chrome MCP click-through to identify the unit page URL pattern.

**Action:** Spawn chip — Chrome MCP probe Brookstone + Hill Country Villas to find the unit-page URL pattern.

### E. FLOORPLAN_INDEX_NO_UNITS no-fingerprint cluster
**Cohort:** 4 in low_strict (`Signal Pointe`, `Heritage at Boca Raton`, `Parker Towers`, `The 101 Kirkland`).

**Signature:** Static /floorplans probe returns HTML but no PMS fingerprint and no obvious unit-page links. These might be custom in-house CMSes. Tier distribution: TIER_MERGED_CROSS_PAGE × 2 + TIER_1_API + TIER_1_API_REPLI360.

**Action:** Spawn chip — Chrome MCP rendered DOM probe to identify the unit display mechanism.

---

## Lower-value clusters

| Cluster | Props | Note |
|---|---:|---|
| FETCH_ERROR (status=0) | 2 | DNS/connection drops — `liveatthearia.com`, `risejulington.com`. Fetcher escalation already handles. |
| BLOCKED_HTTP_403 | 3 | Fetcher escalation already shipped (`59b9102`). Sucuri walls. |
| G5 NO_UNITS / no unit-API in JS | 5 | Likely operator-data-gap. Verify with site-by-site eyeball, then flag. |
| FINGERPRINT_*_NO_UNITS (existing adapter returned 0) | 8 | Each one is a specific adapter debug task; lower leverage than the WP/MarketApts clusters. |
| NO_FINGERPRINT_NO_API (404 landing) | 2 | Dead URL — `villaserenacommunities.com/rockridge-park/`, `forestridgebloomington.com`. Out-of-scope cleanup. |

---

## Recommended next actions

| # | Action | Type | Estimated lift |
|---|---|---|---|
| 1 | `generic_plan_text.py` — backwards rent-lookup + `_NEXT_PLAN_BOUNDARY_RE` bed-fix | Inline ship | 20-40 props |
| 2 | ThinkRESIDE adapter (after canary-wide cohort sizing) | Chip | depends on sizing |
| 3 | MarketApts unit-page drill (Chrome MCP probe + parser) | Chip | 15 props |
| 4 | AppFolio `/availability` fallback path | Chip | 3 sample → ~15-30 extrapolated |
| 5 | FLOORPLAN_INDEX_NO_UNITS no-fingerprint cluster — Chrome MCP probe | Chip | 4 sample → unknown |

Action 1 doesn't conflict with any in-flight chip (`_parsing.py` chip owns shared regex; `generic_plan_text.py` is a separate adapter file).

---

# Wave 2 (2026-05-25, 160 fresh props) — top clusters

| # | Cluster | Count | Action |
|---|---|---:|---|
| 1 | NO_FINGERPRINT_NO_API | 24 | SPAWN CHIP: Chrome MCP rendered DOM probe — custom CMSes / JS-only widgets |
| 2 | HAS_UNIT_MARKERS_AT_2_floorplans rentcafe,securecafe | 16 | FALSE POSITIVE confirmed on 3 — heuristic counted JS `unit_number` variable. Real SecureCafe drill machinery handles these; chase what's blocking it (anti-bot? timing?) |
| 3 | FLOORPLAN_INDEX_NO_UNITS cloudflare,entrata,sightmap | 12 | SPAWN CHIP: Cloudflare-fronted Entrata+SightMap drill |
| 4 | FLOORPLAN_INDEX_NO_UNITS marketapts | 8 | Already chip-queued |
| 5 | FETCH_ERROR | 5 | DEFER — DNS / hard fetch fail |
| 6 | FINGERPRINT_g5_NO_UNITS | 5 | Likely operator-data-gap — verify + flag |
| 7 | FLOORPLAN_INDEX_NO_UNITS wordpress | 4 | Same Elementor signature as 1045 on the Park; existing fix may already cover |

---

# sqft=-1 probe (52 props, 3,483 total units in cohort)

**Triage:**
- **42 SQFT_TRULY_ABSENT (81%)** — operator-data-gap; not a bug, flag it
- **9 SQFT_FOUND_AT_*** (17%) — adapter miss, sqft IS on site
- **1 BLOCKED** — defer

**Per-tier extraction-miss rate (sample):**

| Tier | Sample | Adapter miss % | Cohort units |
|---|---:|---:|---:|
| TIER_1_DOM_APPFOLIO_VANITY | 8 | 0% | 843 |
| TIER_1_DOM_APPFOLIO_VANITY_PLAN_LEVEL | 3 | 0% | 252 |
| TIER_1_DOM_GENERIC_PLAN_TEXT | 8 | 38% | 773 |
| TIER_1_API_RENTCAFE_SECURECAFE | 6 | **67%** | 475 |
| TIER_MERGED_CROSS_PAGE | 5 | 20% | 326 |
| TIER_3_DOM | 5 | 20% | 211 |
| TIER_1_DOM_GENERIC_PLAN_TEXT_PLAN_LEVEL | 4 | 0% | 155 |
| Others (10 tiers, samples 2-4) | 25 | 0-25% | ~450 |

**Highest-leverage findings:**
1. **AppFolio sqft = TRUE operator-data-gap** (0% adapter miss, 100% absent on 11 sampled). 1,095 units across ~100 props affected — should be FLAGGED as `data_gaps=["sqft"]` not counted against quality.
2. **SecureCafe sqft = 67% adapter miss** — vanity site /floorplans pages do publish sqft but the SC drill doesn't FK-join from them. Patterns vary across sites (`"1 Bedroom, 1 Bathroom 700 sq. ft."` on vestaviaplace, `"B5 2 Bed / 2 Bath / 1119 sq ft"` on ardencebloom, JSON-in-script on themtroyal/alvista23). Universal fix is non-trivial — best as a chip.
3. **GENERIC_PLAN_TEXT sqft = 38% adapter miss** — sqft is on `/floorplans` subpage but adapter operated on landing-page body. Orchestrator should chase subpages first; `_generic_dom_floorplans.py` may already handle but isn't being called consistently.

---

# Net actionable from waves 1+2+sqft probe

| Action | Status |
|---|---|
| #1 ChromeMCP probe NO_FINGERPRINT_NO_API cluster (24p w2 + ~16 in main cohort) | **SPAWN CHIP** |
| #2 SecureCafe sqft FK-join from vanity /floorplans | **SPAWN CHIP** (multi-pattern, multi-day) |
| #3 GENERIC_PLAN_TEXT orchestrator subpage chase | **SPAWN CHIP** (touches orchestrator) |
| #4 AppFolio data_gaps=["sqft"] flag | **SPAWN CHIP** (needs canary-export changes to make visible) |
| #5 Cloudflare Entrata+SightMap FLOORPLAN_INDEX cohort (12p) | **SPAWN CHIP** |

---

# Wave 3 (200 fresh n_full=0 props) — confirms pattern convergence

After 480 total probed props (51% of the 934-prop n_full=0 cohort), the same 5-6 cluster patterns repeat with diminishing returns. The remaining ~454 unprobed props will mostly fall into the same buckets.

| Cluster | W1 | W2 | W3 | Total | Status |
|---|---:|---:|---:|---:|---|
| NO_FINGERPRINT_NO_API | 2 | 24 | 17 | ~43 | Chip spawned (Chrome MCP probe) |
| RentCafe/SecureCafe HAS_UNIT_MARKERS (false positive) | 2 | 16 | 21 | ~39 | Existing SC drill handles; heuristic miscount |
| Cloudflare+Entrata+SightMap FLOORPLAN_INDEX | 1 | 12 | 15 | ~28 | Chip spawned |
| MarketApts FLOORPLAN_INDEX | 2 | 8 | 6 | ~16 | Chip already queued |
| WORDPRESS_BACKED | 5 | 3 | 8 | ~16 | Mostly operator-data-gap + Elementor (1045-on-the-Park inline fix shipped) |
| FINGERPRINT_g5_NO_UNITS | 5 | 5 | 4 | ~14 | Operator-data-gap (G5 sites don't publish units) |
| FINGERPRINT_cloudflare_NO_UNITS (NEW IN W3) | — | — | 9 | 9 | CF challenge blocks static probe — needs Chrome MCP or Web Unlocker |
| FINGERPRINT_wix_NO_UNITS | 1 | — | 6 | 7 | Syndication-only flag exists; verify applied |
| FINGERPRINT_squarespace_NO_UNITS (NEW IN W3) | — | — | 5 | 5 | Syndication-only flag exists; verify applied |

---

# Session shipping summary

**Inline commits shipped this session:**

| Commit | Subject | Lift signal |
|---|---|---|
| `ae593e8` | generic_plan_text Elementor "Starting at $X" backwards-lookup | 1045 on the Park + ~20-40 Elementor sites |
| `cc6a2b0` | AppFolio: skip non-housing listings (parking, storage, garage) | 17+ false-positive parking rows dropped, accurate unit counts for ~15 AppFolio props |

**Plus 9 merged chip outputs:**
- AppFolio propertyGroup filter, RentalAddress (Cedar Ridge), RentVision per-plan drill, Entrata PP per-plan drill, EdificeCMS/Cobblestone, Reinhold/ChocolateWorks, PRG/FortressTech, AppFolio Academy Place address filter, parsing regex bundle (#101 #102 #105 #106)

**Plus 11 chips queued for follow-up:**
- ThinkRESIDE adapter, AppFolio /availability sub-page, MarketApts unit-page drill, FLOORPLAN_INDEX_NO_UNITS probe, NO_FINGERPRINT Chrome MCP probe (24+), SecureCafe sqft FK-join, Cloudflare Entrata+SightMap drill, AppFolio sqft data_gaps flag, GENERIC_PLAN_TEXT subpage chase, SightMap zero-rent skip

**Probe infrastructure committed:**
- 4 worklist builders, 1 generic probe runner, 1 sqft-specialised probe runner, 2 cluster analyzers
- ~480 probed properties + ~720 fetches per wave, fully resumable via per-prop pid keyed JSONL

**Coverage:** 480 / 934 n_full=0 props probed (51%) + 52 sqft=-1 props + 40 low-strict = 572 distinct properties deep-probed.

**Pattern convergence:** Wave 3 surfaces no NEW cluster types beyond Wave 2, confirming the canary's residue distribution. The remaining ~450 unprobed n_full=0 props will mostly fall into the same buckets — additional waves give marginal new signal.

---

# Wave-2 cluster #3 drill — Cloudflare + Entrata + SightMap (12 props)

**Drill date:** 2026-05-25
**Cohort source:** `.claude/worktrees/angry-murdock-c19e06/investigations/2026-05-25-unit-debug/artifacts/probe/n_full_zero_w2_results.jsonl` — `verdict=FLOORPLAN_INDEX_NO_UNITS` AND `5_fingerprints == {cloudflare, entrata, sightmap}` (12 properties).

## Cluster thesis vs. reality

The cohort name implies the failure mode is *"CF JS challenge blocks the SightMap iframe load."* Live-probing 4 of the 12 with Chrome MCP shows the thesis is wrong for the majority.

| Property (pid) | Landing | CF challenge fired? | Real unit path | Verdict |
|---|---|---|---|---|
| High Grove (55299) | highgrovegeorgia.com | **Yes** (interstitial on `/conventional/`) | After CF clears, `/conventional/` has `fp-group-item` + `fp-name-link`; per-plan `/floorplans/.../{slug}-{fpid}-1/` renders `.unit-card` markup but every plan says "No Units available currently" | **(C)** Genuinely 0-unit (fully leased) |
| Revive → The Lakes (42085) | reviveapartments.com → lakesatfife.com → thelakeslive.prospectportal.com | No | `/conventional/` exposes per-plan URLs; per-plan page has 5 `.unit-card` rows ($1,697 / 800 sqft / Available Now). Body has `fp-name-link` only — **no `fp-card` / `fp-group-item`** | **(D)** new-theme drill-gate bug |
| 14Fifty Neo (258254) | 14fiftyapartments.com | No | `/kissimmee/14fifty-neocity/conventional/` uses new `beans-floorplans-map-tabs-wrapper` theme with `fp-name-link` only; per-plan page renders 5+ `.unit-card` rows with real availability | **(D)** new-theme drill-gate bug |
| Brazos Ranch (35778) | brazosranch-apts.com | No | `/conventional/` has a real `<iframe src="https://sightmap.com/embed/n9w6m4lmv71">`; per-plan URLs use a different format and don't render `.unit-card` — units live in the SightMap iframe | **(A)** real SightMap iframe (existing sightmap.py adapter probes `/conventional/` for embed codes — should already cover) |

## Why the static probe verdict is misleading
- `5_fingerprints` `cloudflare` tag fires on `cf-ray` headers / `__cf_bm` cookies — i.e. any CF-fronted CDN, **not** an active JS challenge. Only 1 of 4 live-probed properties surfaced the actual interstitial.
- `sightmap` fingerprint fires whenever any path contains `sightmap.com/...`. In 3 of 4 cases the only match was the static `embed/api.js` loader — no actual SightMap iframe.
- The static recon probe only checks shallow paths (`/floorplans`, `/floor-plans`, `/availability` at root) plus WordPress endpoints. It never fetches the deep `/{city}/{slug}/conventional/` URL, so the verdict `FLOORPLAN_INDEX_NO_UNITS` is "no markers at the shallow index" — not "deep path is empty."

## Real bug: Entrata PP unit-card drill gates out the new theme

`ma_poc/pms/adapters/entrata.py` already ships a Prospect-Portal unit-card drill (commit c5642d2, canary 1ef1060 regr#9) that probes the captured body and deep `/conventional/`-style URLs, then iterates plan links via `find_entrata_pp_plan_links` and parses each per-plan body with `parse_entrata_pp_unit_cards`.

**The step-1 (line 1507-1510) and step-3 (line 1570-1574) gates admit a body only when `fp-card` OR `fp-group-item` is present.** The newer PP theme `beans-floorplans-map-tabs-wrapper` (14Fifty Neo, The Lakes, likely several more in the cluster) uses `fp-name-link` only — bodies are silently dropped, the drill never iterates plan URLs, and the adapter emits 0 units.

## Fix shipped (this commit)

- **[ma_poc/pms/adapters/entrata.py](ma_poc/pms/adapters/entrata.py)** — broaden both gate predicates to also accept `fp-name-link`. `find_entrata_pp_plan_links` already lists `.fp-name-link` as its first selector (entrata.py:1219), so once the body is admitted, link discovery and per-plan parse work unmodified.
- **[ma_poc/tests/pms/adapters/test_entrata_pp_unit_drill.py](ma_poc/tests/pms/adapters/test_entrata_pp_unit_drill.py)** — regression guard `test_find_pp_plan_links_beans_floorplans_map_theme` pins a synthetic 14Fifty-style body (no `fp-card`, no `fp-group-item`, no `unit-item`) and asserts plan URLs are still emitted.

Test run: `pytest ma_poc/tests/pms/adapters/test_entrata*.py` → 64 passed, 1 skipped. `ruff check` clean.

## What this fix does NOT cover
- **High Grove (55299)** — CF JS challenge fires on `/conventional/`. The drill's `_entrata_static_fetch` is curl_cffi-based and won't pass the challenge. Moot here because the property is genuinely empty post-challenge anyway, but the cluster has a CF-walled subset that needs Web Unlocker or browser-level rendering.
- **Brazos Ranch (35778)** — real SightMap iframe variant with a per-plan URL format that doesn't render `.unit-card`. Recovery lives in `sightmap.py` (already probes `/conventional/` for embed codes); needs separate verification that it fires for this property.
- The remaining 8 cluster props haven't been individually probed — the fix should lift the new-theme cases among them; residue is either CF-walled (High Grove pattern) or real SightMap iframe (Brazos Ranch pattern).
