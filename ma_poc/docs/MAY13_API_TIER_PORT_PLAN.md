# May-13 API-Tier Port Plan — single-PR strategy

**Date:** 2026-05-20
**Source branch:** `origin/fix/resolver-path-patterns-may13`
**Target branch:** `main`
**Scope:** Port resolver patterns + detector PMS-disambiguation gates + 7 surgical existing-adapter API-tier improvements + 10 new Tier-1 adapters from the May-13 feature branch into main, as a single PR gated by a stratified canary.
**Status:** Planning — no code changes yet.

Companion docs:
- [LOCAL_CANARY_PLAN.md](LOCAL_CANARY_PLAN.md) — canary tooling already exists; this plan consumes it.
- [JUGNU_BASELINE.md](JUGNU_BASELINE.md) — baseline metrics reference.
- [failed_no_data_debugging_playbook.md](failed_no_data_debugging_playbook.md) — failure-bucket taxonomy referenced in stratified sample.

---

## 0. Context

The May-13 feature branch contains 156 commits and 417 changed files. Four parallel deep-dive reviews against `main` revealed:

1. **The branch's per-adapter API-tier improvements account for the unit-yield delta** the team observed. Specifically: RealPage OLL stateful workflow capture (entirely new capability), Entrata `available_date` 7-alias lookup + 5-path probe, SightMap broadened embed discovery, RentCafe SecureCafe drill-down + Nestin recovery, AppFolio cross-origin iframe recovery.
2. **The branch's shared-helper changes regress main.** Branch removes the plan/unit FK-pair detector from [_api_parser.py](../pms/adapters/_api_parser.py) (main commit `bfeda6c` added it), removes the R0 rank-matching guard from [_merge_fns.py](../pms/adapters/_merge_fns.py), removes 4 fields from [base.py](../pms/adapters/base.py) that main heavily uses.
3. **Main has progressed on parsing, availability, and concession quality** since the branch diverged (commits `83567cf`, `0e5a11e`, `0626fa6`, `50ff8b3`, `c9bf722`). Branch versions of [extraction/](../extraction/), [validation/schema_gate.py](../validation/schema_gate.py), [appfolio.py](../pms/adapters/appfolio.py) `_ADDRESS_RE`, [rentcafe.py](../pms/adapters/rentcafe.py) availability-status logic, [amli.py](../pms/adapters/amli.py) partition gate, and concession code are all *older* than main's equivalents. Taking any of them backwards.
4. **Path B (empty-exit retry), interactive reveal, and the 3.8 K-line scraper.py rewrite are orchestration-coupled** and cannot be ported piecemeal. Defer to a dedicated effort.

The job of this PR is to take the per-adapter wins and the additive resolver/detector changes, while explicitly **NOT** taking the shared-helper regressions or main-already-ahead refactors.

---

## 1. Goals + expected yield

| Source | Estimated property uplift | Yield class |
|---|---|---|
| Resolver path + portal patterns | ~130–180 | Coverage (unblocks failed resolution) |
| Detector PMS-disambiguation gates | ~60 hard-recoveries + correct routing for ~250 | Routing |
| Entrata `available_date` aliases + 5-path probe | Fleet-wide move-in capture (was ~0%) | Field quality |
| OneSite three-label outcomes | Unblocks Phase-B retry for ~70 properties | Routing |
| SightMap embed broadening | ~70% of SHAPE_REJECTED bucket | Yield |
| AppFolio cross-origin iframe | ~26 properties | Yield |
| RentCafe SecureCafe + WP + Nestin + hosted-table | ~1,000 plan→unit promotions | Yield |
| RealPage OLL workflow parser | ~187 properties (Category D, net-new) | Yield |
| 10 new Tier-1 adapters | ~1,000+ (G5, Equity, Knock, Cortland, apts247, Irvine, RentManager, Essex, MAA, RentVision) | Coverage |
| Value-parsing surgical fixes | Deposit→rent leakage, JSON-blob FP names | Quality |

**Cumulative target:** ≥2,000 properties newly unit-level extracting, ≥5% total unit-yield uplift across the 500-property canary, Tier 1+2 share ≥40% (CLAUDE.md gate).

---

## 2. Hard exclusions (do NOT port)

| Item | Reason |
|---|---|
| [base.py](../pms/adapters/base.py) | Branch removes 4 fields main uses (`hop_depth`, `candidate_portal_urls`, `floor_plan_signal_count`, `post_process_meta`) |
| [_api_parser.py](../pms/adapters/_api_parser.py) wholesale | Branch removes `_PLAN_FK_KEYS`, `_detect_plan_unit_pair()`, `_merge_units_with_plans()` — regresses Knock/RentManager `{layouts, units}` shapes (PIDs 253774, 268552 reproduce) |
| [_merge_fns.py](../pms/adapters/_merge_fns.py) wholesale | Branch removes R0 rank-matching guard → cross-page phantom inflation (Canyon Ridge 17→4, Olympic 49→3 in 2026-05-16 canary); removes `_nfk()` signal-engine normalisation (Entrata camelCase loss) |
| [extraction/](../extraction/) (especially `dates.py`) | Main has newer `format_loose_date()`, three-layer availability, raw-string date fallback |
| [validation/schema_gate.py](../validation/schema_gate.py) | Tightly coupled to main's parser; branch version is older |
| [scraper.py](../pms/scraper.py) | 3.8 K-line orchestration rewrite — needs dedicated port |
| [empty_exit.py](../pms/empty_exit.py), [interactive_reveal.py](../pms/interactive_reveal.py), `detect_pms_candidates()` | Orchestration-coupled to scraper.py rewrite |
| [generic.py](../pms/adapters/generic.py) (-935 lines) | Logic moved to per-adapter helpers; main works as-is |
| [appfolio.py](../pms/adapters/appfolio.py) `_ADDRESS_RE` revert | Regresses meridiapm 0/300 fix currently in main |
| [rentcafe.py](../pms/adapters/rentcafe.py) removal of `_is_rentcafe_candidate` host-gating | Third-party JSON (CallRail, GTM, Osano) mislabeled as RentCafe |
| [rentcafe.py](../pms/adapters/rentcafe.py) removal of `_UNAVAILABLE_TEXT_RE` / `_AVAILABLE_TEXT_RE` / `_detect_availability_status` | Hard-codes `AVAILABLE` on every row; loses 3-state inference |
| [amli.py](../pms/adapters/amli.py) loosened partition gate | Loses D16 strict unit-level/plan-level partition |
| [scripts/runners/jugnu.py](../scripts/runners/jugnu.py) -1.7 K refactor | Unrelated to accuracy |
| Concession / date / sanity / unit_validity work | Main already ahead |

---

## 3. PR specification

**Title:** `port: API-tier coverage from fix/resolver-path-patterns-may13 (resolver + detector + 7 adapters + 10 new PMSes)`

**Branch:** `port/may13-api-tier-coverage` off `main`

**Size estimate:** ~3–4 K lines across ~35 files

**Body must include (template in §9):**
1. Link to this doc.
2. Full canary diff table (baseline vs. candidate).
3. Win column (PIDs newly SUCCESS, grouped by adapter).
4. Regression column (any PIDs newly FAILED, with remediation commit).
5. Hard-exclusion table (verbatim from §2).
6. Memory-flagged bucket recovery sub-table.

---

## 4. Commit sequence

Commits land low-risk → high-risk so partial review remains possible and so an early bug is caught before later changes layer on. **Each commit must leave `pytest ma_poc/tests/` green** — never push a broken intermediate state.

Branch commits are referenced with their short SHAs from the source branch.

### Commit 1 — Baseline capture (no code)
Captured before any code changes:
- `pytest ma_poc/tests/ -v --tb=short 2>&1 | tee /tmp/baseline_tests.log`
- Stratified canary CSVs created (see §5.2), committed under `ma_poc/tests/canary/canary_500.csv` and `canary_50.csv`
- Baseline canary run on current main (see §5.3)
- Metrics persisted to `/tmp/baseline_500_metrics.json` and `/tmp/baseline_50_metrics.json`

### Commit 2 — Resolver URL + portal patterns
**Files:** [resolver.py](../pms/resolver.py)

Surgical additive ports — manual 3-way reconcile against current main. Do NOT overwrite main wholesale.

- `_PRIORITY_RES` word-boundary anchoring (fixes "uni" in "Communities" false match)
- ~14 new portal host whitelist entries: `securecafe.com`, `securecafenet.com`, `prospectportal.com`, `appfolio.com`, `knockrentals.com`, `doorway.knck.io`, `myresman.com`, `reslisting.com`, `rentcafewebsite.com`, `showmojo.com`, `apartmentsearch.com`, `selftournow.com`, `ovationco.com`, `yottareal.com`, `mriprospectconnect.com`, `spherexx.{app,com}`
- `_CTA_PATH_RE` new path patterns: `/conventional/`, `/floor-plans` variants, `/models`, `/availability`, `/listings`, `/onlineleasing`, `/interactive-site-map`, `/communities/*`, `/properties/*`
- `normalize_appfolio_url` allowlist-based query-param preservation (keeps `filters[property_list]=`)
- Two-pass candidate ordering (portals first, same-host CTA second), `_CANDIDATE_CAP = 8`

**Skip:** anything that imports `interactive_reveal` or `empty_exit`.

**Tests:** keep `tests/pms/test_resolver*.py` green; add 1–2 fixture tests for new patterns.

### Commit 3 — Detector PMS enum + disambiguation gates
**Files:** [detector.py](../pms/detector.py)

Prerequisite for Commits 10–13.

- Extend `PmsName` Literal with 12 new entries: `spherexx`, `knock`, `resman`, `essex`, `maac`, `irvine`, `cortland`, `equity`, `rentmanager`, `rentvision`, `encoreskyline_template`, `aspensquare`
- Matching `_STRATEGY_BY_PMS` entries (copy from branch)
- G5 weak/strong branches gated on absence of Knock/SecureCafe/ResMan markers (commits `d99da26`, `21c5607` from branch)
- Jonah/MeetElise branch gated on competing markers
- Engrain widget signal → yields `("sightmap", 0.88, ...)` when `data-unit` + `data-floorplan` + realpage.com script load present (commit `39bba7b`)
- RealPage OLL Category D yield (0.85) when `leasing.realpage.com` / `rp-leasing-widget` / `/content/apply#k=` markers present
- Entrata `_ENTRATA_REAL_MODULE_RE` negative lookahead skipping `/Apartments/module/application_authentication/`
- Knock regex relaxation (commit `0956c59`)

**Skip:**
- `detect_pms_candidates()` generator — needs Path B orchestration in scraper.py
- Refactor of `_iter_html_markers` return signature — keep main's first-match contract

**Tests:** new fixture-based gate tests using real properties from branch commit messages (Sawmill Station, altaaptstarga, beechmeadowaptsin, Foxchase, livemuseatl).

### Commit 4 — Value-parsing surgical fixes
**Files:** [_parsing.py](../pms/adapters/_parsing.py) — 3 targeted edits only, NOT wholesale port

- **`money_to_int()` regex fix** (branch lines 22–28): main's `re.sub(r"[^\d.]", "", s)` concatenates `"$1,200 - $1,400"` → `"12001400"`. Replace with `re.search(r"\d[\d,]*(?:\.\d{1,2})?", s)` taking first token → resolves to 1200 (low bound). Memory `project_run_2026_05_11` deposit→rent leakage fix.
- **`_unwrap_name_blob()`** (branch lines 409–441): strips JSON-blob floor-plan names like `{"name":"B06","provider_id":"..."}` (2,534 rows in prior runs).
- **Dual `availability_date` + `available_date` emission** (branch lines 522–523): schema reader reads short form; emit both.

**Tests:** add cases to `tests/pms/test_parsing*.py` with real-data samples from `data/runs/<recent>/`.

### Commit 5 — Entrata `available_date` aliases + 5-path probe
**Files:** [entrata.py](../pms/adapters/entrata.py)

Surgical additions; do NOT take the 3-tier orchestration rewrite (coupled to scraper.py).

- 7-alias `available_date` lookup (branch lines 122–145): `available_date`, `availableDate`, `availability_date`, `move_in_date`, `min_move_in_date`, `date_available`, `available_on`, `first_available_date`
- Thread `availability_date=avail_dt` into unit dicts (branch line 155)
- `_probe_known_endpoints()` via `page.evaluate(fetch...)` for 5 paths (branch lines 482–523): `/floor_plans/`, `/availability_pricing/`, `/property_info/`, `/floorplan_data/`, `/getfloorplans/`. Use `fetch(u, {credentials: 'include'})` to preserve cookies.
- `parse_entrata_widget_envelope()` for nested `widget_data.content.floor_plans.floor_plans[]` shape
- `parse_entrata_available_units()` for WordPress-mounted Entrata sites (HTML-entity-encoded `"available_units":[{id, name, available_on, price}]` blob)
- `parse_prospectportal_unit_spaces()` for Cloudflare-fronted `?module=check_availability&action=view_unit_spaces&property[id]=...&property_floorplan[id]=...` GET (HTML fragment with `<a class="unit-button" data-*>`)
- New tier label `TIER_1_API_ENTRATA_PROBE` distinguishing probe-recovered from captured units
- Fallback label `TIER_1_DOM_ENTRATA_WP` for WordPress-embedded units

**Tests:** fixture-based tests for each new parser; HTML samples from cloud logs.

### Commit 6 — OneSite three-label outcomes + alias lookup
**Files:** [onesite.py](../pms/adapters/onesite.py)

Surgical 25-line change at lines 216–241 + 7-alias lookup at lines 75–97.

- Replace single `TIER_1_API_ONESITE` outcome with three:
  - `TIER_1_API_ONESITE` — real units admitted
  - `TIER_1_API_ONESITE_EMPTY` — parsed rows failed validity gate
  - `TIER_1_API_ONESITE_NO_RESPONSE` — no RealPage-shaped responses captured (cluster #6 OLL-widget-shell pattern)
- 7-alias `available_date` lookup: `availableDate`, `firstAvailableDate`, `dateAvailable`, `minimumAvailableDate`, `availabilityDate`, `available_date`, `minAvailableDate`

**Skip:** removal of F7a (`_probe_realpage_units_endpoint`) and F7d (DOM `data-availability` augmentation) — keep main's logic.

**Tests:** verify new label classification on empty/no-response fixtures.

### Commit 7 — SightMap embed broadening + iframe fallback
**Files:** [sightmap.py](../pms/adapters/sightmap.py)

- Broadened `_SIGHTMAP_EMBED_URL_RE` (branch lines 54–65): matches `<a data-src="...">` Fancybox, `var EngrainedUrl = '...'` JS, plain `<a href=...>` anchors, JSON-escaped `\/` paths. Include value-position skip to exclude `"embed_url":"..."` config blobs.
- `_SIGHTMAP_DIRECT_API_RE` (branch lines 66–72) for Angular SPA hardcoded `sightmap.com/app/api/v1/{client}/sightmaps/{id}` URLs
- `_try_sightmap_iframe_fallback()` (branch lines 314–338): when no XHR captured, fetch embed page → read `__APP_CONFIG__.sightmaps[*].href` → call API directly
- `extract_sightmap_api_url()`, `find_sightmap_embed_codes()`, `find_sightmap_direct_api_urls()` helpers
- Three-tier error constants: `_TIER_SHAPE_REJECTED`, `_TIER_AMENITIES_ONLY`, `_TIER_PARSE_FAILED`
- `source_ids` field on unit dicts (`sightmap_unit_id`, `sightmap_floor_plan_id`)

**Tests:** fixture for equityapartments.com-style SHAPE_REJECTED recovery.

### Commit 8 — AppFolio cross-origin embed + vanity slug
**Files:** new [_appfolio_embed.py](../pms/adapters/_appfolio_embed.py), surgical adds to [appfolio.py](../pms/adapters/appfolio.py)

- Copy `_appfolio_embed.py` from branch as-is (276 lines, no removed-AdapterContext-field dependencies)
  - `_LIVE_APPFOLIO_SRC_JS` (direct iframe scrape)
  - `_LIVE_APPFOLIO_TENANT_JS` (any `*.appfolio.com` URL → extract tenant → synthesize `/listings`)
  - `_to_appfolio_listings_root()` URL canonicalisation
  - Sub-path probing via `_APPFOLIO_EMBED_SUBPATHS` (`/listings`, `/availability`, `/availableunits`)
- Add `find_appfolio_slug()` + `_APPFOLIO_SLUG_RE` + `_APPFOLIO_SKIP_SLUGS` to `appfolio.py` (branch lines 70–88)
- Add `source_ids` threading: `{"appfolio_listing_id": listing_id}` (SSR) and `{"appfolio_id": item.get("id"), "appfolio_unit_id": item.get("unit_id")}` (API)

**Skip:** branch's `_ADDRESS_RE` revert at branch line 159 — it regresses main's meridiapm 0/300 fix.

**Tests:** Wix/Squarespace shell + vanity-domain fixtures.

### Commit 9 — RentCafe SecureCafe + WP + Nestin + hosted-table
**Files:** new [_rentcafe_nestin.py](../pms/adapters/_rentcafe_nestin.py), new [_rentcafe_hosted_table.py](../pms/adapters/_rentcafe_hosted_table.py), surgical adds to [rentcafe.py](../pms/adapters/rentcafe.py)

Largest single-commit yield in the PR (~1,000 properties).

- Copy `_rentcafe_nestin.py` (694 lines) from branch as-is. Covers 89% of JSON-LD ALL_fail cluster via two layouts (table + card) and `applyGAClick` button onclick variant for Stonewater.
- Copy `_rentcafe_hosted_table.py` (146 lines) from branch as-is. Parses `<tr class="fp-unit" data-unit-*>` rows on rentcafe.com hosted vanity domains.
- Add `_try_rentcafe_securecafe_probe()` + `_find_securecafe_base()` to `rentcafe.py` (branch lines 440–470). Drills SecureCafe online-leasing portal; re-fetches via `curl_cffi` to bypass Cloudflare. Memory says ~1,060 properties.
- Add `_try_rentcafe_wp_probe()` + `_find_rentcafe_property_id()` to `rentcafe.py` (branch lines 427–447). Direct GET `/wp-json/middleware/v1/getFloorplans/?propertyId[]=<id>` for WP-mounted brand sites.
- Wire new helpers as fallbacks **after** main's primary RentCafe capture path runs and returns plan-only.

**Skip (CRITICAL):**
- Branch's removal of `_is_rentcafe_candidate` host-gating — keep main's `_RC_FAMILY_HOST_TOKENS` check
- Branch's removal of `_UNAVAILABLE_TEXT_RE`, `_AVAILABLE_TEXT_RE`, `_detect_availability_status()` — keep main's 3-state status inference

**Tests:** SecureCafe drill + WP middleware + Nestin per-plan + hosted-table fixtures.

### Commit 10 — RealPage OLL workflow parser
**Files:** [realpage_oll.py](../pms/adapters/realpage_oll.py)

Largest single net-new capability in the PR (~187 properties). Main has zero OLL support.

Server-side curl is bot-walled (DataDome/Akamai); browser interception is the only viable path.

- `_is_oll_workflow_response()` URL/body detector for `leasing.realpage.com/RP.Leasing.AppService.WebHost/appstate/v1/?BpmId=OLL.SearchFloorPlan...` PUT responses (branch lines 339–357)
- `parse_realpage_oll_workflow()` (branch lines 140–243): walks `Workflow.ActivityGroups[].GroupActivities[]`, filters by `__type` contains `ApartmentSelectionLeaseMgmtActivity`, extracts `Units[]`
- `dotnet_date_to_iso()` + `_DOTNET_DATE_RE` (branch lines 68–108): `/Date(1779339600000-0500)/` → ISO `YYYY-MM-DD`. Accepts bare epoch-ms fallback.
- `_to_int()` coercion helpers (branch lines 110–130)
- Browser-interception walk (branch lines 408–443) walking `ctx._api_responses`
- Fallback to floorplan summaries when `Units[]` empty/null (branch lines 192–218) — surfaces rent/sqft context for waitlist-only / fully leased plans
- `TIER_1_API_REALPAGE_OLL` tier label
- URL fingerprint detection: `"leasing.realpage.com"`, `"rp.leasing.appservice"`, `"/appstate/v1"`, `"/content/apply#k="`

**Field mapping:**
- Floorplan: `Floorplan.{Name, Bedrooms, Bathrooms, MinSquareFeet, MinPriceRange, MaxPriceRange, AvailableUnits}`
- Unit: `Units[].{UnitNumber, Id, MinPriceRange, MaxPriceRange, Squarefeet, AvailableDate, Deposit}`

**Detector wiring:** Commit 3 already added the Category D yield; verify routing.

**Tests:** golden-response fixtures for OLL workflow + .NET date conversion edge cases.

### Commit 11 — New Tier-1 adapters: server-side-only (batch A)
**New files:** [cortland.py](../pms/adapters/cortland.py) (236), [equity.py](../pms/adapters/equity.py) (240), [rentmanager.py](../pms/adapters/rentmanager.py) (254), [_probe.py](../pms/adapters/_probe.py) (230), [_iloveleasing_table.py](../pms/adapters/_iloveleasing_table.py) (113)

- Copy 5 files from branch (`git checkout origin/fix/resolver-path-patterns-may13 -- <file>`)
- Add imports + class registration in [__init__.py](../pms/adapters/__init__.py) `_bootstrap_registry` tuple
- Add host-fingerprint regexes in [detector.py](../pms/detector.py)

Coverage:
- **Cortland** — server-rendered `preload = {floorplans: {...}}` JSON in HTML, brace-matched parse, unit-level `availprice[unitId]` map with real numbers + epoch date
- **Equity** — `<ea5-unit>` HTML blocks, curl_cffi, no Playwright (~31 Equity Residential properties)
- **RentManager** — `<eid>.ua.rentmanager.com/Search_Result` endpoint with document.write backtick JSON, no auth, no bot wall

Verified: none of these reference the AdapterContext fields the branch removed.

**Tests:** one fixture-based test per adapter.

### Commit 12 — New Tier-1 adapters: browser-intercept (batch B)
**New files:** [g5.py](../pms/adapters/g5.py) (487), [knock.py](../pms/adapters/knock.py) (458), [irvine.py](../pms/adapters/irvine.py) (327), [apts247.py](../pms/adapters/apts247.py) (272)

- Copy 4 files from branch
- Add to `__init__.py`
- Add host fingerprints in detector

Coverage:
- **G5** — public GraphQL `/graphql` with locationUrn discovery (Morgan Properties / Aimco / Bell / JMG / BH — very large portfolio)
- **Knock** — POST `/v1/property/community/<id>` + GET `/units`, no auth. SSR-hardened init signature (2026-05-20). 26 of 38 recovered in memory's failure study.
- **Irvine** — POST `/units/rank {communityId}` with `communityIdAEM` GUID discovery, unit-level lease-term price ladder
- **apts247** — same-origin REST `/api/v1/floorplans/?api_key=<40hex>`, key embedded in HTML

**Tests:** fixture-based per adapter.

### Commit 13 — New Tier-1 adapters: hybrid REIT (batch C)
**New files:** [essex.py](../pms/adapters/essex.py) (396), [maac.py](../pms/adapters/maac.py) (332), [rentvision.py](../pms/adapters/rentvision.py) (230)

- Copy 3 files from branch
- Add to `__init__.py`
- Add host fingerprints in detector

Coverage:
- **Essex** — bulk `/availability?format=spa` passes curl_cffi (~250 communities); unit-level beds/baths/sqft/specials
- **MAA** — MAA Communities REIT; flagged in memory
- **RentVision** — long-tail Tier-1 cluster

**Defer to a second PR:** `aspensquare.py`, `spherexx.py`, `repli360.py`, `residentservices365.py`, `resman.py`, `encoreskyline_template.py`.

**Tests:** fixture-based per adapter.

### Commit 14 — Test consolidation + documentation
- Confirm `pytest ma_poc/tests/ -v` fully green
- Update [CLAUDE.md](../../CLAUDE.md) adapter list section if it enumerates supported PMSes
- Add memory entry for any port that revealed non-obvious behaviour (per memory system instructions)

---

## 5. Canary plan

Canary is **the merge gate**, not a post-merge sanity check. Two tiers: quick (50-property, during dev) and full (500-property, before merge).

### 5.1 Cadence

| Stage | When | Size | Purpose | Time budget |
|---|---|---|---|---|
| Quick canary | After every commit-group (after Commits 2–4, 5–7, 8–10, 11–13) | 50 properties (stratified) | Early regression detection | 15 min |
| Full canary | After Commit 14, before requesting review | 500 properties | Merge gate | 2–4 hr |

Quick canaries catch regressions when only 2–3 commits could be the cause.

### 5.2 Stratified sample composition

The canary CSVs must intentionally cover every change surface. Random selection under-tests the long-tail PMSes that are the bulk of this PR's value.

**500-property full canary mix:**

| Bucket | Count | Sourced from | Exercises |
|---|---|---|---|
| Known-SUCCESS regression watch | 150 | Last 3 SUCCESS runs on main | Must stay SUCCESS post-port |
| RentCafe (vanity, hosted, Nestin, SecureCafe) | 80 | Memory `project_run_2026_05_20`, ALL_fail bucket | Commit 9 |
| Entrata (XHR, WP, ProspectPortal) | 60 | Cloud logs for `TIER_1_API_ENTRATA` and `_NO_RESPONSE` | Commit 5 |
| RealPage OLL (Category D) | 40 | Memory `project_run_2026_05_12_failed_no_data_rca` | Commit 10 |
| SightMap (SHAPE_REJECTED + AMENITIES_ONLY) | 30 | Memory SHAPE_REJECTED bucket | Commit 7 |
| AppFolio (vanity + Wix/Squarespace shells) | 25 | Memory `project_run_2026_05_20` AppFolio gaps | Commit 8 |
| G5 cloud | 25 | Memory `project_run_2026_05_19` G5 cloud unsupported | Commit 12 |
| OneSite (mix of SUCCESS, suspected `_EMPTY`) | 20 | Cloud logs | Commit 6 retry plumbing |
| Knock (26-of-38 bucket) | 20 | Memory `project_failed_no_data_rca_2026_05_12` | Commit 12 |
| Equity / Cortland / MAA / Irvine / Essex / RentVision | 30 | Catalog properties | Commits 11–13 |
| RentManager / apts247 / iLoveLeasing | 10 | Long-tail | Commits 11–12 |
| `_NO_LEASING_PATH` (resolver coverage) | 10 | Cloud logs failing pre-resolver-port | Commit 2 |

**50-property quick canary:** proportional subsample of the above (15 known-SUCCESS, 6 RentCafe, 5 Entrata, 4 RealPage OLL, 3 SightMap, 3 AppFolio, etc.).

**Both CSVs are created once at the start of the PR work** and committed under `ma_poc/tests/canary/canary_500.csv` and `canary_50.csv`. They are repeatable inputs — never random.

### 5.3 Baseline capture (before any code changes)

```bash
git checkout main && git pull

# Baseline both canary CSVs from current main
python ma_poc/scripts/runners/jugnu.py --csv ma_poc/tests/canary/canary_500.csv \
  --output data/runs/baseline_500/
python ma_poc/scripts/runners/jugnu.py --csv ma_poc/tests/canary/canary_50.csv \
  --output data/runs/baseline_50/

# Persist baseline metrics
python ma_poc/scripts/diagnostics/analyze_cloud_run.py \
  --run-dir data/runs/baseline_500/ \
  > /tmp/baseline_500_metrics.json
python ma_poc/scripts/diagnostics/analyze_cloud_run.py \
  --run-dir data/runs/baseline_50/ \
  > /tmp/baseline_50_metrics.json
```

Commit the CSVs to the PR branch. Do not commit the baseline metrics (they're per-run; capture them fresh at PR time).

### 5.4 Metrics tracked per run

Required in every canary diff (baseline vs. candidate):

| Metric | Why |
|---|---|
| Total unit yield (count) | Primary success signal |
| Per-tier distribution | Tier 1+2 share must rise |
| Per-PMS unit yield (count + share) | Confirms specific adapter wins |
| Per-PMS field completion (% non-null): `rent_low`, `available_date`, `sqft`, `beds`, `baths` | Detects silent regressions |
| Entrata `availability_date` capture rate | Memory says fleet was 0%; must rise materially after Commit 5 |
| RealPage OLL tier label appearance | Should appear post-Commit-10 |
| OneSite three-label distribution | New labels must replace single label post-Commit-6 |
| Properties newly SUCCESS (PID list) | Win column |
| Properties newly FAILED or unit count dropped >20% (PID list) | Blocker |
| Properties where unit count dropped 1–20% | Investigate; don't auto-block |
| Memory-flagged buckets: did each show recovery? | Per-bucket sub-table |

**Tooling:** existing [analyze_cloud_run.py](../scripts/diagnostics/analyze_cloud_run.py) covers most. If per-PMS field completion isn't already in its output, add a small `scripts/diagnostics/canary_diff.py` that joins baseline + candidate JSONs and emits the diff. Keep that script in the PR.

### 5.5 Pass / fail thresholds — block merge if any FAIL

| Threshold | Floor | Action if breached |
|---|---|---|
| Total unit yield delta | ≥ +5% vs. baseline | FAIL — investigate regressing adapter |
| Tier 1+2 share | ≥ baseline (no ground lost) | FAIL |
| SUCCESS→FAILED regressions | ≤ 0.5% of known-SUCCESS bucket (≤1 of 150) | FAIL |
| Per-PMS unit yield (any existing PMS) | ≥ baseline – 5% | FAIL on any single PMS regression |
| Entrata `availability_date` non-null rate | ≥ 60% (was ~0%) | FAIL if < 30% |
| RealPage OLL tier label coverage | ≥ 30 of 40 in bucket | WARN if < 20; FAIL if zero |
| New-adapter tier labels appearing | At least 1 SUCCESS per new adapter in batches B+C | WARN if any zero |
| Memory-flagged buckets — recovery | Each bucket ≥ 20% improvement | WARN; document if flat |
| `ruff check` + `mypy --strict` on touched files | Zero errors | FAIL |
| Full `pytest` | Green, coverage ≥ baseline | FAIL |

### 5.6 Workflow within the PR

```
┌─ Commit 2-4   ── quick canary (50)  ── diff vs baseline_50  ─┐
│                                                               │
├─ Commit 5-7   ── quick canary (50)  ── diff vs baseline_50  ─┤
│                                                               ├── Iterate
├─ Commit 8-10  ── quick canary (50)  ── diff vs baseline_50  ─┤
│                                                               │
├─ Commit 11-13 ── quick canary (50)  ── diff vs baseline_50  ─┘
│
├─ Commit 14 ───── full canary (500)  ── diff vs baseline_500
│                       ↓
│                  ALL thresholds pass?
│                       ↓
└──── Request review + merge
```

### 5.7 Canary failure protocol

When a quick canary fails:
1. **Read the regression column first** — which specific PIDs newly failed?
2. **Inspect those PIDs' diffs**: tier label change, field-completion drop, or hard failure?
3. **Map back to commit(s)** in the last group — canary scope is narrow by design.
4. **Patch on the PR branch** (add a fix commit; do not amend prior commits — keep history honest).
5. **Re-run the same quick canary**. Don't proceed to next commit group until green.

When the full canary fails: same protocol, broader surface. May reveal regressions the 50-property mix missed.

---

## 6. Pre-merge validation gate

Run before requesting review, not after. All must pass.

```bash
# Tests
pytest . -v --ignore=data --ignore=config --tb=short --cov=ma_poc

# Static analysis
ruff check ma_poc/pms/
mypy ma_poc/pms/ --strict

# Real-property smoke (≥1 per change group, recorded in PR body)
python ma_poc/scripts/entrata.py --url <entrata-probe-target>
python ma_poc/scripts/entrata.py --url <rentcafe-securecafe-target>
python ma_poc/scripts/entrata.py --url <realpage-oll-target>
# ... per change group; document URL + before-units + after-units in PR description

# Full 500-property canary
python ma_poc/scripts/runners/jugnu.py --csv ma_poc/tests/canary/canary_500.csv \
  --output data/runs/candidate_500/
python ma_poc/scripts/diagnostics/canary_diff.py \
  --baseline /tmp/baseline_500_metrics.json \
  --candidate data/runs/candidate_500/ \
  > /tmp/canary_diff.json
```

Block merge until every threshold in §5.5 passes. Fixes go in new commits — never amend prior commits.

---

## 7. Timeline

| Stage | Effort |
|---|---|
| Commit 1 (baseline + canary CSV preparation) | 3 hr |
| Commit 2 (resolver) | 3 hr |
| Commit 3 (detector) | 5 hr |
| Commit 4 (parsing fixes) | 2 hr |
| Commits 2–4 quick canary | 15 min |
| Commit 5 (Entrata) | 4 hr |
| Commit 6 (OneSite) | 1 hr |
| Commit 7 (SightMap) | 3 hr |
| Commits 5–7 quick canary | 15 min |
| Commit 8 (AppFolio) | 3 hr |
| Commit 9 (RentCafe) | 8 hr |
| Commit 10 (RealPage OLL) | 6 hr |
| Commits 8–10 quick canary | 15 min |
| Commit 11 (server-only adapters) | 4 hr |
| Commit 12 (browser-intercept adapters) | 6 hr |
| Commit 13 (REIT adapters) | 4 hr |
| Commits 11–13 quick canary | 15 min |
| Commit 14 + full 500-property canary + diff | 1 day |
| Review + merge (assuming canary green) | 0.5 day |
| **Total** | **~7.5 working days** |

The full canary stage is the single largest validation expense. Budget it explicitly. Plan a deliberate window where you can hold the PR open through the canary cycle.

---

## 8. Rollback strategy

- **Full revert**: `git revert -m 1 <merge-sha>` on the merge commit.
- **Selective revert** is non-trivial in a single PR. For any partial rollback, plan to revert the whole thing and re-port the safe subset.
- **Do NOT delete the `port/may13-api-tier-coverage` branch** for at least 2 weeks after merge — keep it for partial-revert reference.

---

## 9. PR description template

```markdown
## Summary
Ports targeted scraping-accuracy improvements from `fix/resolver-path-patterns-may13`.
Net effect: +X% unit yield across the 500-property canary; Tier 1+2 share Y%→Z%;
fleet-wide Entrata move-in-date capture now N% (was ~0%); ~187 RealPage OLL
properties newly extracting at Tier 1.

See [ma_poc/docs/MAY13_API_TIER_PORT_PLAN.md](ma_poc/docs/MAY13_API_TIER_PORT_PLAN.md)
for the full plan, including the two prior gap analyses that informed scope.

## Canary diff (500 stratified properties)

| Metric | Baseline | Candidate | Delta | Pass/Fail |
|---|---|---|---|---|
| Total unit yield | … | … | … | ✅ |
| Tier 1+2 share | … | … | … | ✅ |
| SUCCESS→FAILED regressions | 0 | 0 | 0 | ✅ |
| Entrata availability_date non-null | … | … | … | ✅ |
| RealPage OLL tier label coverage | 0 | … | … | ✅ |
| (see canary_diff.json for full table) | | | | |

## Wins by adapter (PIDs newly SUCCESS)
- **RentCafe Nestin** (N PIDs): …
- **RealPage OLL** (N PIDs): …
- **AppFolio embed** (N PIDs): …
- **G5** (N PIDs): …
- …

## Regressions
None / [list with remediation commit].

## Memory-flagged bucket recovery
| Bucket | Pre | Post | Recovery |
|---|---|---|---|
| G5 cloud unsupported | 0 | … | … |
| RealPage api.ws | 0 | … | … |
| Knock 26-of-38 | … | … | … |
| RentCafe vanity FP-container | … | … | … |

## Did NOT port (with reasons — verbatim from MAY13_API_TIER_PORT_PLAN.md §2)
- `base.py` — branch removes 4 fields main uses
- `_api_parser.py` wholesale — branch removes FK-pair detector
- … (full table)

## Test plan
- [x] `pytest . -v` green
- [x] `ruff check ma_poc/pms/` clean
- [x] `mypy ma_poc/pms/ --strict` clean
- [x] Quick canary green at each commit-group checkpoint
- [x] Full 500-property canary green (see table above)
- [x] Real-property smoke per change group:
  - Entrata probe target: PID … N units pre → M units post
  - RentCafe SecureCafe: PID … N units pre → M units post
  - RealPage OLL: PID … N units pre → M units post
  - SightMap SHAPE_REJECTED: PID … N units pre → M units post
  - AppFolio Wix shell: PID … N units pre → M units post
  - G5 cloud: PID … N units pre → M units post
  - Knock: PID … N units pre → M units post
```

---

## Appendix A — Source-branch commit references

Commits referenced in this plan are from `origin/fix/resolver-path-patterns-may13`:

| Short SHA | Subject | Used by |
|---|---|---|
| `d99da26` | Detector: gate G5 branches on absence of competing PMS markers | Commit 3 |
| `21c5607` | Detector: gate Jonah/MeetElise branch on absence of competing PMS markers | Commit 3 |
| `0956c59` | Cluster #3 (G5): rebase gate on live evidence; relax knock regex | Commit 3 |
| `39bba7b` | Detector: Engrain widget signal routes RealPage+SightMap to sightmap | Commit 3 |
| `d787a8f` | Port main 78516c3 SightMap-embed routing into detector | Commit 3 |
| `337ebaa` | Cluster #4: bootstrap profile BEFORE L1 fetch so escalator engages | Commit 5 |
| `1790362` | RentCafe-SecureCafe portal regex: match any onlineleasing entry | Commit 9 |
| `74b94a4` | RentCafe-Nestin per-plan DOM recovery: unit-level from `/floorplans/{slug}` | Commit 9 |
| `de8632e` | RentCafe-Nestin: accept absolute hrefs from Playwright-rendered HTML | Commit 9 |
| `9355711` | RentCafe-Nestin: clear stale homepage CF cookies before detail probes | Commit 9 |
| `23ad093` | AppFolio-embed tenant-only fallback (Wix shells with auth-URL hints) | Commit 8 |
| `3d2aea8` | AppFolio embed: canonicalize captured URL to /listings root | Commit 8 |
| `727b31c` | Cluster #5 (SightMap SHAPE_REJECTED): broaden embed-code discovery beyond `<iframe>` | Commit 7 |

---

## Appendix B — Memory entries that informed scope

- `project_run_2026_05_11_extraction_bug_taxonomy.md` — deposit→rent leakage, cross-page phantom inflation
- `project_run_2026_05_12_failed_no_data_rca_2026_05_12.md` — 5 root causes for 1877/4983 failures
- `project_run_2026_05_19_unit_fidelity_gaps.md` — RealPage api.ws, equity tile, G5 cloud, WordPress admin-ajax
- `project_run_2026_05_20_appfolio_securecafe_gaps.md` — AppFolio `_ADDRESS_RE`, RentCafe vanity FP-container
- `feedback_smoke_test_real_artifacts.md` — never claim done without real-data verification
- `feedback_extraction_root_cause_depth.md` — root-cause at regex/gate, not sentinel storage
- `feedback_diagnostic_playbook.md` — always pull fresh cloud logs before analysis
