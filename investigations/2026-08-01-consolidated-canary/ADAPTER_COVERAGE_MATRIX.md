# Adapter-by-adapter output coverage matrix

Date: 2026-08-01 (America/Chicago)
Registry commit: `fa1afb72649853fe7d95c6e4916f40a74326afdd`
Canary: `gs://jugnu-canary/runs/2026-08-01-consolidated-strict-fa1afb7/`
Scope: all 47 registered adapters, plus every non-registry surface that owned
final output in the completed 4,982-property canary

## What this matrix proves

This is an exhaustive **coverage accounting**, not an assertion that every
adapter is semantically clean.

- Every one of the 47 registry entries is represented below.
- All 4,982 canary properties, 85,692 unit rows, and 6,768 plan rows were
  attributed and structurally scanned.
- Canary attribution uses `_meta.provenance.adapter`. An attributed adapter can
  still finish through a shared fallback tier, so property/row counts are not
  automatically direct-adapter winner counts.
- Duplicate-ID counts are extra occurrences of a non-empty final `unit_id`
  within a property. Zero duplicates does not prove clean identity because a
  lossy deduplicator can make a collision disappear by deleting a real row.
- “Negative/capture” means a unit row explicitly marked unavailable, leased,
  pending, waitlist, or not available while carrying
  `capture_date_default`.
- “Test modules” counts repository test modules that reference the adapter,
  not test cases and not current-source certification.

## Evidence levels

| Code | Meaning |
|---|---|
| `D3` | Confirmed defect with at least three current/retained source-to-final reproductions, or a complete current adapter cohort |
| `D2` | Confirmed defect with one or two exact current source-to-final reproductions; scope remains narrow |
| `C3` | Current scoped route cleared with at least three source-to-final controls; untested fallback shapes are not certified |
| `M3` | Mixed: one scoped route cleared, while another attributed fallback is confirmed defective |
| `O1` | Canary output and structural invariants scanned; no complete semantic source certification |
| `N0` | No unit output to audit in this canary; no quality claim |

## Registered adapters

| # | Adapter | Attributed properties | Unit / plan rows | Duplicate-ID extras | Negative/capture units | Related test modules | Evidence | Disposition |
|---:|---|---:|---:|---:|---:|---:|---|---|
| 1 | `rentcafe` | 1,842 | 29,353 / 1,393 | 11 | 0 | 27 | `D3` | Findings 1 and 13; finding 18 clears only the sampled future-date hypothesis; finding 19 proves Lake Haven's repeated-surface merge defect. |
| 2 | `resman` | 78 | 8,348 / 447 | 121 | 6,196 | 2 | `D3` | Finding 15 confirms the shared date defect; finding 22 proves the full-roster/available-subset identity merge on three properties. |
| 3 | `apts247` | 144 | 1,647 / 140 | 0 | 0 | 4 | `D3` | Finding 24 preserves 204/204 sampled rows and values but proves native PMS-ID loss on 143 numbered apartments across four current sources. |
| 4 | `entrata` | 429 | 5,657 / 633 | 26 | 0 | 29 | `D3` | Finding 3 confirms building/native-ID loss. |
| 5 | `appfolio` | 250 | 2,106 / 72 | 2 | 255 | 20 | `D2` | Finding 6 proves the Wisconsin-address identity defect on Jade; finding 15 accounts for the 255 negative/capture rows through the shared formatter. |
| 6 | `onesite` | 169 | 2,794 / 92 | 0 | 0 | 6 | `D3` | Finding 12 confirms three-property Mark-Taylor cross-property contamination. |
| 7 | `onsite_apply` | 49 | 433 / 84 | 0 | 0 | 1 | `D3` | Finding 32 implemented/live-verified. Original audit: 28 links/284 rows. Complete 2026-08-02 recheck: 47 links, 39 active bound rosters, 367 physical rows after one proven roommate-application sentinel exclusion; all IDs/baths/rents/dates and 354 provable plans survive, with 13 unproven plans blank. Eight linked pages remain bound-empty/unbound/HTTP failures and two are current no-link controls. |
| 8 | `sightmap` | 221 | 6,437 / 1,217 | 0 | 0 | 14 | `C3` | Finding 23 clears the direct/iframe API roster shape on three current source-to-final controls (521/521 physical units); fallback shapes remain uncertified. |
| 9 | `realpage_oll` | 112 | 1,704 / 243 | 0 | 0 | 5 | `C3` | Finding 26 clears three numeric OnlineLeasing and three CWS GetUnits source-to-final controls (365/365 rows, 78/78 future dates); interception-only OLL and plan fallbacks remain unexercised. |
| 10 | `repli360` | 59 | 1,068 / 29 | 0 | 0 | 1 | `D3` | Finding 27 proves 46 canary WAIT sentinels were emitted as available physical units and proves native-ID loss on 94/94 rows across three current controls. |
| 11 | `rentmanager` | 39 | 354 / 137 | 1 | 0 | 6 | `D2` | Finding 20 proves native-ID/address loss and one deleted Rose Park apartment; scope is one current property. |
| 12 | `rentvision` | 38 | 644 / 0 | 1 | 0 | 3 | `D2` | Finding 21 proves building/native-ID loss on Birch Pond; scope is one current property. |
| 13 | `rentaladdress` | 1 | 0 / 2 | 0 | 0 | 1 | `D2` | Finding 37 is fixed and live-replayed locally on the complete one-property cohort: both current plans and values remain exact with `UNKNOWN`, null date, and `missing` provenance; publish-ceiling now reports `CONFIRMED_PLAN_ONLY` with two plans despite their explained rent tokens, and provenance reports two plan rows / zero physical units. Focused GCP canary remains pending. |
| 14 | `residentservices365` | 10 | 108 / 33 | 0 | 0 | 1 | `D3` | Finding 36 is fixed and independently replayed locally: 108/108 physical rows/native GUIDs, plan names, and terms plus 34/34 floors are exact; all 42 current visible-now states and 66 future dates survive with correct provenance; all 29 Telfair Best Value tuples match; and the adapter emits the current 72-plan catalogue one-for-one (The Vue 10, Westshore 4, Rustic Woods 4). Focused GCP canary remains pending. |
| 15 | `aspensquare` | 8 | 87 / 2 | 0 | 0 | 1 | `D3` | Finding 38 is implemented and independently live-replayed locally on the complete eight-property cohort. The current source has 29 exact plans, 64 displayed apartments, and 87 eligible Knock UUIDs; reconciliation admits 86 unique UUIDs with 86/86 public labels/buildings/human plans and withholds Edgewood's one stale row behind an exact empty roster. All 27 displayed current rows emit `available_now`, 52 admitted future dates remain exact, and 23 Knock rows outside Aspen's capped window remain explicitly flagged. Four non-Aspen live controls (77 rows) preserve public labels/buildings and make only the proven `available=true + occupied` future rows available. Focused GCP canary remains pending. |
| 16 | `essex` | 27 | 212 / 0 | 0 | 0 | 1 | `D3` | Finding 34 implemented/live-verified. The complete 27-property adapter-to-final replay remains 340/340 rows with 234/234 future dates and zero mapped-field mismatches; all 340 native unit/floor-plan/property identities and 27 exact response-provenance records now survive. Eight retained 404 shells, Belcarra's non-200/JSON/shape/empty outcomes, and sibling-response rejection are fixture-tested; focused GCP canary remains the release gate. |
| 17 | `avalonbay` | 28 | 716 / 6 | 4 | 0 | 2 | `D3` | Finding 2 confirms native `unitId` loss and Arlington Square collisions. |
| 18 | `amli` | 11 | 522 / 0 | 0 | 0 | 1 | `D3` | Finding 31 proves 267 current sibling rows admitted across 8/11 properties; on the correct 254-row target cohort it also proves complete bed/building/native-ID loss and 23 unit-area substitutions. Base rent and 198/198 target future dates are clean. |
| 19 | `maac` | 31 | 903 / 0 | 0 | 0 | 1 | `D3` | Finding 28 clears row/value/date fidelity on both current source shapes (327/327 rows and 286/286 future dates) but proves loss of every native unit and property-identity field. |
| 20 | `irvine` | 13 | 599 / 0 | 0 | 0 | 0 | `D3` | Finding 30 audits the complete current cohort: 599/599 values and 395/395 future dates survive, but 80 native `unitID` collisions are hidden by formatter rescue while all unique property/object IDs are discarded. Add a dedicated regression module. |
| 21 | `cortland` | 22 | 617 / 0 | 4 | 0 | 1 | `D3` | Findings 8 and 9 confirm identity loss and fee-inclusive rent substitution. |
| 22 | `reinhold` | 0 | 0 / 0 | 0 | 0 | 1 | `N0` | No canary attribution or output. |
| 23 | `edificecms` | 5 | 89 / 71 | 0 | 0 | 1 | `D3` | Finding 39 is implemented and independently live-replayed locally over all 70 current plans, 89 physical apartments, and 42 exact empty plans. All 30 future on-notice rows now remain `AVAILABLE` with their exact dates; all 89 native IDs/values and all 42 empty plans remain exact. Newport's five-plan aggregate deterministically outranks its two-plan subset, Turtle Dove II is identity-rejected, and exact Edifice plan authority suppresses Turtle Dove's 29 generic/sibling candidates. Focused GCP canary remains pending. |
| 24 | `equity` | 26 | 338 / 0 | 0 | 0 | 2 | `D3` | Finding 33 implemented/live-verified. Original direct audit: 15 controls/181 rows. Current complete recheck: 25 unit-producing controls/344 rows (24 direct + one bounded compliant Hyperbrowser), all identity/value/date contracts exact; nine bare-unit collision extras become source-backed composites. The Terraces remains redirect/no-response. |
| 25 | `funnel` | 88 | 1,864 / 99 | 0 | 0 | 6 | `D3` | Finding 25 proves native unit/plan/property-ID loss on all three direct Spaces winners (55 current rows); other attributed fallback families retain their own dispositions. |
| 26 | `fortresstech` | 10 | 170 / 0 | 0 | 0 | 1 | `D3` | Finding 35 implemented/live-verified. Complete adapter-to-final replay is 170/170 with zero mapped-field mismatches; Vivo preserves all nine typed 282-sq-ft areas, all 170 rows retain native + org/property UUID binding, and all ten exact response-provenance records survive. Current source has 151/151 exact future dates (retained strict capture: 91). The 441-test focused suite is green; focused GCP canary remains the release gate. |
| 27 | `touchtour` | 0 | 0 / 0 | 0 | 0 | 1 | `N0` | No canary attribution or output. |
| 28 | `spherexx` | 22 | 363 / 11 | 49 | 0 | 3 | `M3` | Finding 14 clears the dedicated Spherexx route on three feeds. Most attributed collision/dimension symptoms are G5 fallback rows covered by findings 10 and 17; the combined attribution is not clean-certified. |
| 29 | `knock` | 439 | 10,423 / 8 | 0 | 0 | 9 | `C3` | Finding 5 clears current UUID identity across four live properties; this is scoped to the tested Knock roster shape. |
| 30 | `g5` | 76 | 935 / 6 | 95 | 0 | 4 | `D3` | Findings 10 and 17 confirm apartment-identity loss and the Woodbury dimension contradiction. |
| 31 | `encoreskyline_template` | 63 | 1,330 / 97 | 0 | 0 | 3 | `M3` | Finding 29 clears 306/306 current Jonah resource rows but proves native/property-ID loss on 101/101 Jonah SSR rows. SightMap is scoped-clean; RentCafe/Funnel inherit their own defects; BetterNOI remains separately uncertified. |
| 32 | `marketapts` | 29 | 188 / 59 | 0 | 0 | 1 | `D3` | Finding 40 is implemented and locally replayed. The complete baseline remains 188 exact apartments, 99 future dates, and 53 authoritative empty-plan rows. Retained source-to-final replay removes all six lower-authority generic rows while preserving Ellis's 7 physical + 4 exact empty-plan rows and Riverbank's 4 physical rows; fresh first-party source confirms labeled deposits no longer become rent. The 136-test focused suite is green; focused GCP canary remains pending. |
| 33 | `mri_prospectconnect` | 9 | 92 / 0 | 0 | 0 | 1 | `D3` | Finding 41 is implemented and live-replayed across all eight original direct properties plus Bridgepoint. The original 91 rows remain exact; Elmtree's nine published highs now survive final formatting while 82 single-value rows remain equal-bounded. The full identity gate narrowly admits the `Bridgepoint I`/`Bridgepoint` suffix case and recovers exact MRI unit `8:807`, public/building/plan/term/date context, and its current $995–$1,240 range. The 32-test focused suite is green; focused GCP canary remains pending. |
| 34 | `rentcafe_unit_roster` | 0 | 0 / 0 | 0 | 0 | 1 | `N0` | No canary attribution or output. |
| 35 | `imt_spaces` | 1 | 0 / 0 | 0 | 0 | 1 | `N0` | One attempted property, `FAILED_NO_DATA`; no output to audit. |
| 36 | `realpage_cws` | 36 | 650 / 27 | 0 | 0 | 3 | `C3` | Finding 26 clears the CWS GetUnits roster on three current first-party controls (181/181 rows, 44/44 future dates); plan fallback remains unexercised. |
| 37 | `rentcafe_layout_tab` | 12 | 132 / 31 | 0 | 0 | 4 | `D3` | Finding 42 audits and locally remediates the complete attributed cohort. The prior `/availableunits` shortcut returned 89 of 187 exact apartments, omitted 51 future-dated rows, introduced 13 plan conflicts, lost 65 baths, degraded 31 exact plans, and duplicated Broadway 516. Shared source-priority reconciliation now returns 187/187 native apartments, 187 baths, all 96 explicit-future dates, zero duplicates, all 13 exact semantics, and source hashes/counts for all admitted rows; 409 broad tests and 129 focused tests are green. Focused GCP canary pending. |
| 38 | `wix_floor_plans` | 3 | 18 / 0 | 0 | 0 | 1 | `D3` | Finding 43 is implemented and live-replayed locally over the complete three-property cohort. Westerville now emits its exact three inquiry-only plans with no invented area/date. Bellagio reconciles its current conflicting `PRICING`/`FLOOR PLANS` ranges to six categories, binds the 25-code catalogue and exact labeled 3DPlans map, and preserves native unit `989` / `509`, plan `X09`, 1/1, 892 sqft, $2,089, and `2026-08-14` with source provenance. Vestawood's Wix route stays empty; the AppFolio control remains exactly 18 target / 17 rejected sibling rows. The 48-test Wix/AppFolio suite and 50-test source-id registry are green; focused GCP canary pending. |
| 39 | `equity_apartments` | 0 | 0 / 0 | 0 | 0 | 1 | `N0` | No canary attribution/output, but semantic audit found the same `/UnitFees/{property}/{building}/{unit}` identity contract as finding 33; the dormant DOM path now emits the shared building-unit canonical ID and full property/building/unit provenance, covered by focused tests. |
| 40 | `generic_plan_text` | 101 | 726 / 477 | 0 | 0 | 10 | `D3` | Finding 16 covers the complete 20-property UDR cohort and confirms 454 lost dates; finding 4 clears current UDR identity. Other generic-plan-text shapes remain output-only. |
| 41 | `venterra` | 3 | 74 / 0 | 0 | 0 | 1 | `D3` | Finding 7 confirms native-code loss across four current probes, one affected; zero canary duplicates does not clear row loss. |
| 42 | `camden` | 16 | 130 / 0 | 0 | 0 | 2 | `D3` | Finding 44 is implemented and completely replayed locally. The prior landing adapter emitted 130 suggestions while 182 exact drills published 531 apartments; the older 396-row cross-product fabricated values. The replacement returns 531/531 unique exact physical rows with all dates/terms, 520 positive floors plus 11 source-zero sentinels, and all 160 qualified labels. Fairview/Fallsgrove/North End live controls return 10/14/63 clean rows. The unsafe generic paths are removed; 71 focused tests pass. Focused GCP canary pending. |
| 43 | `squarespace_nopms` | 6 | 58 / 17 | 0 | 0 | 2 | `D3` | Finding 45 is locally remediated and fully replayed. A bounded, same-host authored-route pass makes physical provider data outrank generic price text; Landmark's strict same-figure parser returns 5/5; 30Sixty returns exact AppFolio apartment `522` / listing `554` and no `$1,940` synthetic plan; Cricket returns 8/8 with no four-plan supplement; and pinned 250 High returns 11 physical + 12 legitimate plans with `TEMP` rejected. Tribeca remains 21/21 and Town Center 17/17. The direct live six-property replay is also clean (250 High currently 12 + 11 due source drift), and 601 focused provider tests pass. Focused GCP canary pending. |
| 44 | `thinkreside` | 2 | 46 / 14 | 0 | 0 | 1 | `D3` | Finding 46 is implemented and fully replayed locally. Indy remains exactly 46 physical rows + 7 legitimate empty plans with all 46 composite source identities, 38 `available_now` rows, and 8 exact future dates. Deer Run is exactly 4 property-bound plans with exact names/slugs/area and rent evidence, all `UNKNOWN` and undated; no generic overlap remains. Ridge at Perry Bend is a clean third live control (5 plans). The 29-test adapter suite and 149-test source-to-final boundary set pass; focused GCP canary pending. |
| 45 | `wix_nopms` | 18 | 67 / 40 | 0 | 0 | 4 | `D3` | Finding 47 is implemented and locally regression-verified. The Marq waitlist morphology is rejected with its native UID; Millennium's exact operator-authored index admits four configured-address apartments and rejects seven siblings; the four Wix-plan shapes preserve 27 authored identities/values as `UNKNOWN` and undated; Indian Village, Westgate, and Allen Ranch recover five source-faithful plans; SightMap `TEMP` is rejected and ambiguous marketing-rent joins are withheld. Three current no-data controls remain empty. The focused GCP canary remains the release gate. |
| 46 | `yotta` | 3 | 58 / 0 | 0 | 0 | 1 | `D3` | Finding 48 is implemented and independently live-replayed locally on all three current DBA routes: 27/18/13 native apartments, 7/5/5 stable provider plan IDs, all 58 word-ordinal floors, 17/17 literal `Today` rows as `available_now`, and 41/41 future dates are exact. Each property carries one hashed, property-MATCH unit-producing response covering every admitted row. Focused GCP canary remains pending. |
| 47 | `generic` | 243 | 3,606 / 1,113 | 1 | 1,636 | 48 | `D3` | Findings 11, 14, and 15 confirm defects in Harbor, generic early dedup, and Razz/ResMan fallback. The adapter contains many other subroutes and is not family-wide clean-certified. |

## Non-registry output owners

These provenance owners also produced final unit rows and must not disappear
from a “registered adapters only” review:

| Output owner | Properties | Unit / plan rows | Evidence | Required disposition |
|---|---:|---:|---|---|
| `betternoi_public` | 1 | 9 / 0 | `D2` | Finding 49 is implemented locally: the physical roster is unchanged and the exact property-published unit response now survives as sanitized hashed provenance with its client/floor-plan/property MATCH evidence. Focused GCP canary remains pending. |
| `nesthub_public` | 1 | 1 / 0 | `D2` | Finding 49 is implemented locally: only Annaberg listing `602` survives the 33-row manager boundary, and its exact detail response now survives as sanitized hashed provenance. Focused GCP canary remains pending. |
| `showmojo_public` | 1 | 13 / 0 | `D2` | Finding 49 is implemented and live source-to-final replayed: all 13 native listings survive; 12 `Available now` rows normalize to the capture date with `available_now`; UID `e7c39f1061` preserves raw `Available September 7th` and emits `2026-09-07` with `explicit_future`; two hashed MATCH roster responses cover all 13 admitted rows. Focused GCP canary remains pending. |
| `static_residence_table` | 1 | 3 / 0 | `D2` | Finding 49 is implemented locally: three physical residences remain unchanged and all five numeric stack records enter the plan channel with exact stack/asset identity, dimensions, and rent range as `UNKNOWN` and undated; the exact mixed-table response is hashed in provenance. Focused GCP canary remains pending. |
| `UNATTRIBUTED` | 186 | 0 / 168 | `N0/O1` | 162 failures plus 24 plan-level outputs; preserve as a separate attribution-quality gate |

## Fleet structural findings by adapter

The unit-only invariant scan found:

- exactly **8,087 negative/capture contradictions**: ResMan 6,196, generic
  1,636, and AppFolio 255. These reconcile exactly to finding 15;
- **315 duplicate-ID extras** across eleven attributed adapters: ResMan 121,
  G5 95, Spherexx-attributed output 49, Entrata 26, RentCafe 11, AvalonBay 4,
  Cortland 4, AppFolio 2, generic 1, RentManager 1, and RentVision 1; and
- no inverted or non-positive rent bounds in the final unit rows under the
  bounded numeric checks used for this matrix.

All structural duplicate families now have an evidence-backed disposition.
Findings 19-22 close the four previous residuals: Lake Haven's 11 extras,
Rose Park's deleted fifth apartment, Birch Pond's building collision, and the
ResMan full-roster/available-subset merge. This closes classification, not the
defects themselves; each still requires a fixed source-to-final replay.

## Coverage closure status

| Classification | Registered adapters | Meaning for a future full canary |
|---|---:|---|
| Semantic evidence (`D3`, `D2`, `C3`, or `M3`) | 42 | Known scoped behavior; confirmed defects must be fixed and replayed |
| Output-only (`O1`) | 0 | Every active output-producing registered adapter now has a scoped semantic disposition |
| No unit output (`N0`) | 5 | Cannot be certified from this run; retain as an explicit coverage gap |
| **Total** | **47** | Complete registry accounting |

The registered-adapter matrix and all four non-registry output owners are now
semantically classified: no active attributed output owner remains at `O1`.
Continue by preserving the 186 unattributed properties as a separate
attribution-quality gate rather than silently treating them as adapter-cleared.


## Full-canary gate

Do not launch the next 4,982-property canary merely because this matrix exists.
Before that run:

1. implement and locally replay the evidence-confirmed fixes in
   `ADAPTER_DATA_QUALITY_FIX_PLAN.md`;
2. resolve every structural duplicate candidate above against current source;
3. implement and replay the non-registry corrections from finding 49; no
   active attributed output owner remains at `O1`;
4. keep `N0` adapters labeled unexercised rather than clean;
5. run the focused cohorts from the fix plan; and
6. promote no warm profile until property identity and output-quality gates
   pass.

No full canary or production/profile write was performed while building this
matrix.
