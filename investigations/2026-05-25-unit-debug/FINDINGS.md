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

