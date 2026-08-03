# Adapter data-quality fix plan

Date: 2026-08-01 (America/Chicago)
Branch baseline: `codex/consolidated-canary-2026-08-01` at `fa1afb7`
Evidence ledger: `ADAPTER_DATA_QUALITY_FINDINGS.md`
Coverage prerequisite: `ADAPTER_COVERAGE_MATRIX.md`
Status: all planned remediations implemented locally; consolidated regression
and strict focused GCP canary/reconciliation pending; no production writes

## Outcome

Fix the 49 evidence-backed findings without sacrificing the plan-to-unit and
failed-no-data recovery gains already consolidated on this branch. The rollout
must value source correctness over the headline success rate: rejecting a
cross-property roster is a quality improvement even if the affected property
temporarily moves from `SUCCESS` to `FAILED_NO_DATA`.

The completed consolidated canary is the comparison baseline, not proof of
quality:

| Baseline measure | Result |
|---|---:|
| Properties | 4,982 |
| Reported successes | 4,640 (93.14%) |
| Unit rows | 85,692 |
| Explicitly unavailable rows assigned the capture date | 8,087 |
| UDR rows whose current source date is lost | 454 / 600 |

No warm profile or canary output should be promoted until the quality gates in
this plan pass. The next full canary is additionally blocked on the coverage
closure rules in `ADAPTER_COVERAGE_MATRIX.md`; an output-only non-registry
owner must not be described as semantically clean.

## Priority and work packages

### P0-A: Stop cross-property inventory admission

**Evidence:** finding 12, OneSite/Mark-Taylor. Mira Santi, San Cervantes, and
Waterside at Ocotillo each adopt the same 14 San Norterra apartments.

**Implementation:**

1. In `ma_poc/pms/adapters/onesite.py`, bind every discovered Online Leasing
   portal to the configured property before workflow rows can win.
2. Accept a portal only when an authoritative response or visible portal shell
   agrees on property name, address, configured slug, or vendor property ID.
3. Exclude OneSite links found only inside sibling/portfolio collections such
   as `simplifiedProperties`.
4. Fail closed when identity is contradictory. Do not fall back to “only one
   OneSite host exists in the page” as property proof.
5. Record the identity evidence and the actual unit-producing response in
   provenance.

**Regression set:** the three current Mark-Taylor pages, the San Norterra
portal they wrongly select, and at least three correct OneSite workflow
controls. Tests belong beside `test_onesite_workflow.py` and must exercise the
detector through final formatted output.

**Acceptance gate:** zero San Norterra unit IDs on any of the three configured
Chandler properties. A no-data result is acceptable until a property-matching
source is found.

### P0-B: Make negative availability status authoritative

**Evidence:** finding 15. The full canary contains 8,087 rows with an explicit
`UNAVAILABLE` status and `capture_date_default`; 7,825 are Razz/ResMan
full-roster rows across 44 properties.

**Implementation:**

1. Change the shared resolver in `ma_poc/core/schema_v2.py` so every parseable
   explicit source date wins.
2. Apply the capture date for explicit `AVAILABLE`/`Available Now`.
3. Permit the rent-only fallback only when status is absent or genuinely
   unknown and non-negative.
4. Never manufacture a date for `UNAVAILABLE`, `LEASED`, `PENDING`, waitlist,
   or another explicit negative state when the source supplies no date.
5. Keep the canonical formatter and Jugnu formatter on the same helper.

**Regression set:** complete current records from Village of Cross Creek,
Village at Crown Woods, Milano, and Alleia Long Meadow Farms, plus explicit
`AVAILABLE`, `LEASED`, `PENDING`, waitlist, missing-status-with-rent, and
explicit-future-date cases. Exercise both formatter entry points.

**Acceptance gate:** fleet invariant
`negative availability_status + capture_date_default == 0`. Explicit future
dates must remain byte-for-byte unchanged.

### P0-C: Stop manufacturing RentCafe Applicant availability

**Evidence:** finding 1. Three current properties expose 19 rented floor plans
with no physical units that SurgeX marks `AVAILABLE`; Zander Place also loses
a waitlist-only plan and selects a stale April date over the current August
availability window.

**Implementation:**

1. In `ma_poc/pms/adapters/rentcafe.py`, use `AvailableUnits`,
   `IsFullyOccupied`, `FloorPlanAvailable`, and the surviving unit roster when
   determining plan state.
2. Preserve inquiry-only plans as plan-level evidence, but never as available
   apartments.
3. Preserve a waitlist-only plan after pseudo-unit filtering with an honest
   waitlist/unknown state.
4. For a real apartment, prefer the current `UnitAvailableStartDate` or an
   equivalent current window over a contradictory historical
   `AvailableDate`.
5. Feed the explicit state through the corrected shared date resolver from
   P0-B.

**Regression set:** complete real-shaped Zander Place source plus Georgetown
Crossing and Stockbridge Trails controls. Include the five manually visible
Zander plan states and unit 202 on plan B.

**Acceptance gate:** all 19 zero-inventory plans are non-available; Zander's
waitlist plan remains represented; no inquiry-only row receives the capture
date; the real unit's current availability window wins.

### P0-D: Reconcile repeated rosters by immutable apartment identity

**Evidence:** findings 19 and 22. Lake Haven fetched one successful Jonah
roster twice under equivalent URL spellings. Three Razz/ResMan properties
combined a full roster with its overlapping available subset. In both shapes,
rent in the dedupe key turns a normal price difference into a second physical
apartment row.

**Implementation:**

1. After a normalized scheme/`www`/trailing-slash surface succeeds, suppress
   equivalent lower-ranked candidates for that property.
2. Across unavoidable repeated snapshots, merge first by property-scoped
   native unit ID; never use rent, date, or status to define identity.
3. For the Razz/ResMan pair, retain the ResMan available-subset provenance for
   availability/date and selected-term price, and the full roster for
   catalogue/dimension fields.
4. Preserve both source URLs and response hashes on the resulting single row.

**Regression set:** both retained Lake Haven 36-row snapshots, Village of
Cross Creek, Brandon Place, and Centennial Gardens. Include one control where
the second normalized surface is the only successful fetch so HTTP/HTTPS
fallback remains possible after an actual failure.

**Acceptance gate:** Lake Haven emits one row per physical ID; the three
ResMan controls emit exactly their full-roster identity count with no duplicate
canonical IDs; no unavailable catalogue row gains an availability date.

### P0-E: Keep Repli360 waitlist sentinels out of physical availability

**Evidence:** finding 27. The strict canary emits 46 `WAIT...` rows across four
properties as available physical apartments with the capture date. Current
source shows call-for-pricing, `Availability --`, and a void or 1969-sentinel
action instead.

**Implementation:** inspect the complete Repli unit row before assigning
status. When the WAIT label, missing numeric price, unavailable/call-for-price
text, and sentinel action agree, route the row to honest waitlist/catalogue
evidence rather than unit inventory. Never treat request-echo
`data-available_date` as a source availability date for that shape.

**Regression set:** River Oaks, Shore Park at Riverlake, Reserve at Capital
Center, and Hidden Hills, plus priced physical rows from each property.

**Acceptance gate:** zero WAIT sentinels in physical `units[]`; no sentinel
receives `AVAILABLE` or `capture_date_default`; every current priced physical
row survives once.

### P0-F: Bind AMLI floor-plan queries to the configured property

**Evidence:** finding 31. Eight of eleven current AMLI properties accept every
floor-plan array in their submarket. Only 254 of the 521 current emitted rows
belong to the configured properties; 267 are exact named siblings.

**Implementation:** parse the current `props.pageProps` shape, extract the
target AMLI property ID and Prismic document ID from the property page, and
select only an exact `amli/floorplans` query whose two input IDs match. Apply
the same binding to a submarket fallback and fail closed on zero or multiple
contradictory matches. Preserve query/property identity in provenance.

**Regression set:** all eleven current AMLI properties, with explicit
multi-property fixtures for Quadrangle, Aldrich, Midtown 29, and South Shore.

**Acceptance gate:** the current cohort emits exactly the 254 current target
rows (inventory movement reported separately), zero sibling query IDs, and a
property-identity verdict on every accepted response.

### P1-A: Preserve explicit dates already published by the source

#### UDR native-ID date join

**Evidence:** finding 16. All 600 current UDR apartments join losslessly by
native apartment ID; the current display-label lookup drops 454 dates.

**Implementation:** index the UDR view model by `apartmentId` and
`realpageunitid`, resolve using the JSON-LD `unitid` first, and use display
labels only as an unambiguous fallback. Do not change the corrected
building-qualified canonical unit identity.

**Regression set:** all 20 current UDR properties as a replay ledger; fixtures
must include `7-105` versus `19-105`, `TH-4702`, and one no-prefix control.

**Acceptance gate:** 600/600 pinned rows retain the source label/date; current
live denominator is reported separately if inventory changes. Canonical IDs
remain unchanged and unique.

#### Harbor canonical date key

**Evidence:** finding 11. The Riverworks adapter parses 20 explicit future
dates but emits `available_date_post_fix`, which the formatter does not read.

**Implementation:** emit the normalized date under canonical
`available_date`; retain the visible text as provenance. Prefer this narrow
adapter change over adding another permanent shared alias.

**Regression set:** Riverworks end to end, Waterford Village `Available Now`
control, and Triangle Place Knock-winner control.

**Acceptance gate:** all 20 pinned Riverworks future dates survive; Waterford
still maps `Available Now` to capture date; Triangle's current winner is
unchanged.

### P1-B: Preserve physical apartment identity before deduplication

These changes alter canonical IDs and therefore require an explicit migration
ledger. For every changed row, record `property_id`, old display-based ID, new
native/building-qualified ID, and source native ID. Treat the expected ID
transition separately from true new/disappeared inventory.

| Adapter / evidence | Correct identity rule | Required real-shaped regression |
|---|---|---|
| G5, finding 10 | native `id` is canonical; `displayName` is the apartment label; `name` is plan type | Shadowbrook, Hawthorn Village, Brookside Village; 43 source apartments preserved with unique IDs |
| Cortland, finding 8 | preserve modern native apartment ID and building; preserve legacy `availprice` map key | Mirror Lake, Royal Palm Beach, Brier Creek control; no source row loss or duplicate final IDs |
| Entrata unit cards, finding 3 | retain the visible fourth spec/building and native `entrata_uid`; qualify colliding short numbers | Phoenix Orlando, Abberly Grove, Seasons at Mount Pleasant, Wedgewood control |
| AvalonBay, finding 2 | native `unitId` is canonical; `unitName` remains display | Arlington Square plus Meydenbauer and Montville controls; 81/81 Arlington IDs unique |
| AppFolio, finding 6 | accept bounded Wisconsin grid addresses and keep the complete address identity | all 20 Jade at North Hills rows; 20 final rows and 20 unique IDs |
| Venterra, finding 7 | use complete `unit_code`, or a verified building projection, as canonical identity | The Metropolitan plus Forest View, Canton Mill Lofts, and The Parker controls |
| RentManager/iLoveLeasing, finding 20 | preserve native detail ID as canonical and retain modal street address | Rose Park Commons; five source rows become five unique final IDs |
| RentVision, finding 21 | preserve Apply `UnitId` and Building; use building-qualified fallback only without native ID | Birch Pond; six source rows become six unique final IDs |
| Apts247, finding 24 | preserve numeric PMS `id` as canonical on every apartment; keep visible number/building separately | Broadway Palace, Buena Onda White Rock, Ranch at 1856, and The Maxwell; 204 source rows remain 204 with exact value fidelity |
| Funnel Spaces, finding 25 | **Implemented + locally/live verified 2026-08-02:** preserve native unit ID as canonical plus plan/property asset IDs and source community name | Windsor Burnet, Cirrus, and Estates at Cougar Mountain; audit roster 55/55, current roster 54/54 after one Windsor inventory departure; exact value/date fidelity and 103/103 focused tests |
| Repli360, finding 27 | preserve native row/application `UnitID` for physical apartments; retain visible number and floor-plan/site IDs | Marquis at Great Hills, River Oaks, and Marquis Sonoran Preserve; 94 current source rows stay accounted for while WAIT sentinels route under P0-E |
| MAAC, finding 28 | **Implemented + locally/live verified 2026-08-02:** use `rentCafeApartmentId` as the property-scoped canonical unit anchor; retain MAAC item ULID, floor-plan/property IDs, property name, and visible apartment label | Rocky Point, Wade Park, Boulder Ridge, Providence Main, Trinity, and West Village; audit 327/327, current 328/328 after one Rocky Point addition; all 286 current future dates remain exact and 62/62 focused tests pass |
| Encore/Jonah SSR, finding 29 | **Implemented + locally/live verified 2026-08-02:** preserve `id_value` as the property-scoped source unit anchor plus Jonah record ID/slug and property/floor-plan IDs; keep apartment/building as display | Quattro, Bryn House, and Ascend NonaWest remain 101/101 with 75/75 future dates; untouched resource controls remain 306/306 with 169/169 future dates; 106/106 focused tests pass |
| Irvine, finding 30 | **Implemented + locally/live verified 2026-08-02:** use `propertyID + unitID` as canonical; retain `objectID`, floor-plan/community IDs, property address/name, and request binding | complete 13-property cohort remains 599/599 with 395/395 future dates; all 80 bare-ID collision extras are deliberate migrations; dedicated module plus registry tests pass 52/52 |
| AMLI, finding 31 | after P0-F filtering, use native `unitId` as canonical; retain `engrainUnitId`, nonzero `entrataUnitId`, floor-plan/property IDs, building, and public number | complete 11-property cohort; 254 current target rows remain 254, one Toscana public-number collision is accounted, and no rejected sibling receives an ID migration |
| On-Site Apply, finding 32 | **Implemented + locally/live verified 2026-08-02:** use native On-Site `id` as canonical; retain apartment/display numbers, style ID, unit-child property ID, street/building address, exact source property identity/request, numeric bath, and balanced plan name | original audit 284/284; current complete 49-property recheck finds 47 links and 39 active rosters with 367/367 physical rows after one proven non-unit application sentinel is excluded; 354/354 provable plans, 367/367 baths/rents/dates, and three live end-to-end controls pass |
| Equity, finding 33 | **Implemented + locally/live verified 2026-08-02:** use property-scoped `buildingId:unitId` as canonical on both Equity output paths; retain unit/building plus ledger or UnitFees property provenance; never treat `ledgerId` as apartment identity | complete current cohort: 25/26 unit-producing properties, 344/344 rows and source identities, 269/269 future dates, nine bare-ID collisions become explicit migrations; The Terraces remains redirect/no-response; retained Village fixture and 292-test affected suite pass |
| Essex, finding 34 | use native `unit_id` as canonical; retain native floor-plan ID plus page-derived property ID/request binding; keep public unit/building as display | complete current 27-property cohort remains 340/340 with 234/234 future dates exact; every source ID survives and all deliberate canonical migrations are ledgered |

**Shared implementation rules:**

1. Native IDs are property-scoped unless the vendor contract proves a wider
   scope. Preserve the public apartment label separately.
2. Do not deduplicate on mutable rent, date, or status fields.
3. Do not let an early adapter-local `seen` set discard a row before native or
   building identity is available.
4. Assert source count, admitted count, and final canonical-ID uniqueness at
   every adapter-to-Jugnu boundary.

**Acceptance gate:** every pinned source apartment in the table survives once,
all final canonical IDs are unique within the property, and the migration
ledger accounts for every deliberate ID change.

### P1-C: Publish Cortland base rent, not fee-inclusive total

**Evidence:** finding 9. On four current properties, 113/113 cards publish both
values; current output selects `Starting at ... incl. fees`, overstating base
rent by $15-$145.

**Implementation:** in `ma_poc/pms/adapters/cortland.py`, prefer the explicitly
labeled `Base Rent` value. Preserve the fee-inclusive amount separately only
if the schema has a truthful field for it; otherwise do not overload rent.

**Regression set:** Mirror Lake, Brier Creek, Cortland on Pike, and Alameda
Station, including a control card with only one price label.

**Acceptance gate:** all 113 pinned dual-price cards emit the source base-rent
value; no control loses its only valid rent.

### P1-D: Map AMLI's current unit-level dimensions

**Evidence:** finding 31. On the correctly property-bound 254-row current AMLI
cohort, source publishes bedrooms and building for every row, while final
output preserves neither. Twenty-three unit `squareFeet` values are replaced
by floor-plan minima.

**Implementation:** read `bedroomMax`/`bedroomMin`, preserve unit
`buildingNumber`, and prefer unit `squareFeet` over plan `sqftMin`. Keep the
currently correct base-rent and availability-date selection unchanged.

**Regression set:** the complete 11-property AMLI cohort, including all 23
current unit-versus-plan area differences and studio bedroom zero.

**Acceptance gate:** 254/254 current target rows retain source bedroom and
building; 254/254 retain exact unit square feet; 198/198 target future dates
and every source base rent remain unchanged.

### P1-E: Parse current On-Site baths and floor-plan objects — implemented

**Evidence:** finding 32. The original audit's 284 direct-route rows all lost
baths, and nested price objects broke exact source plan joins. The 2026-08-02
complete recheck expanded to 367 physical rows across 39 active rosters and
confirmed the same deterministic parser defects plus one non-unit roommate
application sentinel.

**Implementation:** complete. Parse top-level floor-plan objects with balanced
braces, join exact `style_id`, extract a positive bounded half-step bath value,
use the native unit ID, retain both number forms and source property lineage,
and fail closed on a missing/mismatched shell boundary. Preserve the existing
active-unit whitelist and do not synthesize a plan for the 13 rows without a
proven style-name binding. Exclude only the exact six-signal `Roommate Add On`
non-unit application option observed live.

**Regression set:** all 49 attributed properties: 39 current active controls,
three bound zero-unit controls, three HTTP-200 unbound error shells, two HTTP
500 controls, and the two no-link controls. Retained fixtures cover nested
`starting_term.best_price`, singular/plural and mixed-half bath labels,
unmapped styles, property mismatch, a legitimate child-property aggregate,
exact generic source plan names, and the roommate sentinel.

**Acceptance gate:** met locally/live. Original audit rows remain accounted;
the current 367 physical rows have unique canonical IDs, 367 numeric baths,
354/354 provable names exact with 13 unproven names blank, 367 rents and dates
exact, 345 published areas exact, and 22 source-missing areas truthful. The
354-test affected suite and three real adapter executions are green. Focused
GCP canary remains the release gate.

### P1-F: Make Essex empty exits observable and boundedly recoverable — implemented

**Evidence:** finding 34. Eight retained canary inputs are explicit Next.js 404
shells with no property ID. Belcarra retained a valid page and property ID but
the adapter discarded the bulk request's status/exception/body-shape outcome,
making its empty result unauditable. All nine exact configured URLs and APIs
currently work and expose 133 current rows.

**Implementation:** complete. Classify a source 404 shell independently from
property-ID absence and API failure, while recognizing that valid pages also
embed the global `/404` route. Match the configured community to its
`PropertyName + PropertyId` pair before accepting an API response. Record each
page/bulk attempt's requested/final page URL, property ID/name, HTTP status,
exception class, response shape/hash, row count, and mutually exclusive
outcome. On an explicit shell or retryable API outcome, perform exactly one
fresh configured-page/API retry. Accept no sibling response or guessed ID; keep
paid Web Unlocker disabled. Preserve native unit/floor-plan/property IDs and
the exact unit-producing response provenance.

**Regression set:** the eight retained 404 shells, Belcarra's valid page with
forced non-200/invalid/empty bulk responses, and current City View, The Palms,
and Avondale successes.

**Acceptance gate:** met locally. Every empty exit has one recorded cause; all
eight retained shells and Belcarra's forced non-200/JSON/shape/empty outcomes
are covered; a captured sibling response is rejected; and the complete current
27-property source-to-final replay is 340/340 with 340 unique native IDs, 234
future dates, 27 exact response-provenance records, and zero identity/value/date
mismatches. The 85-test Essex/registry suite is green. Focused GCP canary remains
the release gate.

### P1-G: Preserve trusted FortressTech micro-unit area and response provenance

**Evidence:** finding 35. Nine distinct Vivo Living Port Royal apartments each
publish a typed 282-square-foot value in the exact first-party SSR roster. The
adapter retains it, but `post_process`'s bedroom-relative heuristic nulls it
because the row is labeled one bedroom. The same full 170-row cohort and native
UUIDs are stable across canary and current capture.

**Implementation:** complete locally. The bedroom-relative exception is gated
by the exact FortressTech availability host/path, matching org/property and
native-unit UUIDs, the exact adapter tier, and the typed source field/value.
Absolute bounds still run first; ambiguous DOM/LLM and malformed-provenance
controls still clamp. Final output preserves the pre-sanity value, decision,
reason, and source field. The adapter records measured status/hash/URL and the
linked UUID boundary and explicitly disables paid Web Unlocker.

**Regression set:** all nine Vivo Beaufort rows, all 161 other current
FortressTech rows, and ambiguous 1BR/282 plus LLM/deposit-leak negative controls
that lack trusted structured provenance.

**Acceptance gate:** met locally. Vivo emits area 282 on all nine rows; the
other 161 areas and all 170 rows/native IDs/rents/plans/dimensions remain exact.
Every row has property UUID binding and all ten properties record the exact
unit-producing response provenance. The current source has 151 future dates
and all 151 are exact (the retained strict capture has 91); 441 focused tests
pass. Focused GCP canary remains the release gate.

### P1-H: Join ResidentServices365 unit context and reconcile plan output

**Evidence:** finding 36. The complete current ten-property cohort reproduces
all 108 canary apartments, but final output replaces all 108 visible plan names
with `Units`/GUID text, drops 108 lease terms and 34 floors, and retains stale
embedded dates on 41 rows whose UI says `Now` or `Today`. Telfair selects the
first hidden price instead of the visible best-value tuple on all 29 rows. The
Vue also expands ten source plan cards into 25 output rows.

**Implementation:** carry parent plan ID/name when discovering each detail URL
and verify it against the visible unit-block plan label. Parse visible floor
and lease term. For price matrices, select and preserve one explicit
rent/term/move-in tuple; prefer RS365's structured `Best Value` tuple when
present instead of DOM order. Treat visible `Now`/`Today` as current
availability and preserve explicit future labels exactly. Prevent the shared
URL-plan fallback from interpreting `/Units/` or an opaque GUID as a plan
name. Reconcile plan candidates by source plan ID or normalized
name/specification and prefer dedicated RS365 card values over generic
restatements.

**Regression set:** the complete ten-property cohort: 108 physical rows, 43
visible-current states, 65 future dates, 29 Telfair best-value tuples, 34 floor
labels, 108 lease terms, The Vue's ten plan cards, Westshore's four available
plan cards, and Rustic Woods' four plan cards.

**Acceptance gate:** 108/108 physical rows and native GUIDs survive; 108/108
plan names, 108/108 terms, and 34/34 floors match source; all visible-current
rows use capture-date semantics with provenance; all 65 explicit future dates
remain exact; every Telfair output represents one coherent source pricing
tuple; The Vue emits exactly ten plans, Westshore keeps four available counts,
and Rustic keeps both two-bedroom dimensions.

**Local result (2026-08-02):** met against a fresh independent source ledger.
The complete ten-property replay remains 108/108 physical rows and native
GUIDs, with 108/108 plan names, 108/108 terms, and 34/34 floors exact. Current
inventory now contains 42 visible-current and 66 explicit-future rows; both
classes are exact through the final formatter, including `available_now`
provenance. All 29 Telfair Best Value tuples match, and the 72-plan catalogue
is one-for-one (The Vue 10, Westshore 4, Rustic Woods 4). Thirteen source plan
cards publish zero rent and are intentionally normalized to null. Focused GCP
canary remains pending.

### P1-I: Keep RentalAddress plan dates and plan-only telemetry honest

**Evidence:** finding 37. Cedar Ridge is the complete one-property cohort. Its
two current plan cards and all plan values are exact, but neither card
publishes a unit, date, or available-now statement. Final output nevertheless
assigns both `UNKNOWN` plans the capture date. The same record is labeled both
`SUCCESS_PLAN_LEVEL` and `publish_ceiling=EXTRACTION_MISS`, with zero plans in
its provenance counters.

**Implementation:** in both floor-plan formatter copies, remove a manufactured
date from every no-anchor plan whose final state is not explicitly
`AVAILABLE`; do not use rent alone to date an inquiry-only plan. Pass the same
final plan-summary collection to verdict, publish-ceiling, provenance, and
output formatting. Count `floor_plans[]` in plan-level data-quality telemetry.

**Regression set:** the complete Cedar Ridge HTML and both current plan cards,
plus explicit `Available Now`, explicit future-date, explicit unavailable, and
rent-only `Check Availability` plan controls through both formatter entry
points.

**Acceptance gate:** both Cedar Ridge plans remain value-exact with null date
and `UNKNOWN` state; an explicitly available plan still earns capture-date
semantics and an explicit future date remains exact; property verdict,
publish-ceiling, and provenance all report the same two-plan plan-only result.

**Local result (2026-08-02):** met on the complete live one-property cohort.
The two current plans and all plan values remain exact; both inquiry-only rows
now have null date / `missing` provenance in both formatter copies. The exact
complete-surface proof converts the current page's ten explained rent tokens
to `CONFIRMED_PLAN_ONLY` with two plans, while unverified rent-bearing plan
collections still return `EXTRACTION_MISS`. Provenance reports two plan rows
and zero physical units. Focused GCP canary remains pending.

### P1-J: Reconcile AspenSquare's public roster with its stable Knock identity

**Evidence:** finding 38. The complete eight-property cohort preserves all 87
eligible Knock UUIDs, but loses all 87 public apartment labels and resolvable
buildings. All 63 retained exact Aspen-to-Knock joins use internal layout text
instead of the human plan name. Forty-two source-available rows become
unavailable solely because they are currently occupied; 41 have explicit
future dates. Four rendered current rows instead keep stale historical dates,
and Edgewood proves that a fallback roster can survive an exact marketing
source that publishes no available apartments.

**Implementation:** parse the current Next.js plan/availability data instead
of relying only on the legacy card selectors. Retain the Knock UUID as
canonical `unit_id`, but carry Aspen `unitNumber` as `unit_name`, resolve
`buildingId` through `units_data.buildings`, and join Aspen's human plan name
and source asset/unit/floor-plan IDs. Let an explicit `available=true` future
offering win over current occupancy; do not change the shared false/null Knock
semantics without a separate multi-property replay. Normalize a rendered
`Available Now` label to the capture date with explicit provenance and preserve
future dates exactly. Reconcile explicit empty, waitlist, and call-for-pricing
marketing states before admitting fallback-only units. Keep rent unchanged
until a term/move-in-aware displayed-price contract is specified. Reconcile
Adley's exact three-plan catalogue instead of retaining two generic extras.

**Regression set:** all eight Aspen properties, 87 eligible UUIDs, 63 retained
marketing joins, all 42 occupied/source-available cases, the four rendered
available-now rows, Avenue's two rendered occupied/future rows, Edgewood's
zero-roster control, Country Manor's live route conflict, Adley's exact three
plans, and Bridgepoint's current non-Aspen `available=true`/occupied/future
control. Add at least three non-Aspen Knock controls with false/null `available`
values before changing shared status logic.

**Acceptance gate:** all 87 UUIDs remain stable and unique; all 87 public labels
and buildings survive; all 63 exact joins carry Aspen's human plan; every
explicit future date is unchanged; visible current rows use capture-date
semantics; no source-available future row is rejected only for occupancy; an
explicitly empty marketing roster cannot silently become a clean fallback
success; Bridgepoint remains a future-available offering with its public
unit/building context; and non-Aspen Knock row/status counts remain explained.

**Local result (2026-08-02):** met against the complete current cohort, with
one source-directed admission delta. The mutable live source has 87 eligible
Knock UUIDs; 86 remain admitted and unique, while Edgewood's lone stale
`Common 1` is explicitly withheld because both exact marketing plans publish
empty rosters. All 86 admitted rows carry a public label, resolved building,
and exact human Aspen plan; 63 are exact current marketing-unit joins and the
23 outside Aspen's capped display window are visibly flagged. All 27 rendered
current rows reach final output with `available_now` provenance and all 52
admitted future dates remain exact. The exact 29-plan catalogue suppresses
generic hop extras. Bridgepoint plus three further non-Aspen live controls
preserve 77/77 labels/buildings and correctly retain all 40
`available=true + occupied` future offerings. The current API no longer
exposes false/null rows on those four controls; those branches remain unchanged
and are pinned separately by the retained 8,580/8,597-row canary contract plus
explicit false, null, and absent-value regressions. Focused GCP canary remains
pending.

### P1-K: Preserve Edifice future availability and bind the plan channel

**Evidence:** finding 39. The complete five-property Edifice cohort preserves
all 89 source apartments, all 42 exact empty-plan rows, and every value/date.
It nevertheless converts 30 future on-notice offerings to `UNAVAILABLE`.
Turtle Dove I also gains 29 generic plan rows after its exact catalogue wins;
three unambiguously match only Turtle Dove II.

**Implementation:** treat `on_notice` plus an explicit future date inside a
positive-`UnitsAvailable` roster as available for future leasing; do not let
present occupancy override that source contract. Preserve every existing date
and value mapping. Give property-bound vendor plan IDs/catalogues priority in
the link-hop plan merge. A generic plan with no name/source ID can enrich only
after an unambiguous match and can never survive as an independent row beside
the exact catalogue. Classify multi-UUID responses as distinct sibling,
aggregate, or strict subset using page labels, response plan IDs, and property
identity before union. Preserve Turtle Dove I/II separation and Newport's
current aggregate/subset behavior.

**Regression set:** all five primary catalogues, 89 physical rows, 42 exact
empty plans, all 30 future on-notice cases, both current Turtle Dove UUIDs,
both current Newport UUIDs, Turtle's 29 generic candidates, and the current
link-hop event shape (`generic:api_broad`/DOM entry evidence followed by exact
Edifice success).

**Acceptance gate:** 89/89 native IDs and every current unit value/date remain
exact; all 30 future on-notice offerings are `AVAILABLE`; all 42 empty plans
remain exact and negative; Turtle Dove I emits no Turtle Dove II plan shape and
no unnamed generic plan; Newport retains its exact 26-unit aggregate without
duplicating the strict subset; and every surviving plan records its actual
source response/property identity.

**Local result (2026-08-02):** met on the complete five-property live cohort.
All 70 plans, 89 native apartments, and 42 exact empty plans remain accounted;
all 30 future `on_notice` rows now finish `AVAILABLE` with exact dates and zero
status errors. Newport's five-plan aggregate deterministically outranks its
two-plan strict subset, while Turtle Dove II is phase-identity rejected. The
exact Edifice plan channel suppresses all 29 generic Turtle candidates, so no
unnamed or sibling-only plan survives. Exact unit and catalogue provenance is
retained. Focused GCP canary remains pending.

### P1-L: Make MarketApts' winning plan channel authoritative

**Evidence:** finding 40. The complete 29-property MarketApts cohort preserves
all 188 retained apartments, all 99 explicit future dates, and all 53
authoritative no-unit plans exactly. The shared merge nevertheless adds four
Ellis Midtown and two Riverbank generic plans whose rents are visibly labeled
deposits. The same rows lose dimensions, and Riverbank `Plan2` replaces two
future offerings with a synthetic capture date.

**Implementation:** when a property-bound adapter wins with physical units or
authoritative plan rows, do not union a lower-authority generic entry plan as a
new independent row. Permit enrichment only after an unambiguous plan identity
join, preserve the winning values on conflict, and carry source channel and
response URL into the merge decision. Make the generic financial-label parser
distinguish deposit from rent even when it runs without a later exact winner.
Derive a plan date/state only from the authoritative plan or unit roster; an
unlabeled dollar value is not availability evidence.

**Regression set:** all 29 MarketApts properties and all four current template
families; 188 retained apartment identities, 99 explicit future dates, and 53
exact no-unit plan rows; the post-canary Sunrise Station unit-change control;
Ellis Midtown's four generic candidates; and Riverbank's two generic candidates
plus both current future-dated `Plan2` apartments.

**Acceptance gate:** the 188 retained apartments, 99 future dates, and 53 exact
empty plans remain unchanged; the current extra Sunrise unit is reported as a
source delta rather than a baseline failure; no Ellis $200 or Riverbank $1,000
deposit appears as rent; Riverbank keeps its published $1,420/$1,625 starting
rents and future plan availability; and no lower-authority generic duplicate
survives beside an authoritative MarketApts row.

**Local result (2026-08-02):** met on the two affected properties and all four
dedicated template regressions. The retained source-to-final replay removes the
six generic rows while preserving Ellis's 7 physical + 4 exact empty-plan rows
and Riverbank's 4 physical rows. A fresh first-party replay confirms the generic
financial parser now skips all six labeled deposits and selects the separately
published asking rents ($1,450/$1,425/$981/$1,125 and $1,420/$1,625). The
dedicated adapter remains unchanged, and the 136-test focused suite is green.
The complete 29-property audit baseline (188 physical rows, 99 future dates, 53
exact empty plans) remains the focused-canary comparison gate.

### P1-M: Preserve MRI rent ranges and prefer its exact property route

**Evidence:** finding 41. Eight current MRI portals preserve all 91 native
`building:unit` identities, every low rent, and all 52 explicit future dates.
Elmtree nevertheless collapses nine explicitly labeled rent highs to the low.
Bridgepoint's exact Knock metadata publishes an MRI route for the same address
and provider code, but the route loses on a controlled `Bridgepoint I` versus
`Bridgepoint` suffix; the Knock fallback then loses public identity/plan/term
context and reverses the future offering's state.

**Implementation:** parse the first and last numeric values only from MRI's
`data-rent-range`, preserving a single value as equal low/high and rejecting
malformed or inverted ranges. Record the labeled source field. Allow an exact
MRI route surfaced by already property-bound Knock metadata to enter candidate
validation. Support a narrow phase/roman-numeral suffix alias only when the
provider code, full address, city/state/ZIP, and remaining normalized name stem
all match; do not weaken the general identity gate. Prefer the property-bound
MRI roster over its lossy Knock mirror. The shared Knock status/public-context
correction remains covered by P1-J.

**Regression set:** all eight direct MRI properties, 91 native identities, 52
future dates, Elmtree's nine multi-value and 82 fleet single-value rent rows,
plus Bridgepoint's marketing-page Knock binding, Knock metadata/roster, exact
MRI index/search response, configured-name suffix, and sole unit `8:807`.

**Acceptance gate:** all 91 identities, low rents, lease terms, and dates remain
exact; Elmtree emits highs of $865, $1,205, and $1,305 on the correct nine rows;
all 82 single-value rows remain equal-bounded; no unrelated name mismatch is
admitted; and Bridgepoint resolves to the exact property without losing stable
UUID provenance, public unit `807`, building `8`, plan `The Hampton`, term, or
future-available state.

**Local result (2026-08-02):** met on a fresh live replay of all eight original
direct MRI properties plus Bridgepoint. The original direct cohort remains 91
native rows; all 82 single-value rows remain equal-bounded and Elmtree's 3/5/1
rows now preserve highs of $865/$1,205/$1,305 through the production formatter.
The controlled suffix gate admits `Bridgepoint I` only with exact `BRI` and full
address/locality identity, while a mismatched name stem remains rejected.
Bridgepoint now resolves to MRI unit `8:807`, public unit `807`, building `8`,
plan `The Hampton`, 12-month term, August 10 available state, and the provider's
current $995–$1,240 range. The 32-test MRI/Knock suite is green; focused GCP
canary remains pending.

### P1-N: Reconcile RentCafe layout-tab surfaces before accepting a roster

**Evidence:** finding 42. Across the complete current 12-property cohort,
`/availableunits` returns only 89 of 187 apartments available through exact
plan drills, omitting 51 future-dated rows. Six properties also expose 13
shortcut rows with the wrong plan/bed semantics. Final canary output loses 65
published baths, duplicates Broadway unit `516`, and replaces all 31 exact
source plan names with generic labels and synthetic dates. Black Hawk's
nine-row SecureCafe fallback is clean.

**Implementation:** remove the unconditional non-empty `/availableunits` early
return. Treat the vanity page, SecureCafe portal, and bounded first-party plan
drills as candidates, then reconcile on property-scoped native apartment
identity. Prefer a plan-specific drill's plan, beds, baths, area, rent range,
and date over contradictory generic-page context. Parse bath from the exact
plan section/header after joining it to the handler plan. Share one native-ID
dedupe/conflict resolver across browser and code-only paths; never preserve a
conflict by prefixing the same raw apartment with two plan hashes. Make exact
plan cards authoritative over generic plan-text rows, preserving source names
and ranges and leaving unknown/inquiry plan dates absent. Keep explicit future
dates byte-for-byte unchanged and normalize visible `Available Now` only at the
formatter boundary.

**Regression set:** all 12 attributed properties and all 59 captured plan
drills; the 187 current unique apartment identities; all 96 current future
dates; the 98 shortcut omissions and six plan-conflict properties; Broadway
unit `516`; the 65 canary bath gaps; Vista's seven missing plan names; all 31
exact plan cards; Black Hawk's nine-row SecureCafe control; the complete
shortcut controls Vista Del Sol, Tudor Place, and 27Seventy; and 27Seventy's
308 repeated raw handlers collapsing to 28 semantically identical apartments.

**Acceptance gate:** every currently published native apartment appears once;
the exact drill union and final native-ID set agree; all 96 future dates and
all rent bounds remain exact; the 13 shortcut conflicts use exact drill
semantics; all source-published baths and plan names survive; Broadway `516`
appears once as the authoritative one-bedroom row; no `UNKNOWN` plan receives
a synthetic capture date; and Black Hawk plus the three complete-shortcut
controls remain value- and identity-exact.

**Local result (2026-08-02):** met across the complete 12-property source
corpus and a fresh live adapter replay. The reconciled result is 187/187 native
apartments, 187/187 published baths, zero raw-identity duplicates, all 13
shortcut conflicts corrected by exact drill context, and response provenance
accounting for every admitted apartment. The source-to-final replay preserves
all 96 explicit-future dates and leaves exact rent-only `UNKNOWN` plans
date-free. Broadway `516` appears once as `Broadway 1 Bedroom`; the actual
strict-canary plan harvest replay suppresses all 31 degraded generic rows.
Black Hawk remains nine exact rows and Vista Del Sol, Tudor Place, and
27Seventy remain complete controls. Woodland Hills succeeds through its
already-vetted `woodlandhillsirving.com` warm route; the obsolete configured
domain is now parked. The 409-test broad RentCafe/SecureCafe suite and
129-test focused source/reconciliation/formatter suite are green; focused GCP
canary remains pending.

### P1-O: Broaden Wix plan parsing and follow only property-bound unit maps

**Evidence:** finding 43. Across the complete three-property cohort,
Westerville's three visible labeled plans and Bellagio's six priced categories
produce no direct rows. Bellagio's exact first-party page additionally links a
3DPlans availability map that currently publishes unit `509`, plan `X09`, 1
bed/1 bath, 892 square feet, $2,089, available `14 Aug`; the canary still
finishes `FAILED_NO_DATA`. Vestawood is the boundary control: its mixed
AppFolio widget contains 18 target and 17 sister rows, and current code keeps
the correct 18.

**Implementation:** recognize bounded Wix plan sections with explicit labels
even when area is absent, separators are not pipes, or rent is a published
range. Keep missing area absent and preserve inquiry/contact availability as
unknown. Add candidate discovery for an external unit surface only when the
exact first-party property page explicitly labels it as its availability map.
Implement the narrow 3DPlans map shape with property/catalogue binding and
native unit/plan provenance; do not create a generic Wix outbound-link crawler.
Keep AppFolio's property-group title and `listable_uid` scope authoritative.

**Regression set:** all three attributed properties; Westerville's three plan
cards and inquiry state; Bellagio's six category ranges, 25 codes, exact map
UUID, and current unit `509`; and all 35 Vestawood widget cards with the exact
18 target UUIDs and 17 rejected Green Springs UUIDs.

**Acceptance gate:** Westerville emits exactly three source-faithful plan rows
without invented area/date/availability; Bellagio preserves all six category
ranges and emits every current map unit once with native identity and explicit
date; an unbound or contradictory map fails closed; Vestawood remains exactly
18-of-35 with every visible target value and no sibling row.

**Local result (2026-08-02):** met against the complete three-property source
cohort. Westerville emits exactly three `UNKNOWN` plan rows with the authored
names/specs/rents and no invented area/date. Bellagio's live source now has a
six-category rent conflict between its nav-labeled `PRICING` and `FLOOR PLANS`
pages; semantic reconciliation keeps the six newer pricing-page ranges while
using the floor-plan page's 25-code catalogue and labeled 3DPlans map. The map
still yields native unit `989` / `509 - Mountain Scenic View`, plan `X09`,
1/1, 892 square feet, $2,089, and `2026-08-14`, with full property/catalogue
binding and response provenance. Mismatched property metadata or catalogue
codes fail closed. Vestawood's Wix route remains empty and its unchanged
AppFolio scope remains exactly 18 target / 17 rejected sibling rows. The
combined 48-test Wix/AppFolio suite and 50-test source-id registry are green;
focused GCP canary remains pending.

### P1-P: Replace Camden suggestions and cross-products with exact plan drills

**Evidence:** finding 44. Across all 16 current Camden properties, the landing
adapter emits 130 suggested representatives while 182 exact first-party plan
pages publish 531 physical apartments. It omits 401 units, every floor, and
every lease term. The older reachable 396-row cross-product is worse: exact
drills prove 263 wrong native IDs, 232 wrong rents, 215 wrong dates, and ten
North End plan mis-associations.

**Implementation:** use the landing suggestion only to identify Camden. Fetch
the exact `/available-apartments` catalogue, then walk its bounded unique plan
slugs (28 is the current cohort maximum) and parse each
`data.floorPlan.units` array. Bind the catalogue and every drill to returned
community name/address/slug. Canonicalize on
`realPageCommunityId:unitId`, preserving native plan ID, plan slug, full public
label, floor, lease term, base monthly rent, and explicit date. Keep
`totalMonthlyRent` as separately labeled fee-inclusive provenance, never rent
high. Remove or redirect both generic `_camden` call sites so the
representative-value cross-product cannot win.

**Regression set:** the complete 16-property capture: 16 roots, 16 catalogues,
182 plan drills, and 531 exact unit composites; the 52 plans missing from root
suggestions; the 260 additional units inside suggested plans; Fallsgrove
`1.1E` units `4140` and `2041`; all 160 building-qualified labels and 24 bare-
label collisions; North End's three bare-unit-ID collisions and four reused
floor-plan IDs; and the complete 396-row legacy fallback replay.

**Acceptance gate:** the final native composite set equals the exact drill
union; every source row appears once with exact plan, dimensions, base rent,
date, floor, and term; all 130 current representatives remain exact while the
other 401 units are recovered; North End remains collision-free under the
community composite; zero generic cross-product row is admitted; and a failed
or contradictory drill produces explicit partial/failure telemetry rather
than a falsely complete success.

**Local result (2026-08-02):** implemented. Complete source-to-final replay
passes at 16/16 properties, 182/182 details, 531/531 unique community/unit
composites, 531 exact dates and terms, 520 exact positive floors plus 11
source-zero sentinels normalized to null, and 160 preserved qualified labels.
The 71-test focused suite is green. Direct live controls pass at Fairview
(10), Fallsgrove (14), and North End (63), including the multi-community
collision case. The two legacy generic call sites are gone and the deprecated
preview parser emits zero rows. Focused GCP canary remains the acceptance
gate.

### P1-Q: Follow authored Squarespace inventory links and reject internal plan placeholders

**Evidence:** finding 45. In the complete six-property Squarespace cohort,
Landmark publishes five exact apartment figures but returns no data, and
30Sixty links an exact property-scoped AppFolio roster with apartment `522`
but emits a synthetic `$1,940` plan instead. 250 High preserves all 11 physical
units and 12 legitimate empty plans but also emits a degenerate SightMap plan
named `TEMP`. Cricket, Tribeca, and Town Center are clean route/identity
controls.

**Implementation:** before fixed guessed subpaths, extract at most two exact
same-origin links that the captured property page itself labels as
availability, available apartments, pricing, or floor plans. Fetch only those
authored routes, preserve their final same-property boundary, and pass any
discovered AppFolio/LeaseLeads/SightMap/ManageBuilding surface through its
existing provider identity gate. Add a strict Squarespace figure-caption
parser that requires a physical unit label, bed/bath, area, rent, and
availability inside the same figure. Once a property-bound physical route is
present, suppress the generic labeled-price success for that property. In the
SightMap plan-presence pass, reject a provable placeholder with an internal
name such as `TEMP`, zero dimensions, and no unit, area, rent, or date. Do not
weaken the Cricket unit-block grammar or either clean provider identity gate.

**Regression set:** all six attributed properties; Landmark's complete five
figures; 30Sixty's exact `/availability-copy` link, AppFolio group `30Sixty
Apts`, listing `554`, and apartment `522`; Cricket's eight physical rows and
visible date tokens; Tribeca's exact LeaseLeads UUID and Barclay 1 term-price
control; 250 High's 11 physical units, 12 legitimate empty plans, and rejected
`TEMP` record `442397`; and Town Center's 17 exact ManageBuilding listing IDs,
15 exact plan joins, two honestly unnamed shapes, and 11 future dates.

**Acceptance gate:** Landmark emits exactly five source-faithful physical rows;
30Sixty emits current scoped AppFolio inventory and no synthetic `Labeled
Property Rent`; 250 High emits the exact 11 physical and 12 legitimate
plan-presence rows with no `TEMP`; Cricket remains 8/8; Tribeca's native unit,
date, dimension, floor, and term-price semantics remain intact; Town Center
remains 17/17 with no sibling/account leakage; and every explicit future date
in the pinned six-property capture survives unchanged.

**Local status:** implemented and satisfied against the complete pinned
six-property capture. Counts are Cricket 8, Landmark 5, Tribeca 21, 30Sixty 1,
250 High 11 physical + 12 legitimate plans, and Town Center 17; no synthetic
`Labeled Property Rent` or `TEMP` remains. The 2026-08-02 direct live replay is
also clean on all six. Live 250 High is currently 12 physical + 11 empty plans
(23 total) because one formerly empty plan gained an apartment; this is source
drift, not a changed acceptance baseline. Source-to-final availability tests,
601 focused provider regressions, Ruff, and compilation pass. Strict focused
GCP canary remains pending.

### P1-R: Support current ThinkReside cards and preserve `available_now`

**Evidence:** finding 46. Indy Flats is an exact 46-unit control with eight
future dates, but all 38 visible `Now` rows are normalized inside the adapter
to a UTC date and mislabeled `explicit_capture_date`. Deer Run's current
towncommunity index publishes exactly four undated plan cards with exact areas,
rent ranges, and detail slugs. The dedicated adapter rejects that shape, after
which shared fallbacks emit seven rows, drop every area/slug, and fabricate a
capture date on all seven.

**Implementation:** add a narrow `li.floorplan` parser that requires one
property-bound card containing a name/link plus structured bed, bath, area,
and/or price child fields. Preserve both area and rent ranges and the exact
detail slug. A positive catalogue price must not imply availability; without
a physical apartment, available-unit count, or explicit availability token,
emit `UNKNOWN` with no date. A successful dedicated catalogue parse must
suppress overlapping generic plan candidates. Change the unit-table date
boundary to retain raw `Now` or explicit provenance until the formatter, which
normalizes it using the run capture date and records `available_now`; retain
explicit future dates exactly. Keep the existing composite source identity of
plan slug plus apartment label so repeated Indy labels remain distinct without
row loss.

**Regression set:** both attributed properties; Deer Run's four exact current
cards and four detail pages; the rejected three generic duplicates; all 46
Indy physical source composites; all seven exact empty-plan slugs; the five
repeated apartment-label families; all 38 `Now` rows; and all eight explicit
future dates.

**Acceptance gate:** Deer Run emits exactly four plan rows with exact names,
beds, baths, area/range, rent/range, and plan slugs, all `UNKNOWN` and undated;
no generic duplicate survives. Indy remains exactly 46 units plus seven
legitimate empty plans, every composite source identity and value is exact,
all 38 immediate rows carry `available_now` normalized to the run capture date,
and all eight future dates survive unchanged.

**Local status:** implemented and fully replayed. The saved two-property source
passes the acceptance counts and values exactly. Direct first-party replay also
passes both attributed properties plus Ridge at Perry Bend as a third current
ThinkReside control. The expanded adapter suite is `29 passed`; the combined
availability/plan-boundary set is `149 passed`. Production Web Unlocker is
explicitly disabled on this adapter's internal fetches. Focused GCP canary is
still pending.

### P1-S: Reconcile Wix provider routes and preserve authored plan records

**Evidence:** finding 47, the complete 18-property `wix_nopms` cohort. Seven
properties expose structured provider routes, four expose plan-level Wix
records/cards, four current property-bound sources are missed, and three are
defensible current no-data controls. Exact defects include The Marq's
application-only `waiting list` card emitted as a unit; Millennium's four
address-matched AppFolio apartments declined because its authored component
omits `propertyGroup`; Parkline's internal `TEMP` plan and two wrong marketing
rent joins; and lossy generic output on all four Wix-plan sites.

**Implementation:**

1. Expand the AppFolio waitlist classifier to cover current morphological
   variants such as `waiting list`, while retaining the stable card UID in
   rejected-row telemetry.
2. Permit a no-`propertyGroup` AppFolio index only when the exact index is
   published by a bounded same-site, operator-authored Wix component. Admit
   only cards whose full street/city/state/ZIP matches the configured property;
   require stable listable UIDs; record admitted and rejected card identities.
   Never use ZIP-only scope for this shape.
3. For repeated Wix CMS records/cards, preserve one record boundary and its
   stable CMS/card identity. Parse source plan name/code, beds, baths,
   area/range, rent low/high, and deposit from labeled fields; do not flatten
   the card before money and area labels are known.
4. Keep a property-bound plan without a physical unit or affirmative
   availability signal as `UNKNOWN` and undated. Positive catalogue rent is
   not availability. A dedicated Wix-record success must suppress overlapping
   generic rows.
5. Apply the SightMap placeholder gate from P1-Q to Parkline and join marketing
   rent only on an unambiguous category/shape. Preserve the three physical
   SightMap rows exactly.
6. Leave East Hampton, 16 Bennett, and Hoyt Tower at no-data when their current
   widgets/pages remain empty; broad bedroom-mix prose, parking charges, and
   application forms are negative controls, not inventory.

**Regression set:** all 18 attributed properties; all 55 current AppFolio
cards and native listable UIDs, including The Marq's rejected UID
`96f414c2-063b-44b9-8d1e-d01bd95ee172`; Park Place's nine exact DoorLoop
listings; Parkline's three units, 13 legitimate empty plans, rejected `TEMP`
record `410367`, and exact `H`/`M` marketing ranges; all 27 current Wix plan
records; Indian Village's two plans; Westgate's two exact authored routes;
Allen Ranch's one available plan; Millennium's four admitted and seven
rejected AppFolio cards; and the three current no-data controls.

**Acceptance gate:** provider routes emit exactly 66 physical apartments and
no waitlist application; all provider-native IDs and current mapped values
remain exact. Parkline emits three units plus 13 legitimate empty plans, no
`TEMP`, and no ambiguous rent join. The four Wix-plan sites emit exactly 27
source-faithful, stable-ID, `UNKNOWN`, undated plan rows; Indian Village,
Westgate, and Allen Ranch emit exactly five source-faithful plan rows with only
Allen Ranch affirmatively available. Millennium emits exactly the four
configured-address apartments, preserves apartment `409`'s 8/9/26 date, and
rejects all seven siblings. East Hampton, 16 Bennett, and Hoyt remain honest
no-data controls.

**Local status:** implemented. The bounded regression fixtures now assert all
27 authored Wix plans, the five recoverable failed-output plans, Millennium's
four exact-address admissions/seven sibling rejections, The Marq waitlist
rejection with native UID, SightMap placeholder/rent-join guards, and all three
empty controls. The focused AppFolio/Wix/SightMap set is green (`81 passed`).
Strict focused GCP canary remains pending.

### P1-T: Preserve Yotta plan/floor identity and `Today` provenance

**Evidence:** finding 48, the complete three-property cohort. The current
property-scoped APIs and fresh end-to-end replay preserve all 58 apartments,
all mapped values, and all 41 explicit future dates. The remaining losses are
exact: 58/58 word-ordinal floors publish as null; 17 provider plan types
collapse to 10 final plan IDs because `dbaUnitTypeId`/`dbaUnitTypeCode` are
dropped; 17 literal `Today` rows never receive `available_now` provenance;
and DBA/unit-response provenance is absent from final output.

**Implementation:**

1. Add `yotta_dba_id`, `yotta_floor_plan_id`, and
   `yotta_floor_plan_code` alongside `yotta_unit_id` in `source_ids`. Retain
   the provider's generic layout description separately when the plan code is
   the distinguishing source label.
2. Drive Yotta plan grouping from the stable property-scoped
   `dbaUnitTypeId`, not only generic description/beds/baths. Do not make
   square footage or rent part of identity.
3. When `dateAvailable` is the literal `Today`, preserve that semantic token
   through the formatter so the normalized date is the capture date and the
   provenance is `available_now`. Otherwise preserve the explicit
   `MoveInDateAvailable` value exactly; do not timezone-shift it.
4. Extend the bounded floor formatter to recognize exact word ordinals such
   as `First Floor`, `Second Floor`, and `Third Floor`, while retaining the
   original label in `floor_raw` and keeping the existing 1-100 sanity bound.
5. Populate `unit_source_provenance` with the sanitized
   `GetFloorPlans/{dba}/1` URL, source/admitted counts, and the already-proven
   property-identity verdict. Never persist the response body.

**Regression set:** DBAs `200`, `201`, and `55`; all 58 native unit IDs; all
17 provider plan IDs/codes; all 58 word-ordinal floors; all 17 `Today` rows;
all 41 explicit future dates; three exact `GetDBADetails` identity controls;
and sibling/ambiguous-DBA rejection fixtures.

**Acceptance gate:** the cohort remains exactly 27/18/13 physical rows with
unique native IDs and zero mapped-value differences. Published plan identity
has 7/5/5 distinct provider anchors, all 58 floors normalize to 1/2/3 while
their raw labels survive, all 17 immediate rows carry `available_now`, and all
41 future dates remain unchanged. Every property records one exact
unit-producing response and its passed identity gate.

**Local status:** implemented and live-replayed. Direct current DBA requests
followed by the production formatter return 27/18/13 units, 7/5/5 provider plan
anchors, 58/58 normalized word-ordinal floors, 17/17 `available_now` rows,
41/41 exact future rows, and one hashed property-MATCH response per property.
The closure suite is part of the green 321-test final-finding set. Strict
focused GCP canary remains pending.

### P1-U: Preserve non-registry dates, plans, and response provenance

**Evidence:** finding 49, the complete four-owner cohort. All 26 current
physical rows and mapped values replay exactly, but ShowMojo listing
`e7c39f1061` says `Available September 7th` and publishes as the capture date;
12 other ShowMojo rows lose explicit `available_now` provenance; 1515 Park
Place's five exact stack/plan records are discarded; and all four winners emit
an empty `unit_source` list.

**Implementation:**

1. Pass ShowMojo's exact `availability_text` into the canonical date path.
   Normalize only bounded English ordinal suffixes on a recognized month/day
   expression before parsing (`7th` -> `7`); retain the untouched source token
   in raw output. Preserve `Available now` so it resolves to the capture date
   with `available_now` provenance.
2. Have the static residence-table recovery return two channels: the existing
   three physical residences and plan summaries for numeric stack ranges.
   Preserve stack code, exact plan-image URL/asset code, beds, baths, and rent
   range. Mark each stack `UNKNOWN`, undated, and plan-level; never expand a
   range into apartment numbers.
3. At the shared bare-list recovery bridge, translate each owner's existing
   bounded telemetry into `unit_source_provenance`: sanitized winning URL,
   source/admitted counts, response hash where available, and passed
   property-identity verdict. Persist no response body and do not infer a
   property verdict from row count alone.
4. Leave BetterNOI's nine rows and NestHub listing `602` unchanged except for
   additive response provenance.

**Regression set:** Vista's nine native UUIDs and three published plan UUIDs;
Annaberg's 33-row roster, one admitted listing, and rejected configured
listing `56`; Park Northside's 52-row account, all 13 admitted UIDs, 39
rejected cards, 12 `Available now` tokens, and future UID `e7c39f1061`; all
eight current 1515 Park Place table rows, including the three physical codes
and five stack/asset identities.

**Acceptance gate:** physical output remains exactly 9/1/13/3 with zero
identity or mapped-value differences. ShowMojo emits 9/7/26 for
`e7c39f1061`, all 12 immediate rows carry `available_now`, and no missing-year
date is shifted backward. 1515 Park Place emits exactly three physical units
plus five `UNKNOWN`, undated plan rows. Each winner records the exact
unit-producing response and passed identity gate; no portfolio sibling is
admitted.

**Local status:** implemented. Direct Park Northside source-to-final replay is
13/13 with 12 `available_now`, one `explicit_future` at `2026-09-07`, and two
hashed MATCH roster responses covering all 13 rows. BetterNOI and NestHub
retain their physical outputs with exact-response provenance, and 1515 Park
Place retains its three residences plus five `UNKNOWN`, undated stack plans.
The combined final-finding closure set is green (`321 passed`). Strict focused
GCP canary remains pending.

### P2-A: Preserve SecureCafe native `UnitID`

**Evidence:** finding 13. Four current legacy pages publish 87 distinct native
unit IDs, none of which the parser keeps. The sampled display labels happen to
be unique today, so this is a stability fix rather than a current collision
recovery.

**Implementation:** extract `UnitID` from both input/button and anchor/action
encodings, register it as a stable source ID, and make it canonical when
present. Keep the visible apartment label and the Woodman no-native-ID
fallback.

**Acceptance gate:** all 87 pinned native IDs survive and are canonical; the
four Woodman display identities remain valid.

### P2-B: Remove lossy generic pre-deduplication

**Evidence:** finding 14. Both generic API parsers reduce the current Coventry
eight-apartment source to five when fed the reachable generic shape, although
the normal dedicated Spherexx winner correctly preserves all eight.

**Implementation:** prefer removing the early `unit_num`-only `seen` gate. If
an early guard is still required, key it by stable native ID or normalized
building plus apartment label. Let the common identity layer perform final
deduplication after all disambiguators exist.

**Acceptance gate:** generic parser replays preserve Coventry 8/8, Fairmount
2/2, and Irondale 6/6. The dedicated Spherexx winner remains unchanged.

### P2-C: Repair only explicit G5 dimension contradictions

**Evidence:** finding 17. Eagle Rock Woodbury publishes 40 zero-dimension rows
whose `1 Bed`, `2 Bed`, `1X1`, and `2X2` codes are unambiguous; two
same-operator controls publish correct numeric dimensions.

**Implementation:** when and only when numeric zero contradicts an explicit
non-studio plan/apartment token, derive beds/baths from that token and record
correction provenance. Preserve zero for Studio and ambiguous names.

**Acceptance gate:** all four Woodbury source shapes are corrected; Towson,
West Hartford, and genuine studio fixtures are byte-for-byte unchanged.

## Deliberate no-fix scope

- UDR identity (finding 4) is already corrected on this branch; only its date
  join needs work.
- Knock identity (finding 5) is already corrected and needs regression
  protection only.
- The normal dedicated Spherexx winner in finding 14 is correct; the fix is
  limited to the reachable generic fallback.
- SecureCafe future-date comparison (finding 18) is not a justified parser
  fix: four current public sources contain no row/action date. Further work is
  a separately measured endpoint-discovery experiment. Saybrooke remains
  unverified rather than assumed.
- RealPage numeric OnlineLeasing and CWS GetUnits (finding 26) require no
  current fix: six controls preserve 365/365 rows and native IDs plus 78/78
  explicit future dates. Keep the stateful OLL workflow and CWS plan fallback
  outside that clearance until separately exercised.
- CAPTCHA solving, Web Unlocker, FlareSolverr, and fingerprint rotation remain
  excluded from production. Hyperbrowser remains an allowed clean production
  fetch path under its existing bounded configuration.

## Implementation sequence

Each numbered step should be an independently reviewable commit with its own
tests and replay evidence.

1. Freeze sanitized real-shaped fixtures and a source-to-final assertion
   harness. Fixtures must contain only fields necessary to reproduce each
   defect.
2. Implement P0-A OneSite and P0-F AMLI property binding and quarantine the
   confirmed sibling routes from any candidate profile set.
3. Implement P0-B shared availability semantics.
4. Implement P0-C RentCafe Applicant state/date handling.
5. Implement P1-A UDR and Harbor date preservation.
6. Implement P1-B identity changes and generate the canonical-ID migration
   ledger.
7. Implement P1-C Cortland base-rent selection, P1-D AMLI dimensions,
   P1-E On-Site field parsing, P1-F Essex recovery telemetry, P1-G
   FortressTech structured-area preservation, P1-H ResidentServices365
   field/price/plan reconciliation, P1-I RentalAddress plan/telemetry
   correction, P1-J AspenSquare public-roster reconciliation, P1-K Edifice
   future-status/property-bound plan reconciliation, P1-L MarketApts
   source-authoritative plan reconciliation, P1-M MRI range/property-route
   reconciliation, P1-N RentCafe layout-tab surface reconciliation, P1-O Wix
   plan/unit-route recovery, P1-P Camden exact-plan-drill recovery, P1-Q
   Squarespace authored-route/placeholder recovery, and P1-R ThinkReside
   current-card/provenance recovery, P1-S Wix-no-PMS provider/plan-record
   reconciliation, P1-T Yotta plan/floor/provenance preservation, and P1-U
   non-registry date/plan/provenance preservation.
8. Implement P0-D repeated-roster reconciliation and P0-E Repli waitlist
   semantics, plus the RentManager/RentVision identity rows in P1-B.
9. Implement P2-A/P2-B/P2-C stability and narrow normalization fixes.
10. Run the combined focused canary; investigate every row-count or identity
   delta before a full run.
11. Trial-merge `origin/main`, rerun affected suites, then run the 4,982-property
    full canary only after all focused gates pass.

## Verification ladder

### 1. Local deterministic tests

Run the relevant adapter suites plus formatter, schema, identity,
post-processing, and Jugnu boundary tests. Every new regression must assert the
complete source -> adapter -> formatter -> post-process result, not parser row
existence alone.

### 2. Current live probes

Re-probe at least three properties for every family-level rule. Use direct
public fetch first and the bounded clean Hyperbrowser path only for current
403s. Label the capture time and do not compare mutable live row counts to a
stale fixture without explanation.

### 3. Focused canaries

Run four small cohorts before paying for a full canary:

1. **Property-boundary cohort:** the three contaminated Mark-Taylor properties,
   the adopted San Norterra portal, correct OneSite controls, and all eleven
   AMLI properties from finding 31, plus all seven Wix-no-PMS provider routes,
   Millennium's four admitted and seven rejected AppFolio cards, and the three
   current Wix no-data controls from finding 47, plus Yotta DBAs `200`, `201`,
   and `55` with their exact `GetDBADetails` identity gates from finding 48,
   plus all four exact non-registry property/portfolio boundaries from finding
   49.
2. **Availability cohort:** all 44 baseline Razz contradiction properties, all
   20 UDR properties, the three Harbor properties, the three RentCafe
   Applicant probes, all ten ResidentServices365 properties from finding 36,
   Cedar Ridge from finding 37, all eight AspenSquare properties from finding
   38, all five Edifice properties from finding 39, and Bridgepoint's paired
   Knock/MRI future-availability control from finding 41, plus the complete
   RentCafe layout-tab cohort and its 96 current future dates from finding 42,
   plus Bellagio's exact linked unit map and future unit `509` from finding 43,
   plus all 531 current Camden future dates from finding 44, plus the complete
   six-property Squarespace cohort and its pinned explicit future dates from
   finding 45, plus both ThinkReside properties, Indy Flats' 38 visible `Now`
   rows, and its eight explicit future dates from finding 46, plus the complete
   Wix-no-PMS cohort, Millennium apartment `409`'s 8/9/26 date, Parkline's
   three dates, and all undated Wix-plan controls from finding 47, plus
   Yotta's 17 literal `Today` rows and all 41 explicit future dates from
   finding 48.
   Include Park Northside's 12 current tokens and future ShowMojo UID
   `e7c39f1061` from finding 49.
3. **Identity cohort:** every affected and control property named in P1-B,
   SecureCafe finding 13, generic finding 14, Lake Haven, the three
   Razz/ResMan overlap controls, all four Apts247 finding 24 controls, and the
   three Funnel Spaces finding 25 controls, plus every Repli360 finding 27
   identity and waitlist control, and all six MAAC finding 28 controls.
   Include the three Encore/Jonah SSR controls and all three scoped-clean
   resource-JSON controls from finding 29, plus the complete 13-property
   Irvine cohort from finding 30 and the property-bound 254-row AMLI target
   cohort from finding 31, plus all 28 active On-Site properties and the
   retained Mill Creek control from finding 32, all current Essex properties,
   the nine retained Essex failure-shape fixtures from finding 34, and the
   complete FortressTech cohort from finding 35, plus all 87 Aspen UUIDs and
   all 63 retained Aspen marketing joins from finding 38, plus the Turtle Dove
   and Newport multi-UUID controls from finding 39, plus Bridgepoint's exact
   Knock-to-MRI route and controlled name-suffix case from finding 41, plus all
   187 RentCafe layout-tab native identities and Broadway unit `516` from
   finding 42, plus Vestawood's 18 target and 17 rejected sibling UUIDs and
   Bellagio's exact map/property binding from finding 43, plus all 531 Camden
   community/unit composites and the North End collision controls from finding
   44, plus the exact provider/property bindings for all four physical-unit
   Squarespace routes from finding 45, plus all 46 Indy Flats plan-slug/unit
   composites and the five repeated-label controls from finding 46, plus all
   55 Wix/AppFolio listable UIDs, nine DoorLoop IDs, three SightMap unit IDs,
   Millennium's exact address boundary, and The Marq's rejected waitlist UID
   from finding 47, plus all 58 Yotta unit IDs and all 17 Yotta provider
   plan/DBA anchors from finding 48, plus all 26 non-registry physical source
   identities and their rejected portfolio controls from finding 49.
4. **Value cohort:** the four Cortland price properties and three G5 dimension
   properties, plus all eleven AMLI properties with their 23 current unit-area
   differences, the On-Site bath/plan cohort from finding 32, and the complete
   ResidentServices365 plan/term/floor/date/best-price replay from finding 36,
   plus Aspen's exact plan/unit/building labels and Adley's three-plan
   catalogue from finding 38, plus all 89 Edifice rows and 42 exact empty-plan
   rows from finding 39, plus the complete 29-property MarketApts replay and
   the six generic plan candidates from finding 40, plus all 91 direct MRI rows
   and Elmtree's nine published rent ranges from finding 41, plus the 65
   RentCafe layout-tab bath gaps, 31 exact source plans, and six shortcut-plan
   conflicts from finding 42, plus Westerville's three inquiry plans and
   Bellagio's six category rent ranges and current unit values from finding 43,
   plus all Camden exact drill values and the complete unsafe-fallback replay
   from finding 44, plus Landmark's five exact figures, 30Sixty listing `554`,
   250 High's rejected `TEMP` record, and the three clean Squarespace controls
   from finding 45, plus Deer Run's exact four-card catalogue/ranges and three
   rejected generic duplicates, and Indy Flats' complete current value replay
   from finding 46, plus all 27 current Wix plan records, the five recoverable
   failed-output plans, Millennium's four exact apartments, Parkline's
   `TEMP`/`H`/`M` controls, and the three current no-data controls from finding
   47, plus all 58 Yotta word-ordinal floors, plan codes, mapped values, and
   unit-producing response records from finding 48, plus all eight 1515 Park
   Place table rows and the four non-registry unit-source records from finding
   49.

The combined focused report must retain raw source count, parser count,
formatted count, final admitted count, canonical-ID uniqueness, property
identity verdict, availability provenance, and actual winning response URL.

### 4. Full 4,982-property canary

Compare against the exact baseline above and report both coverage and quality.
The full run passes only when:

- no audited route admits a property-identity mismatch;
- explicit-negative rows with `capture_date_default` fall from 8,087 to zero;
- every parseable explicit future date survives unchanged;
- no adapter loses a source apartment through display-number-only dedup;
- canonical unit IDs are unique per property, with deliberate migrations fully
  accounted for;
- Cortland base rent and the narrow G5 dimension corrections match source;
- any reduction in reported success is explained property by property as a
  rejected false success or a genuine live-source change; and
- plan-to-unit and failed-no-data conversion metrics are reported separately
  using the same strict gate as the previous cohorts.

## Promotion and rollback

- Do not promote warm profiles merely because their routes still fetch. A
  route is promotable only after property identity and output-quality gates
  pass on the fixed code.
- Regenerate the local positive-only profile candidate from the fixed focused
  canary, then from the fixed full canary. Never merge a newer route over an
  older good route without field-level evidence.
- Keep each work package separately revertible. If a focused cohort violates a
  stop gate, revert or disable only that package; do not roll back unrelated
  plan-to-unit or failed-no-data gains.
- Preserve all audit ledgers, fixture hashes, canary prefixes, and migration
  ledgers so a later run can reproduce every admission decision.
