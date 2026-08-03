# Adapter data-quality findings

Date: 2026-08-01 (America/Chicago)
Branch: `codex/consolidated-canary-2026-08-01` at `fa1afb7`
Status: all 49 confirmed findings implemented locally; consolidated regression
and strict focused GCP canary/reconciliation remain pending

This ledger records only defects that have all three of the following:

1. a reachable production code path;
2. current live source evidence or a retained real capture; and
3. a deterministic source-to-adapter-to-output reproduction.

The findings below do not rely on RealPage as ground truth. They compare the
operator's current public source and visible marketing UI with SurgeX's own
adapter output.

## 1. RentCafe Applicant API manufactures availability on inquiry-only plans

### Verdict

Confirmed. The Applicant FloorPlansV2 parser treats a published floor plan
with no physical units as available merely because it has a rent. The output
formatter then manufactures the capture date. It also drops a waitlist-only
plan and can prefer a stale historical date over the current availability
window shown on the marketing site.

### Primary manually validated property

- Canonical property: `239094`, Zander Place
- Marketing page: <https://www.zanderplace.com/floorplans>
- Applicant API property ID: `450410`
- Backing endpoint:
  <https://zanderplace.securecafeapplicant.com/onlineleasing/api/floorplan/getfloorplanandavailableunits?propertyId=450410&RequestBeforeLogin=true&isPropertyList=false>

The marketing page visibly distinguishes two states:

- inquiry-only cards show **Inquire for details** and **Contact**, with no
  available apartment or availability date;
- real inventory cards show **Starting at**, **Available On**, and
  **View Details**.

The user manually confirmed this distinction in the live UI. Current source
and output reproduction:

| Plan | Visible UI | Current source | Current SurgeX output |
|---|---|---|---|
| F-NP (Surface Lot Parking Only) | Inquire / Contact | `AvailableUnits=0`, `IsFullyOccupied=true`, empty `UnitAvailability` | plan-level `AVAILABLE`, `available_date=2026-08-01` |
| 1C1NP (Surface Lot Parking) | Inquire / Contact | `AvailableUnits=0`, `IsFullyOccupied=true`, empty `UnitAvailability` | plan-level `AVAILABLE`, `available_date=2026-08-01` |
| 1CNP (Surface Lot Parking Only) | Inquire / Contact | `AvailableUnits=0`, `IsFullyOccupied=true`, empty `UnitAvailability` | plan-level `AVAILABLE`, `available_date=2026-08-01` |
| C | Inquire / Contact | `AvailableUnits=0`, `IsFullyOccupied=true`; one `WAIT147S` pseudo-unit with status `Waitlist` | entire plan is dropped after the pseudo-unit is rejected |
| E | Inquire / Contact | `AvailableUnits=0`, `IsFullyOccupied=true`, empty `UnitAvailability` | plan-level `AVAILABLE`, `available_date=2026-08-01` |
| B / unit 202 | Available On 8/1/2026 | stale `AvailableDate=04/01/2026`; current `UnitAvailableStartDate=2026-08-02T00:00:00` | `available_date=2026-04-01` |

The one-day difference between the UI's August 1 and the API window's August
2 is consistent with local-time display. The defect is not that one-day edge;
it is that the adapter selects the stale April 1 field and misses the current
August availability window entirely.

### Three-property live scope check

The same FloorPlansV2 path was probed through current public JSON on three
properties:

| Property | Source plans with rent but no physical unit inventory that SurgeX marks available |
|---|---:|
| Georgetown Crossing (`234581`, Applicant property `622474`) | 3 |
| Zander Place (`239094`, Applicant property `450410`) | 4 |
| Stockbridge Trails (`240595`, Applicant property `296857`) | 12 |
| **Total** | **19** |

All 19 reproduced as plan-level `AVAILABLE` with the capture date. This is a
three-property confirmation of the parser behavior, not a claim that every
RentCafe property is affected.

### Reachable code path

- `ma_poc/pms/adapters/rentcafe.py:1795` branches only on whether
  `UnitAvailability` is a non-empty list; it does not use `AvailableUnits`,
  `IsFullyOccupied`, or `FloorPlanAvailable` as availability truth.
- `ma_poc/pms/adapters/rentcafe.py:1800` rejects the waitlist pseudo-unit, but
  the non-empty-list branch has no plan fallback after all entries are
  rejected.
- `ma_poc/pms/adapters/rentcafe.py:1830` chooses legacy `AvailableDate` and
  ignores `UnitAvailableStartDate`.
- `ma_poc/pms/adapters/rentcafe.py:1839` emits the empty-inventory plan through
  `make_unit_dict` without an explicit status or `available_units` value.
- `ma_poc/pms/adapters/_parsing.py:1186` defaults that missing status to
  `AVAILABLE`.
- the production Jugnu formatter consequently resolves the missing date to
  the scrape date for the rent-bearing row.

### Required behavior

- Preserve inquiry-only plans as plan-level evidence; do not publish them as
  available apartments.
- Carry the explicit `AvailableUnits=0` signal and map fully occupied plans to
  a non-available state with `available_date=null`.
- Preserve a waitlist-only plan after filtering its pseudo-unit, with honest
  waitlist/unknown status rather than dropping the plan.
- For real unit rows, prefer the current availability window and/or visible
  marketing date over a conflicting historical `AvailableDate` field.
- Add regression fixtures that cover the complete adapter-to-Jugnu formatter
  boundary, not only parser row existence.

## 2. AvalonBay discards the unique native apartment ID

### Verdict

Confirmed. AvalonBay's embedded Fusion inventory supplies a unique native
`unitId` for every apartment, but the adapter selects the shorter visible
`unitName`, does not preserve `unitId` in `source_ids`, and the formatter uses
that non-unique visible number as `unit_id`.

### Primary manually inspectable property

- Canonical property: `26892`, Avalon at Arlington Square
- Marketing page:
  <https://new.avaloncommunities.com/virginia/arlington-apartments/avalon-at-arlington-square/#community-unit-listings>

The current embedded Fusion payload contains:

- 81 apartment records;
- 81 unique native Avalon `unitId` values; and
- only 47 distinct visible `unitName` values.

Therefore 34 rows collide when visible number alone is used as identity.

Example: visible unit number `303` represents six distinct apartments:

| Native `unitId` | Floor plan | Area |
|---|---|---:|
| `AVB-VA023-016-303` | A2-Std-Upper | 652 |
| `AVB-VA023-024-303` | A3-S-Upper | 826 |
| `AVB-VA023-038-303` | A4-H-Upper | 852 |
| `AVB-VA023-025-303` | A4-E-Upper | 852 |
| `AVB-VA023-023-303` | A4-S-Upper | 852 |
| `AVB-VA023-036-303` | A4-E-Upper | 852 |

The adapter emits all six as `unit_number=303`, with no Avalon native ID in
`source_ids`. The output formatter consequently emits six identical
`unit_id=303` values inside one property.

### Three-property live scope check

| Property | Source apartments | Unique native IDs | Distinct visible numbers | Current visible-number collisions |
|---|---:|---:|---:|---:|
| Avalon at Arlington Square (`26892`) | 81 | 81 | 47 | 34 |
| Avalon Meydenbauer (`36964`) | 27 | 27 | 27 | 0 |
| Avalon Montville (`262540`) | 25 | 25 | 25 | 0 |

All three currently lose their native IDs. Only Arlington Square currently
produces duplicate visible-number identities, so the confirmed collision
blast radius is one property; this is not presented as an Avalon-wide
collision rate.

### Reachable code path

- `ma_poc/pms/adapters/avalonbay.py:68` asks `get_field` for `unitName` before
  `unitId`, so the unique ID is ignored whenever both exist.
- `ma_poc/pms/adapters/avalonbay.py:133` passes the visible value as
  `unit_number` and supplies no `source_ids` containing the native Avalon ID.
- the production formatter prefers `unit_number` as `unit_id`; fallback
  identity resolution never gets a chance because the visible number is
  non-empty.

### Required behavior

- Keep the visible `unitName` as display metadata.
- Preserve the native Avalon `unitId` as the stable per-unit identity and in
  `source_ids`, with its scope registered as a stable unit identifier.
- Add a property-level uniqueness test using a real-shaped multi-building
  fixture where the same visible apartment number occurs in multiple native
  IDs.

## 3. Entrata unit cards discard the building/address disambiguator

### Verdict

Confirmed on the current public source and current branch. Entrata's unit-card
template can publish the same apartment number in multiple buildings. The
fourth visible item in `.unit-specs` distinguishes the physical apartments,
and every card also has a distinct native `entrata_uid`, but the parser keeps
only the repeated apartment number. Depending on mutable rent differences,
the production post-processor either drops a real apartment as an "exact"
duplicate or ships colliding `unit_id` values.

### Four-property live reproduction

The pages below were rendered in clean, isolated test sessions and then passed
through the current Entrata parser, production Jugnu formatter, and per-property
post-processor.

| Property / current plan page | Current source distinction | Parser output | Final output |
|---|---|---|---|
| Phoenix Orlando (`19299`), [The Dahlia](https://www.livephoenixorlando.com/floorplans/orlando-FL/the-phoenix-orlando/the-dahlia-545556-1/) | two `207` cards, visible tokens `48` and `60`, native IDs `4270134` and `4270158` | `207`, blank building on both | source 6 rows becomes 5; one real `207` is dropped |
| Abberly Grove (`34482`), [Hatteras With Sunroom](https://www.abberlygrove.com/floorplans/raleigh-NC/abberly-grove/hatteras-with-sunroom-23044-1/) | `201` in `07` and `06`; `202` in `09` and `10`; four distinct native IDs | repeated `201` and `202`, blank building | all 4 survive because rent differs, but final IDs are `201, 202, 201, 202` |
| Seasons at Mount Pleasant (`257328`), [1B](https://www.seasonsmtpleasant.com/floorplans/mount-pleasant-WI/seasons-at-mount-pleasant/1b-1167440-1/) | two `106` cards, visible tokens `Q 4441` and `D 4320`, native IDs `5092046` and `5072910` | `106`, blank building on both | both survive with duplicate final `unit_id=106` |
| Wedgewood on the Green (`36173`), [0B](https://wg.barringtonresidential.com/henrietta/wedgewood-on-the-green-apartments/floorplans/0b-1158500/fp_name/occupancy_type/conventional/) | three unit `1` rows in buildings `73`, `154`, and `134` | repeated `1`, but all three buildings are preserved | correctly rewritten to `73-1`, `154-1`, `134-1` |

The Wedgewood row is the useful control: when the template labels the building
in the shape the parser already recognizes, the existing Jugnu building
disambiguator works. The defect is concentrated in the unit-card shape whose
fourth `.spec-item` is visibly present but unlabeled in the rendered DOM.

This also explains the seven affected properties in the July 31 output. Every
collision group had distinct `entrata_uid` values, but final identity used the
repeated visible number. The four current probes establish the mechanism; they
do not assume that the July roster is still unchanged.

### Reachable code path

- `ma_poc/pms/adapters/entrata.py:2036-2060` reads each `.unit-card`, its
  visible apartment number, native UID, and flattened text.
- `ma_poc/pms/adapters/entrata.py:2062-2079` consumes the first three visible
  spec values (beds, baths, and area), but does not consume the fourth
  building/address value.
- `ma_poc/pms/adapters/entrata.py:2122-2125` recognizes a building only when
  the flattened text literally contains the word `building`; the live
  unit-card tokens are unlabeled values such as `48`, `09`, and `Q 4441`.
- `ma_poc/pms/adapters/entrata.py:2127-2147` preserves `entrata_uid` only as
  provenance and emits the shorter visible apartment number as identity.
- `ma_poc/scripts/runners/jugnu.py:3326-3327` consequently selects that
  repeated number as `unit_id`.
- `ma_poc/scripts/runners/jugnu.py:2935-2969` can disambiguate only when every
  row has a non-empty parsed building. `ma_poc/scripts/runners/jugnu.py:3173`
  then applies an exact-dedup fingerprint that omits availability date; this
  is why the two Phoenix `207` rows collapse when their other normalized
  fields match.

### Required behavior

- Parse and retain the fourth unit-card `.spec-item` as the public
  building/address disambiguator after validating the exact live shape on a
  three-property fixture set.
- Keep the visible apartment number as display metadata, but form a unique
  physical identity from the disambiguator plus apartment number when the
  number collides within a property.
- Preserve `entrata_uid` as provenance and as a collision-safety anchor; do
  not silently drop a row merely because mutable fields happen to match.
- Add end-to-end fixtures for both outcomes seen live: identical normalized
  rows that currently collapse and different-rent rows that currently survive
  with duplicate IDs.

## 4. UDR July collision candidate is cleared on the current branch

### Verdict

Verified negative. The July output contained UDR number collisions, but the
current branch already distinguishes UDR's page-sequence prefix from a real
building prefix and composes the latter into the public unit identity.

Current direct probes produced:

| Property | Source rows | Native IDs | Distinct final unit IDs | Duplicate final IDs |
|---|---:|---:|---:|---:|
| Newport Village I (`2958`) | 69 | 69 | 69 | 0 |
| Cambridge Woods (`8179`) | 11 | 11 | 11 | 0 |
| Commons at Windsor Gardens (`30751`) | 60 | 60 | 60 | 0 |
| **Total** | **140** | **140** | **140** | **0** |

Examples now retain building-qualified identities such as `TH-4702`,
`47-09102`, `27B-101`, `39B-101`, `7-105`, and `19-105`. The implementation is
`ma_poc/pms/adapters/_udr.py:289-327` and `:393-409`, introduced in this
branch's consolidation commit `4e7d1aa`. The focused UDR suite passes 52/52.
No additional UDR identity defect is claimed from the July CSV.

## 5. Knock July collision candidate is cleared on the current branch

### Verdict

Verified negative on the current branch. Knock's public roster deliberately
reuses short visible apartment labels, but each physical apartment has a
distinct UUID. Consolidation commit `4e7d1aa` now preserves that UUID as both
`source_ids.knock_unit_id` and the canonical `unit_id`.

Four current marketing-page-to-Doorway-API-to-Jugnu reproductions produced:

| Property | Eligible source rows | Distinct visible numbers | Extra rows colliding on visible number | Distinct native UUIDs | Distinct final IDs |
|---|---:|---:|---:|---:|---:|
| Madison at Largo (`1783`) | 34 | 12 | 22 | 34 | 34 |
| Royal Park (`221319`) | 30 | 13 | 17 | 30 | 30 |
| Mosby Citrus Ridge (`281928`) | 75 | 44 | 31 | 75 | 75 |
| Sun Lake Apartments (`2305`) | 49 | 34 | 15 | 49 | 49 |
| **Total** | **188** | **103** | **85** | **188** | **188** |

The current public metadata also matched the configured property name and
address in all four probes. `ma_poc/pms/adapters/knock.py:344-352` captures the
UUID and `:371-390` emits it as canonical identity. The focused Knock and
source-ID suites pass 67/67. The repeated visible labels remain useful display
metadata; they no longer collide in final output.

## 6. AppFolio rejects Wisconsin grid addresses as identity

### Verdict

Confirmed on one current 5k property. The public AppFolio listing page gives a
different complete street address for every Jade at North Hills apartment, but
the address identity helper accepts only addresses beginning with a decimal
house number. Wisconsin grid addresses beginning `N72W...` therefore lose the
stable address identity and fall back to the short apartment suffix.

Current property-scoped source:
<https://harmoniq.appfolio.com/listings?filters%5Bproperty_list%5D=Prop%20Group%20Jade%20at%20North%20Hills>

The current source has 20 rows and four repeated suffix groups, each separated
by its public building address:

| Suffix | Distinct current addresses | Native listing IDs |
|---|---|---|
| `107` | `N72W12759... Unit 107`; `N72W12823... Unit 107` | `1155`, `3191` |
| `305` | `N72W12801... Unit 305`; `N72W12759... Unit 305` | `1430`, `3319` |
| `208` | `N72W12727... Unit 208`; `N72W12801... Unit 208` | `3335`, `3243` |
| `212` | `N72W12823... Unit 212`; `N72W12801... Unit 212` | `3224`, `303` |

The current parser emits all 20 with `unit_id=null` and only the short suffix
as `unit_number`. After the production formatter and post-processor:

- 20 source apartments become 18 final rows;
- one `107` and one `212` apartment are dropped as false exact duplicates;
- both `305` rows and both `208` rows survive but collide on their final IDs.

This is stronger than the July symptom, which exposed only the `305` and `208`
surviving collisions. It also demonstrates why duplicate-free output alone is
not sufficient: the other two collision groups were made "clean" by losing a
real apartment.

### Reachable code path

- `ma_poc/pms/adapters/appfolio.py:1265-1272` correctly reads the complete
  listing address and separates the display suffix.
- `ma_poc/pms/adapters/appfolio.py:1334-1343` intends to use the complete
  address as identity, but only when `address_unit_id` accepts it.
- `ma_poc/pms/adapters/_parsing.py:1042` requires a decimal digit at the start
  of an address. `ma_poc/pms/adapters/_parsing.py:1061-1074` therefore rejects
  valid Wisconsin grid addresses beginning with `N72W...`.
- `ma_poc/pms/adapters/_parsing.py:1105-1115` returns no address identity, so
  the production formatter falls back to the repeated apartment suffix.

### Required behavior

- Recognize the bounded Wisconsin grid-address form (for example
  `N72W12759 Good Hope Rd`) without loosening address detection to plan names.
- Continue using the complete public address, including unit suffix, as the
  stable display-backed identity.
- Add a complete AppFolio-to-Jugnu fixture containing all four Jade collision
  groups and assert 20 source rows, 20 final rows, and 20 unique final IDs.

Scope is deliberately limited to Jade at North Hills. This probe proves the
reachable format defect; it does not claim an AppFolio-wide collision rate.

## 7. Venterra keeps the unique unit code only as provenance

### Verdict

Confirmed on The Metropolitan. Venterra's source `unit_code` is building
qualified and unique, while `unit_name` is only the short apartment number.
The adapter preserves the former in `source_ids` but emits only the latter as
identity. Existing floor-plan disambiguation happens to fix different-plan
collisions, but it cannot distinguish the same apartment number in two
buildings when both rows share a plan.

Four current direct probes:

| Property | Source rows | Repeated visible-number extras | Unique native codes | Duplicate final IDs |
|---|---:|---:|---:|---:|
| The Metropolitan (`48177`) | 14 | 2 | 14 | 1 |
| Forest View (`30237`) | 20 | 0 | 20 | 0 |
| Canton Mill Lofts (`14524`) | 14 | 2 | 14 | 0 |
| The Parker (`33327`) | 18 | 0 | 18 | 0 |

At The Metropolitan, the current source has three visible `202` rows:

- `KY4MP-2602-202`, plan `1343-B1`;
- `KY4MP-2616-202`, plan `1343-A1`; and
- `KY4MP-2634-202`, plan `1343-B1`.

The A1 row is separated by the existing floor-plan rule. The two B1 rows are
distinct building-qualified source apartments but both finish as
`be1201c1-202`. This matches the two native codes seen in the July collision
and reproduces on the current source.

`ma_poc/pms/adapters/venterra.py:121-136` captures `venterra_unit_code` in
`source_ids` but passes only `unit_name` as `unit_number`; no explicit native
`unit_id` or parsed building is emitted. Required behavior is to retain the
short name for display while using the complete Venterra code, or a verified
building-plus-number projection of it, as physical identity. Add a fixture
with same-number, same-plan apartments in two buildings. The focused Venterra
suite currently passes 16/16 but does not cover this shape.

## 8. Cortland drops both building and native apartment identity

### Verdict

Confirmed across both current Cortland source templates. The modern card page
visibly publishes a building number and multiple native identifiers; the
legacy preload uses a unique `availprice` map key. Both parsers discard those
fields and emit only the short apartment number.

Three current properties establish the scope and control:

| Property | Source rows | Distinct short numbers | Final rows | Distinct final IDs | Result |
|---|---:|---:|---:|---:|---|
| Cortland Mirror Lake (`3181`, modern cards) | 39 | 27 | 38 | 35 | one real row dropped; 3 duplicate IDs remain |
| Cortland Royal Palm Beach (`255134`, legacy preload) | 28 | 16 | 27 | 26 | one real row dropped; 1 duplicate ID remains |
| Cortland Brier Creek (`34500`, modern cards) | 9 | 9 | 9 | 9 | current clean control |

Mirror Lake's visible cards include values such as `Building 14 | Floor 2`
and the apartment anchor includes all of:

- `data-apartment-id=215991314`;
- `data-unit-id=904971`;
- `data-event-extra` with `building_number=14` and
  `apartment_number=211`; and
- a building-qualified detail path ending `/6211/`.

The same short numbers recur in different buildings. Current `304` appears in
buildings 16, 18, and 30; current `200` appears in buildings 22 and 31. The
parser emits blank building and no native source IDs. Existing floor-plan
disambiguation resolves some different-plan groups, but same-plan rows still
collide or disappear.

Royal Palm provides an independent legacy-shape reproduction. Its 28
apartments live under 28 unique `availprice` keys. Two current `305` rows use
native keys `215994910` and `215994941`, but the parser discards the map keys;
the post-processor drops one because all remaining fingerprint fields match.

### Reachable code path

- `ma_poc/pms/adapters/cortland.py:156-164` reads only the short `Apt #...`
  text from a modern card.
- `ma_poc/pms/adapters/cortland.py:195-197` reads floor but never reads the
  adjacent visible building.
- `ma_poc/pms/adapters/cortland.py:249-266` emits neither building nor the
  card's native data attributes in `source_ids` or `unit_id`.
- `ma_poc/pms/adapters/cortland.py:305-333` iterates
  `availprice.values()`, throwing away each unique legacy map key.

Required behavior is to preserve the current native apartment ID and building,
keep the short apartment number as display metadata, and cover both templates
with complete source-to-Jugnu collision fixtures. The focused Cortland suite
passes 10/10 but contains neither current identity shape.

## 9. Cortland emits fee-inclusive price as base rent

### Verdict

Confirmed across four current properties and 113/113 modern cards. Cortland
now displays both a fee-inclusive `Starting at` value and a lower `Base Rent`.
The adapter's older regex always selects `Starting at`, inflating the rent
field by the property's mandatory fee bundle.

| Property | Cards with both values | Cards where values differ | Per-card overstatement |
|---|---:|---:|---:|
| Cortland Mirror Lake (`3181`) | 39 | 39 | $77 |
| Cortland Brier Creek (`34500`) | 9 | 9 | $145 |
| Cortland on Pike (`2982`) | 58 | 58 | $115 |
| Cortland Alameda Station (`62782`) | 7 | 7 | $15 |
| **Total** | **113** | **113** | **$15-$145** |

For example, a current Mirror Lake card says `Starting at $1,342 incl. fees`
and `Base Rent $1,265`; current parser output is `$1,342`. A current Brier
Creek card says `$1,791` versus base `$1,646`; parser output is `$1,791`.

`ma_poc/pms/adapters/cortland.py:186-193` recognizes only the fee-inclusive
label and passes it through as both rent bounds at `:258-260`. Required
behavior is to prefer the explicitly labeled base-rent node/value and preserve
the fee-inclusive total separately if the output contract needs it. Add a
fixture with both labels and assert that the base value wins.

## 10. G5 substitutes the floor-plan type for apartment identity

### Verdict

Confirmed across three current properties. G5's GraphQL response gives every
apartment both a unique native `id` and a unique visible `displayName`, while
the `name` field contains a repeated floor-plan type such as `1X1` or `2X2`.
The adapter asks for all three fields but selects `name` first, discards both
real identity fields, and publishes the plan type as `unit_id`.

Current marketing-page-to-GraphQL-to-Jugnu reproductions:

| Property | Source apartments | Unique native IDs | Unique visible labels | Final rows | Unique final IDs |
|---|---:|---:|---:|---:|---:|
| [Shadowbrook](https://www.shadowbrookapartments.com/) (`3785`) | 18 | 18 | 18 | 13 | 3 |
| [Hawthorn Village](https://www.hawthornvillageapts.com/) (`35934`) | 12 | 12 | 12 | 12 | 3 |
| [Brookside Village](https://www.brooksidevillageapts.com/) (`33267`) | 13 | 13 | 13 | 13 | 2 |
| **Total** | **43** | **43** | **43** | **38** | — |

Examples from the current source make the field semantics explicit:

- Shadowbrook source rows include native `1495776951`, `name=1X1`, and
  `displayName=M308`, plus native `1495776957`, `name=1X1`, and
  `displayName=M205`. The parser emits `unit_number=1X1` for both.
- Hawthorn Village currently has visible apartments `162`, `052`, `217`,
  `106`, `100`, and `202` under `name=1X1`; all six finish with
  `unit_id=1X1`.
- Brookside Village has visible apartments such as `C102`, `G202`, `L102`,
  and `H101`, each with a distinct native ID, but 12 of its 13 output rows
  finish with `unit_id=2X2`.

The post-processor makes the symptom look smaller at Shadowbrook by deleting
five physical apartments whose mutable normalized fields happen to match.
The 13 survivors still contain six `1X1`, five `2X1`, and two `2X2` rows with
colliding final IDs. Hawthorn and Brookside retain all source rows only because
other fields differ; their identities remain non-unique.

### Reachable code path

- `ma_poc/pms/adapters/g5.py:79-94` explicitly queries `id`, `name`, and
  `displayName` for every apartment.
- `ma_poc/pms/adapters/g5.py:282` selects `name` before `displayName`, even
  though the current source uses `name` for the repeated plan type.
- `ma_poc/pms/adapters/g5.py:285-301` emits no native G5 ID in `unit_id` or
  `source_ids`.
- the production formatter therefore aliases the repeated plan type to
  `unit_id`; exact-dedup can then delete a real apartment when its other
  normalized fields match.

Required behavior is to preserve native G5 `id` as the stable physical
identity, retain `displayName` as the public apartment label, and keep `name`
only as a plan-type fallback when the nested floor-plan name is absent. Add a
complete three-property-shaped fixture that asserts source count preservation
and unique final identities.

## 11. Harbor Group future dates are lost at the Jugnu boundary

### Verdict

Confirmed on the current production winner for Riverworks. The Harbor Group
parser reads and normalizes explicit public availability dates correctly, but
emits them under field names the production formatter does not recognize.
Every dated apartment therefore receives the scrape date in final output.

Three complete current detection-to-adapter-to-Jugnu reproductions isolate the
affected route and provide both controls:

| Property | Current winning route | Source/adapter rows | Explicit future dates entering Jugnu | Future dates preserved in final output |
|---|---|---:|---:|---:|
| [Riverworks](https://www.harborgroupmanagement.com/apartments/pa/phoenixville/riverworks) (`67524`) | empty Knock -> generic Harbor `TIER_3_DOM` | 28 | 20 | 0 |
| [Waterford Village](https://www.harborgroupmanagement.com/apartments/MA/bridgewater/Waterford-Village) (`30734`) | empty Knock -> generic Harbor `TIER_3_DOM` | 20 | 0 | 0 (clean `Available Now` control) |
| [Triangle Place](https://www.harborgroupmanagement.com/apartments/nc/durham/triangle-place/) (`4944`) | Knock `TIER_1_KNOCK_API` | 17 | 6 | 6 (independent formatter control) |

Riverworks' current public unit pages include, for example:

- unit `01-5112`: `Available September 30, 2026`;
- unit `01-1414`: `Available September 9, 2026`;
- unit `01-6108`: `Available August 24, 2026`; and
- unit `01-1108`: `Available October 7, 2026`.

The Harbor parser correctly turns those into `2026-09-30`, `2026-09-09`,
`2026-08-24`, and `2026-10-07`. The complete current production path emits
all 28 Riverworks apartments, but final output gives every one
`available_date=2026-08-01`.

Triangle proves that neither the source family nor the canonical formatter is
universally broken. Its current Knock winner has six future dates and all six
survive unchanged. Calling the Harbor parser directly on Triangle's parallel
public unit pages also exposes 13 explicit dates, including three future
dates; those would be lost if the fallback became the winner, but that is not
claimed as Triangle's current production result.

### Reachable code path

- current detector evidence selects Knock on all three marketing pages.
  Exact `KnockAdapter.extract` returns zero for Riverworks and Waterford, so
  the normal PMS-to-generic fallback runs the reachable Harbor sub-tier;
  Triangle's exact Knock adapter returns 17 rows and wins before that fallback.
- `ma_poc/pms/adapters/_harbor_group.py:282-283` emits the visible text as
  `available_date_raw` and its normalized ISO value as
  `available_date_post_fix`.
- `ma_poc/core/schema_v2.py:582-600` defines the formatter's accepted aliases,
  but includes neither Harbor field. The Jugnu formatter consequently sees no
  explicit date and applies its normal available-now capture-date fallback.

Required behavior is to emit the normalized value under the canonical
`available_date` key (while retaining the visible text as provenance), or add
the Harbor normalized alias to the shared resolver. Add an end-to-end
Riverworks-shaped fixture that reaches `_format_v2_unit` and asserts every
explicit future date survives unchanged; keep a Waterford-shaped
`Available Now` fixture that still resolves to capture date.

## 12. OneSite adopts a sibling Mark-Taylor property's apartments

### Verdict

Confirmed current cross-property contamination on three of three live
Mark-Taylor properties. Their shared `PRELOADED_STATE` contains a portfolio
array named `simplifiedProperties`. One sibling entry publishes San
Norterra's Online Leasing link. The detector scans the whole document, the
OneSite workflow probe treats that one sibling link as property-scoped, and
all three configured properties consequently ship San Norterra's apartments.

Complete current production-path reproductions:

| Configured property | Current winner | Final apartment rows | Adopted OneSite SiteId | Adopted portal |
|---|---|---:|---|---|
| [Mira Santi](https://www.mark-taylor.com/apartments/az/chandler/mira-santi/floor-plans/) (`14538`) | `TIER_1_API_ONESITE_WORKFLOW` | 14 | `5199527` | `9026050.onlineleasing.realpage.com` |
| [San Cervantes](https://www.mark-taylor.com/apartments/az/chandler/san-cervantes/floor-plans/) (`16078`) | `TIER_1_API_ONESITE_WORKFLOW` | 14 | `5199527` | `9026050.onlineleasing.realpage.com` |
| [Waterside at Ocotillo](https://www.mark-taylor.com/apartments/az/chandler/waterside-at-ocotillo/floor-plans/) (`14155`) | `TIER_1_API_ONESITE_WORKFLOW` | 14 | `5199527` | `9026050.onlineleasing.realpage.com` |

The three final rosters are byte-for-byte identical across unit number, plan,+rent, area, source SiteId, and portal. Their common unit numbers are `23`,
`60`, `106`, `107`, `154`, `174`, `199`, `215`, `285`, `287`, `309`, `312`,
`333`, and `365`.

The source itself gives decisive identity evidence:

- each current page's primary `sitePage.property` identifies its configured
  Chandler property;
- the selected portal appears instead at
  `sitePage.simplifiedProperties[33].yardi_apply_now_link`;
- that sibling object explicitly says `name=San Norterra`, links to
  `/apartments/az/phoenix/san-norterra`, and carries its Phoenix address; and
- fetching the selected portal shell returns visible identity
  `San Norterra, 28515 N. North Valley Pkwy, Phoenix, AZ 85085` plus
  `widgetLoader.js?siteId=5199527`.

The workflow response initially contains 19 San Norterra plan/apartment rows;
normal post-processing admits its 14 numbered apartments. That is why the
result looks like a clean unit-level success rather than a link-hop error.

### Reachable code path

- `ma_poc/pms/detector.py:831-852` promotes any OneSite portal marker anywhere
  in the HTML; it does not distinguish the primary property object from a
  sibling portfolio entry.
- `ma_poc/pms/adapters/onesite.py:545-563` unescapes the entire document and
  accepts the sole `onlineleasing.realpage.com` host. The page contains other
  sibling SecureCafe links, but only one OneSite host, so the existing
  `len(sub_hosts) == 1` portfolio guard passes.
- `ma_poc/pms/adapters/onesite.py:756-763` parses the workflow and records
  SiteId/portal provenance without comparing the portal's visible property
  identity to `AdapterContext`.
- `ma_poc/pms/adapters/onesite.py:1365-1395` admits and returns those rows as a
  high-confidence unit-level winner. The strict identity helper at
  `:778-814` protects a different CWS fallback only; it is not applied to the
  workflow path.

Required behavior is to fail closed before adopting a published OneSite
portal: its visible name/address or an authoritative property-details response
must agree with the configured property. Link discovery should additionally
exclude URLs located only inside sibling-community or portfolio recommendation
collections. Add a real-shaped Mark-Taylor fixture with a primary property and
multiple `simplifiedProperties` siblings, then assert that no sibling portal
can become the winner.

## 13. SecureCafe legacy rows discard native `UnitID`

### Verdict

Confirmed current provenance and identity defect across four of five live
legacy `availableunits.aspx` pages. Every affected apartment publishes a
distinct native `UnitID` beside its `FloorPlanID`, but the parser preserves
only the plan identifier and uses the shorter visible apartment label as its
sole physical identity.

Current isolated live probes (plain HTTP returned Cloudflare 403; clean
Hyperbrowser used proxy on, stealth off, CAPTCHA solving off):

| Property | Current rows | Rows with native `UnitID` | Distinct native IDs | Native IDs preserved by parser | Distinct final display-based IDs |
|---|---:|---:|---:|---:|---:|
| Big Creek Apartments (`43097`) | 34 | 34 | 34 | 0 | 34 |
| Twin Oaks (`54743`) | 10 | 10 | 10 | 0 | 10 |
| Legacy Fort Mill (`239318`) | 26 | 26 | 26 | 0 | 26 |
| The Paramount (`60386`) | 17 | 17 | 17 | 0 | 17 |
| Woodman Apartment Homes (`232538`) | 4 | 0 | 0 | 0 | 4 |
| **Total** | **91** | **87** | **87** | **0** | **91** |

Representative current source-to-parser pairs:

- Big Creek native `UnitID=45003788`, visible `3A_0302`, preserved source ID
  only `securecafe_floorplan_id=5947374`;
- Twin Oaks native `10730311`, visible `3800_104`, preserved plan ID
  `2293926`;
- Legacy Fort Mill native `45802847`, visible `7_0303`, preserved plan ID
  `5952231`; and
- The Paramount native `3637304`, visible `3N16-ADA`, preserved plan ID
  `1055235`.

All visible labels are currently unique in this five-property sample, so no
current duplicate or dropped-row claim is made. The defect is that identity
stability depends unnecessarily on a display label even when the vendor gives
an explicit stable apartment key; a future label reuse or rename cannot be
reconciled reliably.

### Reachable code path

- `ma_poc/pms/adapters/rentcafe.py:2599-2683` is the production parser for
  the current legacy pages.
- `:2657-2665` explicitly extracts `FloorPlanID` into `source_ids`, but has no
  companion extraction for the adjacent `UnitID` carried by the same apply
  action.
- `:2672` emits only the visible apartment text as `unit_number`; the Jugnu
  formatter consequently uses that display value as canonical identity.

Required behavior is to preserve `UnitID` as a registered stable source ID
and canonical physical `unit_id`, while retaining the apartment label for
display. Keep the Woodman shape as a fallback control: when no native key is
published, the existing visible identity remains valid. Add a mixed fixture
covering input-button and anchor-link `UnitID` encodings plus the no-ID
fallback.

## 14. Generic API fallback deduplicates apartments before using building identity

### Verdict

Confirmed reachable fallback defect with a current real-data reproduction,
but **not** a defect in the normal Spherexx winner. Both generic API parsers
extract a building value and then omit it from their pre-normalization dedup
key. Real apartments sharing a display number across buildings are therefore
dropped before the common identity layer can disambiguate them.

Three current Spherexx legacy availability feeds provide the live scope and
two controls:

| Property | Current source rows | Native source IDs | Visible apartment labels | Generic-parser rows | Dedicated Spherexx-parser rows |
|---|---:|---:|---:|---:|---:|
| Coventry Square (`70255`) | 8 | 8 | 5 | 5 | 8 |
| Fairmount Towers (`259386`) | 2 | 2 | 2 | 2 | 2 |
| Irondale at Wharton (`281149`) | 6 | 6 | 6 | 6 | 6 |

Coventry currently publishes three repeated-label pairs, each separated by
building and native apartment ID:

- `2C`: building `044`, native `522319`; building `032`, native `556140`;
- `1C`: building `034`, native `549100`; building `042`, native `550516`; and
- `1B`: building `034`, native `564163`; building `042`, native `564175`.

The current HTML feeds were first parsed by the production
`parse_spherexx_legacy_availability` route. The same eight real Coventry rows
were then transformed only into aliases accepted by the generic parsers
(`floorPlanName`, `unitNumber`, `building`, rent, beds, baths, area, and date).
Both `parse_generic_api` and `parse_api_responses` deterministically retained
only the first member of each repeated display number: 8 source rows became 5.
The two control feeds remained 2/2 and 6/6.

### Reachable code path

- `ma_poc/pms/adapters/generic.py:1005-1006` creates a function-local `seen`
  set; `:1140-1144` keys it by `unit_num` alone whenever a display apartment
  number exists, even though the function has already extracted `building`.
- `ma_poc/pms/adapters/_api_parser.py:1620-1622` extracts `building`, but
  `:1639-1642` again keys `seen` by `unit_num` alone.
- The normal published Spherexx link-hop is safe today:
  `ma_poc/pms/adapters/_pms_portal_hop.py:731-736` dispatches to
  `parse_spherexx_legacy_availability`, whose native-ID path preserves all
  eight Coventry apartments. No current Spherexx production-winner row-loss
  claim is made.

Required behavior is to include a stable native source ID when present and,
otherwise, the normalized building plus display apartment label in any early
dedup key. Preferably, remove this lossy early dedup and let the common identity
postprocessor decide after all disambiguators are present. Add the eight-row
Coventry shape as a real-data fixture and assert 8/8 rows with eight unique
canonical IDs, plus the two unaffected controls.

## 15. The formatter assigns today's date to explicitly unavailable apartments

### Verdict

Confirmed fleet-wide write-boundary defect. In the completed consolidated
4,982-property canary, **8,087 rows** are simultaneously
`availability_status=UNAVAILABLE` and
`availability_date_provenance=capture_date_default`. Their emitted
`available_date` is the scrape date even though the source/parser explicitly
says the apartment is unavailable.

The largest current cluster is the Razz/ResMan full-roster surface: **7,825
rows across 44 properties**. The source publishes rent for both available and
unavailable physical apartments, so rent is not proof of present availability.
Four direct, current `/models` captures reproduce the source-to-output error:

| Property | Source apartments | Source `available=false` | Source dated/available | False rows after generic parser | False rows after production formatter |
|---|---:|---:|---:|---:|---:|
| Village of Cross Creek (`37143`) | 234 | 213 | 21 | 213 `UNAVAILABLE`, no date | 213 `UNAVAILABLE`, dated 2026-08-02 |
| Village at Crown Woods (`56166`) | 389 | 362 | 27 | 362 `UNAVAILABLE`, no date | 362 `UNAVAILABLE`, dated 2026-08-02 |
| Milano (`14581`) | 318 | 279 | 39 | 279 `UNAVAILABLE`, no date | 279 `UNAVAILABLE`, dated 2026-08-02 |
| Alleia Long Meadow Farms (`258143`) | 399 | 370 | 29 | 370 `UNAVAILABLE`, no date | 370 `UNAVAILABLE`, dated 2026-08-02 |

The operator's current public availability portal is a useful independent
control. Village of Cross Creek currently renders 13 dated available
apartments, while its marketing page's `$inventory.units` contains the wider
234-apartment physical roster. A representative full-roster record is
`C7532A`: `available=false`, rent `$1,100`, and no date. Passing that exact
live record through `parse_api_responses` produces `UNAVAILABLE` with no raw
date; passing the parsed row through the production Jugnu formatter changes it
to `UNAVAILABLE` with `available_date=2026-08-02` and provenance
`capture_date_default`.

The remaining consolidated-canary contradictions are 255 AppFolio full-roster
rows plus seven generic API rows. They are the same formatter behavior, not a
claim that those adapters independently misread their source.

### Reachable code path

- `ma_poc/pms/adapters/_api_parser.py:1535-1539` correctly interprets the Razz
  `available` value and emits `UNAVAILABLE`; `:1678-1682` also carries the
  absence of a source date correctly.
- `ma_poc/scripts/runners/jugnu.py:3572-3578` sends the row's explicit status
  and `has_rent=True` into the shared resolver.
- `ma_poc/core/schema_v2.py:1833-1838` returns the scrape date for **any**
  rent-bearing row after checking only whether a parsed date exists. It does
  not stop when status is `UNAVAILABLE`, `LEASED`, `PENDING`, or a waitlist
  state. The canonical and production formatters both invoke this helper, so
  the defect is shared rather than formatter drift.

Required behavior is: preserve every parseable explicit date; default to the
capture date for explicit `AVAILABLE`; permit the rent-only fallback only when
status is absent or non-negative/unknown; and never manufacture a date when an
explicit negative status has no date. Add the four real-shaped Razz records,
plus `LEASED`, `PENDING`, and waitlist regressions, against both formatter
entry points.

## 16. UDR building-safe identity disconnects every source availability date

### Verdict

Confirmed current regression across the complete 20-property UDR canary
cohort. The parser's building-aware unit identity is correct, but its adjacent
date lookup still uses the old bare apartment label. Twelve properties now
retain **zero** of their visible dates even though the same page carries a
lossless native-ID join.

All 20 current public pricing pages were fetched directly on August 2; no
proxy, CAPTCHA solver, or browser evasion was used:

| Measure | Current result |
|---|---:|
| UDR properties successfully live-probed | 20 / 20 |
| Current JSON-LD apartments | 600 |
| Rows dated by the current parser | 146 |
| Rows whose date is lost by current lookup | 454 |
| Rows joinable by native apartment ID | 600 / 600 |
| Source labels saying `Now` | 100 |
| Source labels carrying an explicit date | 500 |

The twelve zero-date properties are 1274 at Towson, Vitruvian West, Garrison
Square, Arbor Park of Alexandria, Newport Village, Commons at Windsor Gardens,
Highlands of Marin, The Courts at Huntington Station, Savoye, Slade at
Channelside, Cambridge Woods, and HQ @ 1532 Harrison. The other eight are
controls: their JSON-LD identity happens not to require a building prefix, so
the old display-label lookup still succeeds.

Four representative exact reproductions:

| Property | Current rows | Dates retained now | Native-ID date matches |
|---|---:|---:|---:|
| Vitruvian West | 90 | 0 | 90 |
| Arbor Park of Alexandria | 92 | 0 | 92 |
| Newport Village | 69 | 0 | 69 |
| Commons at Windsor Gardens | 59 | 0 | 59 |

For Commons, JSON-LD identifies apartment `7-105` and publishes internal
`unitid=13670653`. The adjacent UDR view model calls it marketing unit `105`,
but also publishes `apartmentId=13670653` and `AvailableDateLabel=9/19/2026`.
The current code looks for `7-105` in a map keyed only by `105`, misses, and the
formatter replaces the missing date with the scrape date. The same native-ID
equality holds for all 600 current rows.

The RP comparison independently pointed to this cluster: 356 strict one-to-one
unit matches had an RP future date after August 2 while the consolidated canary
used `capture_date_default`. RP was used only to prioritize the probe; the
defect and full scope above come from current first-party UDR pages.

### Reachable code path

- `ma_poc/pms/adapters/_udr.py:186-230` builds the date map from only
  `marketingName`/`lookUpName`, even though the same object exposes
  `apartmentId` and `realpageunitid`.
- `:403-410` correctly constructs a building-qualified display identity such
  as `7-105`; `:428-436` separately extracts the stable JSON-LD `unitid`.
- `:507-512` nevertheless performs only the building-qualified display lookup,
  so the already-extracted native key is never used for the date join.

Required behavior is to index the view model by native apartment ID and by
display label, then resolve date by `internal_unitid` first. Display lookup may
remain only as a backward-compatible fallback when it is unambiguous. Fixtures
must cover the repeated-label building shape (`7-105` vs `19-105`), the
townhome prefix shape (`TH-4702`), and one no-prefix control; assert that the
existing canonical unit IDs do not change while every source date survives.

## 17. One G5 property publishes zero dimensions that contradict explicit plan codes

### Verdict

Confirmed current one-property data-normalization defect, with two clean
same-operator controls. This is not classified as a G5-wide failure.

The current public G5 GraphQL response for Eagle Rock Apartments at Woodbury
contains 40 apartments. Every joined floor plan says `beds=0` and `baths=0.0`,
but every row also provides an explicit non-studio plan or apartment code:

| Source plan name | Rows | Source apartment code | Correct dimension signal |
|---|---:|---|---|
| `1 Bed` | 23 | `1X1` | 1 bed / 1 bath |
| `A3-1x1D` | 7 | `1X1` | 1 bed / 1 bath |
| `2 Bed` | 5 | `2X2` | 2 beds / 2 baths |
| `B4-2x2D` | 5 | `2X2` | 2 beds / 2 baths |

The parser emits all 40 as zero-bedroom/zero-bathroom. The output normalizer
correctly treats zero baths as invalid and publishes null, but preserves zero
beds because zero is normally the valid Studio representation. These rows
therefore look like studios despite an unambiguous 1- or 2-bedroom source name.

Two current Eagle Rock controls disprove a portfolio-wide rule: Towson returns
10 apartments with correct dimensions, and West Hartford returns seven with
correct dimensions. The anomaly is isolated to Woodbury in the full canary
(38 surviving G5 rows after downstream identity collisions).

### Reachable code path

- `ma_poc/pms/adapters/g5.py:277-280` reads the raw zero values.
- `:283-294` emits them without checking for contradiction against
  `floorplan.name` or the apartment code.
- The shared bedroom normalizer must retain zero for genuine studios, so this
  cannot be fixed with a global `0 -> null` clamp.

Required behavior is a narrow contradiction repair: only when numeric zero is
present **and** an explicit plan/apartment token says `1x1`, `2x2`, `1 Bed`, or
an equivalent non-studio shape, prefer the explicit token and record the
correction provenance. Preserve zero for `Studio`, `S*`, and genuinely
ambiguous names. Add Woodbury's four source shapes plus the Towson and West
Hartford controls.

## 18. SecureCafe RP future-date candidate is not confirmed from the current source

### Verdict

Cleared as a parser-defect claim on the currently reachable sample. RP was
useful for selecting properties whose July SurgeX output used the capture date,
but four current, property-identified `availableunits.aspx` responses publish
no date in their canonical apartment rows and no `MoveInDate` in their action
links. Passing those exact responses through the production parser also emits
no source date. There is therefore no current source value that the parser can
be shown to drop.

Plain HTTP returned Cloudflare 403. Each live check used one isolated
Hyperbrowser session with residential proxy enabled, stealth disabled, and
CAPTCHA solving disabled:

| Property | Current source rows | Rows with visible source date | Rows with action-link `MoveInDate` | Parser rows with a date | Strict July RP-future / SurgeX-capture candidates |
|---|---:|---:|---:|---:|---:|
| Waterline Square (`261530`) | 44 | 0 | 0 | 0 | 16 |
| One Clinton Park (`250124`) | 18 | 0 | 0 | 0 | 12 |
| The Meyden (`60750`) | 20 | 0 | 0 | 0 | 6 |
| Promenade at Aventura (`5974`) | 11 | 0 | 0 | 0 | 7 |
| **Total** | **93** | **0** | **0** | **0** | **41** |

Each response visibly identified the configured property. The first four
columns come from the August 2 live response. The last column is only a
candidate count from the two user-provided July comparison files; it is not
treated as source truth. Saybrooke (`4904`) has nine additional strict July
candidates, but its current portal remained 403 even through the clean browser
session and is left unverified rather than generalized from.

This result does **not** prove that the operator has no private or separate
term-calculation endpoint carrying a future date. It establishes the narrower
claim needed by this audit: the current public apartment rows consumed by the
production SecureCafe parser contain no future date to preserve. Any further
work here is endpoint discovery, not a justified parser change. The existing
`capture_date_default` provenance should remain so consumers can distinguish
the inferred date from an explicit source date.

## 19. Lake Haven re-crawls one successful Jonah roster under two URL spellings

### Verdict

Confirmed one-property cross-page accumulation defect. This is not a claim
that Jonah itself publishes duplicate apartments.

The retained strict canary event stream shows two successful recoveries of the
same Lake Haven floor-plan surface:

| Canary event (UTC) | Requested surface | Recovered rows |
|---|---|---:|
| 2026-08-02 02:04:15 | `https://lakehavenluxury.com/floorplans` | 36 |
| 2026-08-02 02:08:40 | `http://www.lakehavenluxury.com/floorplans/` | 36 |

Both requests resolved to the same canonical host/path and both won through
`TIER_1_DOM_JONAH_SSR_UNITS`. The final property contains 48 rows but only 37
distinct `unit_id` values. All 11 collision extras repeat the same apartment,
plan, area, and availability date; only the mutable rent range differs. For
example, apartment `750-304` appears twice on `A Renovated`, 700 square feet,
available `2025-09-05`, at `$1,974-$5,768` and `$2,014-$5,789`.

A current direct replay of both URL spellings through `recover_jonah_ssr`
returned 36 rows and 36 unique identities from each. The current two snapshots
happened to agree on rent. That clean present-time control does not clear the
retained canary: it confirms that one invocation is a complete roster and that
the second successful invocation is redundant. The canary captured a normal
dynamic-price change between the two invocations and thereby exposed the
unsafe merge key.

### Reachable code path

- `ma_poc/pms/scraper.py:145-188` defines a normalized URL identity, but it is
  used only for tarpit handling; a successful equivalent URL is not suppressed.
- `ma_poc/pms/scraper.py:6662-6716` accumulates every successful floor-plan
  sub-result.
- `ma_poc/pms/scraper.py:6796-6810` deduplicates the accumulated rows by
  `(unit identity, plan, rent)`. Rent is mutable, so one apartment survives
  twice when the two requests observe different prices.

Required behavior is to stop crawling a second scheme/`www`/trailing-slash
variant after the normalized surface has already succeeded, and to merge any
unavoidable repeated source snapshots by immutable apartment identity before
considering rent. Add a retained Lake Haven replay that feeds two 36-row
snapshots with the canary price changes and asserts 37 physical identities,
not 48 rows.

## 20. Rose Park discards both native identity and street address

### Verdict

Confirmed current one-property RentManager/iLoveLeasing identity defect.

The current first-party Rose Park Commons page publishes five availability
rows. Each has a distinct `data-featherlight` native ID and the associated
detail modal publishes its street address:

| Native ID | Visible apartment | Street-qualified source identity | Rent / sqft / date |
|---:|---:|---|---|
| 2410 | 1 | 1614 W Eldridge Ave, #1 | $1,045 / 510 / 2026-09-01 |
| 2416 | 3 | 1615 W Eldridge Ave, #3 | $1,045 / 510 / 2026-10-01 |
| 4090 | 10 | 1634 W County Rd B, #10 | $1,010 / 605 / 2026-10-01 |
| 2484 | 1 | 2128 Fry St, #1 | $975 / 600 / 2026-09-01 |
| 2496 | 1 | 2136 Fry St, #1 | $975 / 600 / 2026-09-01 |

A current Playwright source-to-final replay produced five adapter rows but
only four final rows. The parser uses the native ID to deduplicate its own DOM
loop, then discards it and the modal address. The two Fry Street apartments
therefore become byte-identical short-ID rows and Jugnu's exact-fingerprint
pass deletes one. The retained canary has the same 5-to-4 transition.

### Reachable code path

- `ma_poc/pms/adapters/_iloveleasing_table.py:79-93` extracts the stable detail
  ID only into a local `seen` key.
- `:95-112` emits no `source_ids`, address, or building value.
- `ma_poc/scripts/runners/jugnu.py:3028-3043` consequently sees the two Fry
  Street apartments as exact duplicates and deletes the later row.

Required behavior is to preserve the native detail ID as the canonical
property-scoped identity and retain the modal street address as supporting
provenance. The five current source apartments must survive as five unique
final IDs.

## 21. Birch Pond drops an explicit building and two native unit IDs

### Verdict

Confirmed current one-property RentVision identity defect.

Birch Pond's current Brunswick detail page publishes three physical
apartments. Two use the short visible apartment number `2`, but the source
disambiguates them twice:

| Apartment | Building | Apply `UnitId` | SightMap ID | Rent | Available |
|---:|---:|---:|---:|---:|---|
| 2 | 32 | 90 | 10699909 | $1,469 | 2026-08-04 |
| 3 | 30 | 75 | 10699920 | $1,429 | 2026-08-08 |
| 2 | 12 | 18 | 10699900 | $1,420 | 2026-09-12 |

The production adapter's current live replay returns all six Birch Pond
apartments, but both short-number-2 rows have empty `building` and empty
`source_ids`. The final formatter therefore emits two `unit_id="2"` records;
their different rent and date prevent the exact-fingerprint pass from
collapsing them. The retained canary has the same two-row collision.

### Reachable code path

- `ma_poc/pms/adapters/rentvision.py:416-490` anchors on the apartment `<th>`
  and extracts rent/date, while ignoring the adjacent Building cell and the
  native Apply/SightMap IDs in the same row.
- `:769-800` concatenates the parsed detail rows without enriching their
  identity.

Required behavior is to make the source-native Apply `UnitId` canonical when
present, retain the visible number separately, and preserve Building as an
additional disambiguator/provenance field. A bounded fallback may use
`building-apartment` when no native ID exists. The current six source rows
must finish with six unique canonical IDs.

## 22. ResMan full-roster and availability sources are joined on mutable rent

### Verdict

Confirmed three-property cross-source identity defect.

The Razz/Vike marketing page publishes a property-scoped full rent roll in
`initialStoreState.$inventory.units`. Its public ResMan portal publishes an
overlapping available subset. Both are legitimate first-party sources, but
they are two views of the same physical apartments. The strict canary followed
both routes and accumulated them before identity reconciliation:

| Property | Canary full roster | ResMan physical rows | Same-rent overlap removed | Rent-different overlap retained | Final rows / unique IDs |
|---|---:|---:|---:|---:|---:|
| Village of Cross Creek (`37143`) | 234 | 13 | 8 | 5 | 239 / 234 |
| Brandon Place (`56151`) | 200 | 10 | 3 | 7 | 207 / 200 |
| Centennial Gardens (`243936`) | 713 | 17 | 3 | 14 | 727 / 713 |

The canary event streams independently record the two source counts for each
property (`generic:embedded_json` followed by a
`TIER_1_API_RESMAN` link-hop). Current live probes reproduce the overlap on all
three property-identified sources. Village and Brandon still publish 234/13
and 200/10 respectively. Centennial's mutable full roster has grown from 713
to 792, but its current portal still has 17 physical rows and the same
14-rent-different/3-same-rent split that explains the retained collision.

Example: Village apartment `H7580D` is one source ID in both feeds. The
marketing full roster currently publishes a `$1,254` range minimum while the
portal's selected 12-month price is `$1,282`; the final canary preserves both
rows under `unit_id="H7580D"`. Comparable current mismatches occur on 7 of 10
Brandon portal apartments and 14 of 17 Centennial portal apartments.

### Reachable code path

- The generic embedded tier emits the full roster and also queues the exact
  ResMan portal.
- `ma_poc/pms/scraper.py:6662-6716` accumulates the portal and later marketing
  route results.
- `ma_poc/pms/scraper.py:6796-6810` includes rent in the accumulation dedupe
  key, and `ma_poc/scripts/runners/jugnu.py:2916-3043` includes rent in the
  final exact fingerprint. Both therefore preserve a duplicate whenever two
  legitimate source views price the same apartment differently.

Required behavior is an immutable unit-ID merge. Preserve one physical row,
retain both source provenances, use the ResMan available subset for explicit
availability/date and selected-term rent semantics, and use the full roster
for catalogue/dimension coverage. Mutable rent/date/status differences must
be field-resolution inputs, never apartment-identity inputs.

## 23. SightMap's direct roster route clears three source-to-final controls

### Verdict

Cleared on the current direct/iframe SightMap API shape. This is a scoped clean
control, not a claim about every generic or browser fallback attributed to the
SightMap adapter.

Three current first-party SightMap responses were replayed through
`parse_sightmap_payload`, common post-processing, the production Jugnu
formatter, and final per-property deduplication:

| Property | Current source units | Parser drops | Final unit rows | Unique final IDs | Plan-presence rows |
|---|---:|---:|---:|---:|---:|
| Tisdale at Lakeline Station (`279758`) | 221 | 0 | 221 | 221 | 1 |
| Wildwood off Main (`32746`) | 188 | 0 | 188 | 188 | 0 |
| The Parker (`288891`) | 112 | 0 | 112 | 112 | 2 |
| **Total** | **521** | **0** | **521** | **521** | **3** |

All 521 source-native unit IDs survived in `source_ids`; none of the physical
rows lacked a final `unit_id`. Direct comparison to the live response found no
rent or availability-date mismatch. Each canary property also carried a
property-name identity match for the exact SightMap asset and the retained
canary counts agree with this current replay except where live inventory is
expected to move.

### Reachable code path

- `ma_poc/pms/adapters/sightmap.py:187-296` joins the unit roster to its floor
  plans and preserves both `sightmap_unit_id` and `sightmap_floor_plan_id`.
- `:298-369` emits at most one unavailable catalogue row for a plan name that
  has no physical unit; those three plan-presence rows remain explicitly
  separated from the 521 unit rows through final formatting.

The 608 date-missing SightMap-attributed rows in the fleet matrix remain a
source-discovery question, not evidence of a parser defect: the tested route
preserves every date the current source publishes. Fallback shapes must remain
outside this scoped certification until separately replayed.

## 24. Apts247 discards a stable native PMS identity on numbered apartments

### Verdict

Confirmed four-property identity/provenance defect, with clean row and value
fidelity on the same controls.

Every current same-origin Apts247 response publishes a distinct numeric `id`
for each physical apartment. The production parser uses that ID only when the
visible apartment `number` is blank; whenever a number is present, the native
ID disappears entirely from both canonical identity and `source_ids`:

| Configured property | Current branding / landing | Source apartments | Native IDs preserved | Final rows / unique IDs |
|---|---|---:|---:|---:|
| Broadway Palace (`64390`) | Broadway Palace | 68 | 0 | 68 / 68 |
| The Lake Lofts (`9168`) | Buena Onda White Rock | 61 | 61, only because all visible numbers are blank | 61 / 61 |
| The 1856 Apartments (`31564`) | Ranch at 1856 Apartments | 38 | 0 | 38 / 38 |
| The Maxwell (`68313`) | The Maxwell | 37 | 0 | 37 / 37 |
| **Total** |  | **204** | **61** | **204 / 204** |

All 204 source rows matched the parser exactly on plan, visible identity,
building, rent bounds, and source availability date. Thus this finding does
not allege current row or value loss. It establishes that 143 stable native
IDs are thrown away even though the endpoint supplies them.

Broadway Palace demonstrates why the two identities are not interchangeable:
native IDs `829515` and `767763` are both visibly apartment `523`. The current
formatter happens to rescue the collision as `North-523` and `South-523`
because Building is populated. That derived identity is valid today, but the
actual unit-producing response and its immutable IDs are absent from the
output, so a future missing/renamed building or changed display number cannot
be reconciled safely across runs.

### Reachable code path

- `ma_poc/pms/adapters/apts247.py:151-159` reads `u["id"]` only as a fallback
  when `u["number"]` is empty.
- `:160-180` emits the visible/fallback number but passes no `source_ids`, even
  though the function's own contract identifies `id` as the real stable PMS
  identity.

Required behavior is to preserve the numeric Apts247 ID on every physical row,
make it the property-scoped canonical identity, and retain the visible number
and building separately. Existing blank-number rows should keep their current
`apt-<id>` migration mapping. The four current controls must remain 204/204
with exact rent/date fidelity after the identity change.

## 25. Funnel Spaces discards its native unit, plan, and property IDs

### Verdict

Confirmed three-property identity/provenance defect on the dedicated Funnel
Spaces roster shape. The current row and value mapping itself is clean.

Each current first-party Spaces card publishes `data-spaces-unit-id` (also
mirrored in `data-spaces-id`), `data-spaces-plan-id`, a property asset ID, and
the property name. The parser retains only the display apartment number:

| Property | Source / final rows | Unique native unit IDs | Native IDs preserved | Property asset |
|---|---:|---:|---:|---:|
| Windsor Burnet (`119144`) | 30 / 30 | 30 | 0 | 267407 |
| Cirrus (`58969`) | 16 / 16 | 16 | 0 | 301977 |
| The Estates at Cougar Mountain (`26967`) | 9 / 9 | 9 | 0 | 269991 |
| **Total** | **55 / 55** | **55** | **0** |  |

All 55 current cards identify the configured community, and the current counts
exactly match the retained strict canary. Source-to-final replay preserved the
display number, plan, beds, baths, area, rent, and explicit availability date
on every row; all 55 final display IDs happen to be unique today. This finding
therefore does not claim present row loss. It proves that the immutable IDs and
the actual unit-producing property/plan relationship are discarded.

For example, Windsor Burnet's visible apartment `2217` has native unit ID
`5376446`, plan ID `271703`, property asset `267407`, and property name
`Windsor Burnet` in the same source tag. Output contains only `unit_id="2217"`
and empty `source_ids`. That prevents a safe cross-run reconciliation if the
operator changes the display number and weakens the property-identity evidence
available to the warm-profile gate.

### Reachable code path

- `ma_poc/pms/adapters/funnel.py:121-137` reads only the display unit, value,
  dimension, plan-name, and date attributes from each card.
- `:138-152` emits no `source_ids`, despite the same tag carrying native unit,
  plan, and property IDs.

Required behavior is to make `data-spaces-unit-id` the property-scoped
canonical identity; retain `data-spaces-unit` as the display label and preserve
plan ID, asset ID, and source community name as provenance. The three current
controls must remain 55/55 with exact value/date fidelity after the ID
migration.

### 2026-08-02 remediation validation

The finding was revalidated against all three live first-party inventory
pages before implementation. The schema is unchanged. Windsor Burnet's live
roster moved from 30 to 29 apartments while Cirrus remained 16 and Cougar
Mountain remained 9, so the current denominator is 54; the known Windsor
`2217` card still carries native ID `5376446`, plan `271703`, and asset
`267407`. This is inventory movement, not parser loss.

The registered Spaces parser now preserves the native unit, plan, and asset
IDs, makes the native unit ID canonical, retains the public number as the
display label, and stamps the community/asset boundary plus the exact
unit-producing URL. A complete 54-row retained-source regression verifies
source-to-final ID, plan, bed, bath, area, rent, and date fidelity. A fresh
live source-to-final replay returned 29/29, 16/16, and 9/9 rows respectively;
all 54 native IDs were non-empty and unique, all 54 property bindings matched,
and all 54 published dates survived. The focused Funnel and source-ID registry
suite passed 103/103.

## 26. RealPage public GetUnits routes preserve native IDs and explicit dates

### Verdict

Cleared on two current public RealPage roster shapes: numeric OnlineLeasing
`Proxy/GetUnits` and same-origin CWS `Proxy/GetUnits`. The stateful,
interception-only OLL workflow did not produce unit rows in this canary and is
not included in the certification.

#### Numeric OnlineLeasing roster

The numeric portal host is itself a property ID, and the strict parser admits
only rows whose payload `propertyId` matches that host. Three current marketing
pages each published exactly one numeric root:

| Property | Source / final rows | Native IDs retained | Future dates retained exactly |
|---|---:|---:|---:|
| Mayflower (`25781`, root `8109560`) | 74 / 74 | 74 | 5 / 5 |
| Dixon at Stonegate (`11317`, root `8801344`) | 68 / 68 | 68 | 13 / 13 |
| Shade (`279103`, root `9107021`) | 42 / 42 | 42 | 16 / 16 |
| **Total** | **184 / 184** | **184 / 184** | **34 / 34** |

Every source display number and native ID was unique. Base rent, plan,
dimensions, availability status, and the complete explicit date matched from
source through final output. The parser retained native unit, floor-plan, and
partner-property IDs in `source_ids`.

#### Property-hosted CWS roster

Three exact marketing hosts exposed the same-origin CWS callback consumed by
production:

| Property | Current source / final rows | Native IDs retained | Future dates retained exactly |
|---|---:|---:|---:|
| Bella Mirage (`39346`) | 73 / 73 | 73 | 13 / 13 |
| Taverna at the Forum (`293332`) | 68 / 68 | 68 | 7 / 7 |
| Aven Ridge (`258135`) | 40 / 40 | 40 | 24 / 24 |
| **Total** | **181 / 181** | **181 / 181** | **44 / 44** |

Taverna's live roster decreased from the retained canary's 69 to 68; this is a
current inventory change, not parser loss. All 181 current base rents and
explicit dates matched exactly. All display IDs and native IDs were unique.

These feeds also publish many dates earlier than the August 1 capture date:
146/184 OnlineLeasing rows and 136/181 CWS rows. Production preserves them and
labels them `historical_embedded`. They are source-stale values, not a SurgeX
future-date loss or a manufactured capture date. The important requested
invariant holds: all 78 dates that are actually future-dated in the current
sources survive unchanged.

### Reachable code path

- `ma_poc/pms/adapters/realpage_oll.py:150-228` scopes the numeric roster to
  the portal host and keeps only explicitly available physical apartments.
- `ma_poc/pms/adapters/realpage_cws.py:215-304` maps base rent, explicit
  availability date/status, and native source IDs without substituting
  fee-inclusive `totalRent`.

No fix is justified for these two roster shapes. Retain them as focused
regressions, and keep the OLL workflow and plan-only CWS fallback labeled
unexercised until they have their own exact source-to-final controls.

## 27. Repli360 turns waitlist sentinels into apartments and drops native IDs

### Verdict

Confirmed two Repli360 defects: a four-property availability/row-type defect
and a separate three-property native-identity defect.

#### Waitlist sentinels are emitted as available physical units

The retained strict canary has 963 rows won directly by Repli360. Forty-six of
them across four Sequoia properties have a visible `WAIT...` identifier, no
rent, `availability_status="AVAILABLE"`, and the capture date. Current live
source-to-adapter replay reproduces the same semantic error, with normal live
inventory movement changing the count to 45:

| Property | Canary WAIT rows | Current WAIT rows | Current rows with rent | Output status / date |
|---|---:|---:|---:|---|
| River Oaks (`21347`) | 16 | 17 | 0 | AVAILABLE / 2026-08-02 |
| Shore Park at Riverlake (`2598`) | 11 | 11 | 0 | AVAILABLE / 2026-08-02 |
| Reserve at Capital Center (`2594`) | 10 | 10 | 0 | AVAILABLE / 2026-08-02 |
| Hidden Hills (`16969`) | 9 | 7 | 0 | AVAILABLE / 2026-08-02 |
| **Total** | **46** | **45** | **0** |  |

The current source semantics contradict that output. On all four properties a
sampled WAIT row visibly says `Call for Pricing` and `Availability --`; its
native row ID is real, but it is a waitlist/catalogue sentinel rather than an
available apartment. Hidden Hills and River Oaks also publish an application
link with `MoveInDate=12/31/1969`; the others use `javascript:void(0)`. The
row's `data-available_date` equals the move-in date supplied to the API request,
so it is request echo, not evidence that the sentinel became available today.

The adapter ignores those contradictions, declares every returned row
`AVAILABLE`, and treats `data-available_date` as authoritative. The shared
formatter then faithfully carries the false status/date; this is upstream of
the shared availability resolver.

Required behavior is to classify a row as a non-physical waitlist/catalogue
placeholder when the source jointly shows the WAIT identifier, no numeric
price, `Availability --`/call-for-pricing, and a void or 1969-sentinel action.
Preserve its plan existence if useful, but do not emit it as an available unit
and do not attach the request/capture date.

#### Physical rows lose the source-native unit ID

Repli360's response publishes a native unit ID in `selected_units`, the
`unitlisting` class, `data-apartmentid`, and/or a `UnitID` application-link
parameter. Three current full source-to-final controls show the ID is discarded:

| Property | Current source / final rows | Unique native IDs | Native IDs retained |
|---|---:|---:|---:|
| Marquis at Great Hills (`14117`) | 33 / 33 | 33 | 0 |
| River Oaks (`21347`) | 30 / 30 | 30 | 0 |
| Marquis Sonoran Preserve (`38525`) | 31 / 31 | 31 | 0 |
| **Total** | **94 / 94** | **94** | **0** |

All 94 native IDs differ from the visible apartment number and all visible
numbers happen to be unique in these snapshots. Example: Great Hills apartment
`1238` is native unit `33752567` on floor-plan ID `4464985`. Output keeps only
`unit_id="1238"` and empty `source_ids`. Thus the current row count is clean,
but cross-run identity and source provenance are not.

### Reachable code path

- `ma_poc/pms/adapters/repli360.py:584-638` reads the visible number, price,
  and `data-available_date`, but neither the native row/application ID nor the
  contradictory availability/action text.
- `:625-637` unconditionally emits every row as `AVAILABLE` and passes no
  `source_ids`.
- `:808-813` deduplicates only on visible number plus building after the native
  ID is already lost.

Required identity behavior is to preserve the native Repli/RealPage unit ID as
the property-scoped canonical anchor, retain the visible number separately,
and preserve the floor-plan/site IDs as provenance. Apply that migration only
to real physical rows; WAIT sentinels must not become physical inventory merely
because they also carry an internal database ID.

## 28. MAAC preserves inventory values and dates but discards native identity

### Verdict

Confirmed a MAAC identity/provenance defect across both current source shapes.
The apartment values themselves are clean in the tested scope: six complete
current source-to-final controls preserve all 327 rows, their plan, dimensions,
rent bounds, and dates. All 286 explicit future dates survive unchanged.

| Source shape / property | Source / final rows | Explicit future dates preserved | Native IDs retained |
|---|---:|---:|---:|
| API: MAA Rocky Point (`6194`) | 79 / 79 | 72 / 72 | 0 / 79 |
| API: MAA Wade Park (`52140`) | 64 / 64 | 57 / 57 | 0 / 64 |
| API: MAA Boulder Ridge (`12525`) | 44 / 44 | 32 / 32 | 0 / 44 |
| Embedded HTML: MAA Providence Main (`232992`) | 55 / 55 | 52 / 52 | 0 / 55 |
| Embedded HTML: MAA Trinity (`218985`) | 43 / 43 | 36 / 36 | 0 / 43 |
| Embedded HTML: MAA West Village (`54550`) | 42 / 42 | 37 / 37 | 0 / 42 |
| **Total** | **327 / 327** | **286 / 286** | **0 / 327** |

Every source row has both a unique MAAC item ULID and a unique
`rentCafeApartmentId`; the latter is also the `UnitID` in the public apply
link. The source additionally publishes `rentCafeFloorplanId`,
`rentCafePropertyId`, a MAAC property ULID, and the source property name. The
adapter emits none of those fields in `source_ids` or property provenance and
uses only the visible `apartmentName` as `unit_id`.

For example, Rocky Point apartment `3556TB` is source item
`01KGMK0W458DG6740E1JYTCFVQ`, RentCafe unit `11234319`, RentCafe floor plan
`2321200`, and RentCafe property `611784`. Final output retains only
`unit_id="3556TB"`. All visible apartment names happen to be unique in these
six current snapshots, so no row is deleted today; the defect is unstable
cross-run identity and missing property-binding evidence.

The Providence control is the current source identity after the configured
Paddock Club URL redirects to the rebranded MAA Providence Main page. Its
embedded and API arrays agree exactly, so this is not evidence of
cross-property contamination.

### Reachable code path

- `ma_poc/pms/adapters/maac.py:70-75` chooses `apartmentName` as the only unit
  identifier.
- `:121-142` maps values and dates correctly but emits no `source_ids` and no
  source property-identity fields.
- `:360-363` and `:398-432` feed the same parser from the embedded-HTML and API
  paths, so both routes share the identity loss.

Required behavior is to use `rentCafeApartmentId` as the property-scoped
canonical unit anchor when present, retain the MAAC item ULID as a second
source ID, and preserve the floor-plan/property IDs and returned property name
as provenance. Keep `apartmentName` as the public display label. This is an
identity migration only; no rent or availability-date change is supported by
the evidence.

### 2026-08-02 remediation validation

All six controls were fetched again from their first-party property pages and
resolved public `/api/properties/{ULID}/units/available/` endpoints before the
change. Rocky Point moved from 79 to 80 live rows; the other five counts stayed
64, 44, 55, 43, and 42, for a current total of 328. Every row still carries a
non-empty, within-property unique RentCafe apartment ID and MAAC item ULID,
plus floor-plan/property IDs and an exact returned property name. The known
Rocky Point `3556TB` row retained RentCafe ID `11234319` and MAAC ULID
`01KGMK0W458DG6740E1JYTCFVQ` across the audit boundary.

The shared MAAC row parser now makes `rentCafeApartmentId` canonical, keeps
`apartmentName` as the public display label, and preserves the MAAC unit ULID,
RentCafe floor-plan/property IDs, MAAC property ULID, source property name,
and exact producing URL. A fresh live source-to-final replay retained all
328/328 current rows and both complete native-ID sets. All 286 explicit future
dates survived; mapped plans and public labels were unchanged. The focused
MAAC and source-ID registry suite passed 62/62.

## 29. Encore/Jonah resource identity is clean, but the SSR route drops it

### Verdict

Mixed, with the two Jonah routes separated by exact winning tier.

#### Resource JSON route: scoped clean

Three complete current source-to-final controls preserve all physical rows,
the SightMap-native unit anchor, rent, dimensions, and every explicit date:

| Property | Source / final rows | Native IDs retained | Future dates preserved |
|---|---:|---:|---:|
| 5Line (`276734`) | 152 / 152 | 152 / 152 | 132 / 132 |
| Marlowe Tomoka Village (`288502`) | 137 / 137 | 137 / 137 | 23 / 23 |
| Broadstone Overlands (`295254`) | 17 / 17 | 17 / 17 | 14 / 14 |
| **Total** | **306 / 306** | **306 / 306** | **169 / 169** |

The exact per-plan resource exposes `engrain_data.unit_id`; output retains it
as `source_ids.sightmap_unit_id` and anchors the canonical ID on that value.
All 306 final IDs are unique in-property. Post-processing and the Jugnu
formatter reject or deduplicate zero rows and introduce zero date mismatches.
This clearance is only for `TIER_1_DOM_JONAH_RESOURCE_JSON`.

#### SSR unit-data route: confirmed identity/provenance defect

Three separate complete current controls show the SSR payload's stable IDs are
discarded while its row values and dates remain correct:

| Property | Source / final rows | Source IDs retained | Future dates preserved |
|---|---:|---:|---:|
| Quattro (`253388`) | 7 / 7 | 0 / 7 | 4 / 4 |
| Bryn House (`274384`) | 40 / 40 | 0 / 40 | 34 / 34 |
| Ascend NonaWest I (`278113`) | 54 / 54 | 0 / 54 | 37 / 37 |
| **Total** | **101 / 101** | **0 / 101** | **75 / 75** |

Every one of the 101 exact `data-jd-fp-selector="unit-data"` source rows has
a unique `id_value`, unique Jonah record `id`, unique slug, property ID, and
floor-plan ID. The formatter receives none of them: it emits empty
`source_ids` and anchors identity on the visible apartment number, qualified
by building only when needed. The current visible/building pairs are unique,
so the final row count happens to remain 101/101.

Concrete source examples are Quattro apartment `716` (`id_value=1196234`,
record `id=40688`, property `4467`, floor plan `4`), Bryn House apartment
`1316` (`id_value=37586862`, record `id=262206`, property `p1680785`), and
Ascend NonaWest apartment `04-301` (`id_value=10601550`, record `id=101601`,
property `31617`). In each case output keeps the visible label and loses the
source IDs.

The SSR source-to-final replay also confirms that fee-free base rent is
selected correctly and all 75 explicit future dates survive. This is not a
rent or availability-date defect.

### Reachable code path

- `ma_poc/pms/adapters/_encoreskyline_units.py:247-260` derives SSR identity
  only from `apartment_number` and `building`.
- `:263-313` reads rent/date/dimensions but emits neither `source_ids` nor the
  source property/floor-plan IDs and deduplicates on the display composite.
- By contrast, `:475-523` correctly retains the resource route's
  `engrain_data.unit_id`; that route should remain unchanged.

### 2026-08-02 remediation validation

The complete three-property SSR scope was re-fetched before implementation.
Counts and payload shapes were unchanged: Quattro 7, Bryn House 40, and Ascend
NonaWest 54. All 101 rows again had non-empty, unique `id_value`, record ID,
and slug values, one exact source property ID, and a floor-plan ID. The three
documented example rows retained their prior native values across the audit
boundary.

The SSR parser now prefers `id_value` as canonical identity, retains the Jonah
record ID and slug, preserves property/floor-plan IDs and exact producing URL,
and keeps apartment/building as separate display context. A fresh live
source-to-final replay returned 7/7, 40/40, and 54/54 rows, with 101 unique
canonical IDs and all 75 explicit future dates exact. The untouched resource
route was also live-replayed as a negative-regression control: 5Line 152/152,
Marlowe Tomoka Village 137/137, and Broadstone Overlands 17/17, retaining all
306 SightMap IDs and all 169 future dates. The combined Encore/Jonah and
source-ID registry suite passed 106/106.

Required SSR behavior is to preserve `id_value` as the property-scoped source
unit anchor, retain the Jonah record ID and slug as secondary source IDs, and
retain property and floor-plan IDs as provenance. Register `id_value` in the
SightMap namespace only where returned metadata proves that vendor identity;
otherwise keep an explicit Jonah namespace. Preserve the public apartment
number/building as display fields. No change to base-rent or date selection is
supported by this evidence.

The remaining Encore-attributed winners are not silently certified by this
finding: SightMap inherits finding 23, Funnel inherits finding 25,
RentCafe/SecureCafe inherits findings 1, 13, and 18, and the two BetterNOI
winners remain in the output-only queue.

## 30. Irvine values are clean, but `unitID` is not a community-unique key

### Verdict

Confirmed an identity/provenance defect across the complete current Irvine
adapter cohort. All 13 configured properties replay through the public
`/units/rank` response with exactly the same 599-row total as the strict
canary. Source-to-final value fidelity is clean: 599/599 rows, all rent bounds,
and all 395 explicit future dates survive with zero mismatch.

The source contract proves the current canonical key is insufficient:

| Identity check across the 13 current properties | Result |
|---|---:|
| Source rows / final rows | 599 / 599 |
| Distinct source `objectID` values | 599 |
| Distinct source `propertyID + unitID` composites | 599 |
| Duplicate extras when using bare `unitID` | 80 |
| Final rows carrying any `source_ids` | 0 |

The 80 bare-`unitID` collisions occur on three master communities:

| Property | Source rows | Distinct `unitID` | Collision extras |
|---|---:|---:|---:|
| Crescent Village - Verona (`231107`) | 79 | 74 | 5 |
| Promenade at Irvine Spectrum (`263158`) | 316 | 250 | 66 |
| Santa Clara Square (`230542`) | 81 | 72 | 9 |
| **Total** | **476** | **396** | **80** |

The shared formatter happens to rescue all current collisions by prefixing
the building. For example, Crescent source `unitID=24` occurs once under
property `2915722`, building `04`, source `objectID=2915722_4_24`, and once
under property `2637658`, building `01`, source
`objectID=2637658_9_24`; output invents `04-24` and `01-24` while retaining
neither native anchor. Promenade has the same pattern in 57 collision groups.

Every current `objectID` is exactly
`{propertyID}_{floorplanID}_{unitID}`. More importantly, the simpler
`propertyID + unitID` composite is unique on all 599 current rows, so physical
identity does not need to depend on mutable rent/date or downstream rescue.
All 599 rows also return the same `communityIDAEM` that was read from the
configured marketing page before the API request. The source publishes the
community marketing name, property address, source property ID, floor-plan
IDs, and unique `objectID`; output preserves none of those identity fields.

This audit does **not** confirm cross-property contamination. The marketing
page's `communityIDAEM` binds every returned row, including multi-property
master communities. The configured label `Crescent Village - Verona` versus
the source label `Crescent Village` is a configuration-identity review item,
not enough evidence to delete five of the six source property groups.

### Reachable code path

- `ma_poc/pms/adapters/irvine.py:99-104` selects bare `unitID` before the
  unique `objectID`.
- `:130-151` maps values and dates correctly but emits no `source_ids` and
  drops `propertyID`, `communityIDAEM`, `floorplanUniqueID`, source address,
  and marketing name.
- `:230-250` sends the exact page-derived community GUID but records only the
  generic endpoint URL and body, not the request identity that bound the
  response to the property.

Required behavior is to use the source `propertyID + unitID` composite as the
property-scoped canonical anchor, retain `objectID` and `floorplanUniqueID` as
secondary source IDs, and preserve `communityIDAEM`, community name, source
property ID/address, and the actual request payload as provenance. Keep
`unitID` and building as public display fields. Add a dedicated Irvine test
module; the registry currently has zero directly referencing modules.

No rent or availability-date change is justified. The complete cohort has
zero rent mismatch and preserves all 395 current future dates.

### 2026-08-02 remediation validation

The complete 13-property cohort was revalidated before implementation. Four
marketing pages exposed their page-bound GUID through direct HTTP; the other
nine current 403 pages were read in one bounded clean Hyperbrowser session
with proxy enabled and both stealth and CAPTCHA solving disabled. All 13 GUIDs
then produced ordinary public rank responses. The source denominator remained
599 exactly: 599 unique `objectID` values and 599 unique
`propertyID + unitID` composites, with the same 80 bare-`unitID` collision
extras and row-level `communityIDAEM` echo on all 599 rows.

The parser now makes the property/unit composite canonical, preserves raw
unit/object/property/floor-plan/community IDs, retains public unit/building,
community name and property address, and records the exact active request
payload alongside the unit-producing endpoint. A fresh live source-to-final
replay stayed 599/599 with all rents and all 395 explicit future dates exact.
The 80 former building-prefix rescues are now explicit source-backed identity
migrations with no row loss. The new dedicated Irvine module and source-ID
registry suite passed 52/52.

## 31. AMLI admits submarket siblings and drops exact target-unit fields

### Verdict

Confirmed three distinct defects across the complete 11-property current AMLI
cohort: cross-property admission, native identity/field loss, and unit-area
substitution. Base rent and explicit availability dates are clean.

#### Eight properties admit every sibling array in the submarket

The strict canary's 522 AMLI rows are submarket totals, not target-property
totals. Current inventory is one row lower at Broadway Park, but otherwise
reproduces the canary property-by-property. The current 521-row adapter source
decomposes into only 254 rows for the configured properties and 267 rows from
sibling properties:

| Configured property | Canary rows | Current adapter rows | Exact target rows | Sibling rows |
|---|---:|---:|---:|---:|
| AMLI Broadway Park (`261770`) | 31 | 30 | 30 | 0 |
| AMLI Quadrangle (`37386`) | 49 | 49 | 18 | 31 |
| AMLI Dry Creek (`62778`) | 20 | 20 | 20 | 0 |
| AMLI Evanston (`54553`) | 36 | 36 | 11 | 25 |
| AMLI on Aldrich (`237704`) | 53 | 53 | 8 | 45 |
| AMLI Dadeland (`61548`) | 44 | 44 | 20 | 24 |
| AMLI Toscana Place (`239952`) | 52 | 52 | 35 | 17 |
| AMLI West Loop (`242191`) | 28 | 28 | 28 | 0 |
| AMLI Midtown 29 (`68148`) | 92 | 92 | 24 | 68 |
| AMLI Riverfront Green (`66940`) | 46 | 46 | 26 | 20 |
| AMLI South Shore (`40193`) | 71 | 71 | 34 | 37 |
| **Total** | **522** | **521** | **254** | **267** |

The source makes each boundary authoritative. Every floor-plan query carries
`queryKey.input.amliPropertyId` and a Prismic property document ID; those IDs
match the exact configured property's current page. Floor plans also carry a
single `entrataPropertyId` and CMS property name. For Midtown 29, the payload
contains AMLI Wynwood (40 units), AMLI Midtown 29 (24), and AMLI Midtown Miami
(28). Current output's 92 rows are exactly `40 + 24 + 28`.

The parser instead searches for a `propertyUid` inside each floor-plan row.
The current schema supplies property identity on the enclosing query, not on
the row. When no row-level UID exists, the parser explicitly accepts the
entire array, and the caller loops over every array in the submarket.

All 11 current property pages also contain exact target floor-plan queries
under `props.pageProps`; the current helper looks only under root
`pageProps`, so it sees zero of them and unnecessarily takes the contaminated
submarket fallback. A simple root-path change is not sufficient because each
page contains a second floor-plan-shaped list; select the exact
`["amli", "floorplans"]` query and verify its two target IDs.

#### Correct target rows still lose bedrooms, building, identity, and area

After selecting only the exact 254 target rows and replaying them through
post-processing and the Jugnu formatter:

| Exact target-field check | Source | Final |
|---|---:|---:|
| Physical rows | 254 | 254 |
| Rows with explicit bedroom count | 254 | 0 |
| Rows with explicit `buildingNumber` | 254 | 0 |
| Rows with unique `unitId` / `engrainUnitId` | 254 | 0 in `source_ids` |
| Rows with explicit unit `squareFeet` | 254 | 254, but 23 values differ |
| Explicit future dates | 198 | 198 exact |

The bedroom loss is a schema-key error: current floor plans publish
`bedroomMax`/`bedroomMin`, while the parser reads only `bedrooms`. The adapter
does not map unit `buildingNumber` at all. It uses floor-plan `sqftMin` before
unit `squareFeet`, causing 23 wrong target-unit areas. Every target `unitId`
and `engrainUnitId` is unique within its property, yet neither survives.
Toscana also has one repeated public `unitNumber`; its native IDs distinguish
the rows and should be canonical rather than relying on downstream plan-ID
rescue.

The current 254 target rows have zero base-rent mismatch and zero date
mismatch. AMLI publishes both `baseRent` and fee-inclusive `totalRent`; the
adapter's `rent` choice equals `baseRent` on every current target row. Do not
change rent or date selection.

### Reachable code path

- `ma_poc/pms/adapters/amli.py:83-102` ignores the current
  `props.pageProps` wrapper and discards query identity when returning bare
  arrays.
- `:132-177` looks for row-level property UID and accepts every row when the
  current schema correctly provides identity only on the enclosing query.
- `:181-206` reads the obsolete `bedrooms` key, selects plan `sqftMin`, omits
  unit `buildingNumber`, and emits no native IDs.
- `:351-354` loops over every submarket floor-plan array without binding its
  query input to the configured property.

Required property behavior is to extract the page's target AMLI property ID
and Prismic document ID, select only the exact `amli/floorplans` query whose
input matches both, and fail closed on contradiction. Preserve the query
identity and Entrata property ID/name in provenance.

Required row behavior is to map `bedroomMax`/`bedroomMin`, unit
`buildingNumber`, and unit `squareFeet`; use native `unitId` as the canonical
anchor and retain `engrainUnitId`, nonzero `entrataUnitId`, floor-plan ID, and
property IDs as source provenance. The 11-property migration ledger must
separate 267 rejected sibling rows from deliberate canonical-ID changes on
the 254 target rows.

## 32. On-Site kept rows and dates but lost baths, plans, and canonical ID

### Verdict

Confirmed three direct-route field/identity defects in the audit snapshot and
implemented the bounded remediation on 2026-08-02. The original snapshot had
28 linked properties and 284 rows. A complete same-day re-enumeration of all
49 On-Site-attributed marketing pages found 47 current links: 39 now return a
property-bound active roster, three return a bound zero-unit roster, three
return an HTTP-200 error shell with no property boundary, and two return HTTP
500. Mill Creek and Reserve at Evanston are the two current no-link controls.
The denominator expansion is live source evolution, not a claim that the
original 28-property measurement covered 47 pages.

The 39 current active rosters expose 368 whitelisted application objects. One
is a proven non-unit option at Seville at Mace Ranch: apartment label
`Roommate Add O`, plan `Roommate Add On`, 0 beds, `0 bath`, null area, and
waitlist state. The adapter now removes that exact multi-signal sentinel and
retains 367 physical units. Post-fix source-to-final replay is:

| 2026-08-02 current direct-route check | Source | Final |
|---|---:|---:|
| Physical rows after proven sentinel exclusion | 367 | 367 |
| Unique native On-Site unit IDs | 367 | 367 canonical IDs |
| Positive supported bathroom labels | 367 | 367 numeric baths |
| Rows with provable source plan name | 354 | 354 exact |
| Rows without a source style/name binding | 13 | 13 blank |
| Explicit availability dates | 367 | 367 exact normalized dates |
| Explicit future dates as of 2026-08-02 | 213 | 213 exact |
| Source rents | 367 | 367 exact |
| Published numeric areas | 345 | 345 exact |
| Source-missing areas | 22 | 22 truthful `-1`/missing markers |
| Richer `display_unit_number` labels | 61 | 61 retained as `unit_name` |
| Shell-property-bound rows | 367 | 367 with ID/name/address provenance |

The bathroom defect was deterministic: the source publishes values such as
`"1 bath"`, `"2 bath"`, `"1 1/2 bath"`, and `"2 1/2 bath"`; the old adapter
forwarded the labels unchanged, and the numeric formatter rejected them. The
new parser accepts only positive, bounded half-step numeric values. It does
not turn the non-unit sentinel's `0 bath` into a fabricated apartment fact.

The current floor-plan objects publish `name` and `style_id`, but the old plan
regex stopped at the first nested `}`. Modern `starting_term` objects contain
nested price dictionaries before `style_id`, so the map was empty. Balanced
top-level object parsing now binds 354 exact names. The remaining 13 reference
style IDs absent from the shell's floor-plan list and remain unproven rather
than being filled heuristically. Four exact operator names (`1 Bed 1 Bath` or
`2 Bed 1 Bath`) would otherwise hit the generic-placeholder scrubber; an exact
On-Site plan-name provenance token preserves them without weakening the
generic path's hygiene rule.

Every current physical row has a unique native `id`; it now drives canonical
`unit_id`, while `apartment_num` remains `unit_number` and the 61 richer
`display_unit_number` values survive as `unit_name`. The adapter also retains
the shell property ID and each unit object's property ID separately. This is
load-bearing at Ventana 257: the requested/returned shell is property 717420,
while 34 explicitly whitelisted units belong to child property 717421. Those
are retained as a proven aggregate, not rejected as sibling contamination.

The shell provides authoritative property metadata too. On all 39 active
controls, `property.property_id` exactly equals the ID requested from the
marketing link, and every admitted row now retains property ID/name/address,
the exact unit-producing URL, and request provenance. A missing or mismatched
shell boundary fails closed before admission. The three current HTTP-200 error
shells and two HTTP-500 responses therefore remain explicit no-data outcomes;
they are not counted as fixed unit rosters.

### Reachable code path

- `ma_poc/pms/adapters/onsite_apply.py:163-166` uses a brace-hostile plan
  regex; nested `starting_term.best_price` objects terminate it before
  `style_id`.
- `:229-262` maps bathroom label text without numeric normalization, uses the
  public apartment label as identity, and does not preserve the richer display
  label or shell property metadata.
- `:307-345` fetches an exact numeric property URL but admits its roster
  without validating the returned property ID/name/address.

Implemented behavior parses top-level floor-plan objects with balanced braces,
binds `style_id` to the exact source name, normalizes only the bounded numeric
bath value, and uses native On-Site unit ID as canonical while keeping both
public number forms. It validates and retains returned property
ID/name/address, preserves child-property provenance, and keeps the active
`unit_list` whitelist, rent, area, and date logic unchanged. The focused
regression/registry/formatter suite passes 354/354 tests; live end-to-end
adapter controls pass Pullman (12 rows), Ventana 257 (40), and Seville (5
physical rows after the sentinel exclusion).

## 33. Equity values were clean, but identity was lossy

### Verdict

The original audit checked the complete 26-property canary cohort against
current first-party pages. Fifteen ordinary direct pages returned 181 rows,
ten canary-success pages returned transient HTTP 403, and The Terraces
redirected to the generic Equity home page. The retained strict canary had 338
physical rows from 25 successes and one `TIER_1_API_EQUITY_NO_RESPONSE`.

The 2026-08-02 remediation recheck found substantial source-access evolution.
In the final reconciled snapshot, 24 properties returned their server-rendered
unit blocks directly and one transiently blocked control was recovered through
one bounded compliance-mode Hyperbrowser session (residential proxy on;
CAPTCHA solving and stealth off). Those 25 current rosters contain 344 physical
rows. The Terraces still redirects to the generic home page and remains the
sole explicit no-response control. Earlier same-day direct passes ranged from
23 to 24 reachable properties, so this is a timestamped fetch snapshot, not a
claim that Cloudflare reachability is permanently fixed.

All mapped commercial and availability fields are exact on the 15 currently
reachable controls:

| 2026-08-02 current complete-cohort check | Source | Final |
|---|---:|---:|
| Physical rows | 344 | 344 |
| Explicit future dates | 269 | 269 exact |
| Rent / plan-contract / bed / bath / area / floor mismatches | — | 0 / 0 / 0 / 0 / 0 / 0 |
| Lease-term normalization mismatches | — | 0 |
| Rows with complete Equity source identity | 344 | 344 |
| Unique `buildingId:unitId` composites | 344 | 344 canonical IDs |
| Bare `unitId` collision extras | 9 | 0 canonical collisions |

The apparent 181-row lease-term mismatch from the first mechanical comparison
is not a defect. Equity publishes strings such as `12 mo`; the output contract
normalizes them to integer months (`12`) and retains the exact source string as
`lease_term_raw`. A retained 2501 Porter row independently confirms that
source-to-final transform.

Identity was defective. Equity's comment block supplies `ledgerId`,
`buildingId`, and `unitId`, but the adapter maps only building and public unit
number and emitted no `source_ids`. Across all 344 current rows,
`buildingId + unitId` is unique within each property. Bare `unitId` is not:
Circa Fitzsimons now has eight collision extras and Liberty Park has one. The
old downstream collision rescue happened to make those outputs unique, but it
produced an asymmetric derived policy rather than preserving the source
apartment anchor. Both the server-HTML `equity` path and the currently N0 DOM
fallback `equity_apartments` now emit the same source-backed composite while
keeping building and public unit number separately.

`ledgerId` must not be promoted as apartment identity. It repeats across many
unit blocks and Summit Crossing now publishes two ledger IDs within one
marketing page. It is useful source-asset provenance only. The correct
property-scoped apartment anchor is the `buildingId + unitId` composite.

The originally blocked Village at Del Mar control was captured as a retained
real first-party fixture after a compliant live session returned eight current
rows. The dedicated Equity paths plus source-ID registry pass 73/73 tests, and
the broader affected formatter/availability suite passes 292/292. Live full
adapter executions pass Circa Fitzsimons (27 rows/eight bare collisions),
Liberty Park (10/one), and Summit Crossing (33 rows/two ledgers).

### Reachable code path

- `ma_poc/pms/adapters/equity.py:54-56` parses all three source identifiers.
- `:82-135` discards `ledgerId`, maps `buildingId` and `unitId` only as display
  fields, and emits no native identity map.
- `ma_poc/scripts/runners/jugnu.py:3326-3327` initially aliases public
  `unit_number` to canonical identity; downstream collision rescue can make
  the result unique but cannot recover honest source provenance.

Implemented behavior uses the property-scoped `buildingId + unitId` composite
as canonical identity, retains both components plus `ledgerId` in
`source_ids`/provenance, and keeps public building/unit labels separately. The
DOM fallback additionally retains the exact `/UnitFees/{property}/{building}/
{unit}` property ID. Rent, plan hygiene, dimensions, availability date, and
lease-term normalization are unchanged. The retained Village fixture and the
complete 344-row source-to-final replay cover the formerly blocked route; the
focused GCP canary remains the release gate.

## 34. Essex preserves mapped values but loses native identity and fails opaquely

### Verdict

The complete 27-property Essex cohort was live-probed through the same
first-party page-derived bulk API used by the adapter. Every current community
page resolved a property ID, every exact bulk endpoint returned HTTP 200, and
the complete source-to-final replay preserved 340/340 rows. All mapped fields
were exact:

| Complete current-cohort check | Source | Final |
|---|---:|---:|
| Physical rows | 340 | 340 |
| Explicit future dates | 234 | 234 exact |
| Rent / plan / bed / bath / area / floor / building mismatches | — | 0 / 0 / 0 / 0 / 0 / 0 / 0 |
| Unique native `unit_id` values | 340 | 0 in `source_ids` |
| Native `floorplan_id` values | 340 | 0 retained as source IDs |

There is no current display-number collision: native `unit_id`, public
`name`, and `building_name + name` are all unique within each current
property. The identity defect is nevertheless direct rather than hypothetical.
The API publishes a stable native apartment ID and floor-plan ID on every row;
the parser replaces native identity with the public apartment label and drops
both source IDs. A later display-label change therefore looks like a new
apartment, and the output cannot be traced back to the exact source object.

The request is property-bound but its binding is also discarded. The marketing
page supplies the Essex `propertyId`, and the adapter sends that exact value in
`/api/properties/{propertyId}/availability`. The bulk response does not echo a
property ID per row, so the page-derived request ID and final response URL are
the authoritative binding and must be retained as provenance. No current
cross-property response was observed.

#### The nine canary misses are recoverable, but their immediate causes differ

The strict canary produced only 18 Essex successes (212 rows) and nine
`FAILED_NO_DATA` records. The retained raw page artifacts now cached locally
separate those misses:

- Eight artifacts are exact Next.js 404 shells (`itemPath=/404`) with no
  `propertyId`: Avondale at Warner Center, Brookside Oaks, Carmel Creek,
  Foster's Landing, Mira Monte, Summit Park Village, The Palms at Laguna
  Niguel, and The Village at Toluca Lake I. The adapter therefore had no API
  key. The same configured URLs currently resolve to valid community pages and
  their exact APIs expose 120 current units.
- Belcarra's retained page is a valid property page and contains
  `propertyId=510860`, but the canary still emitted no rows. The adapter records
  neither bulk response status nor exception/body-shape evidence, so the
  retained run cannot distinguish a transient non-200, invalid JSON, an empty
  response, or another fetch failure. Its exact current endpoint returns 13
  rows.

This proves a failure-observability/recovery gap, but it does **not** justify
inventing a single historical cause for all nine. The current 133-row total is
a live denominator and is not substituted into the earlier canary count.

### Reachable code path

- `ma_poc/pms/adapters/essex.py:82-85` correctly finds a property ID when the
  page contains one.
- `:95-165` reads native `unit_id` and `floorplan_id` but emits neither as
  source identity; it uses public `name` as `unit_number`.
- `:169-231` performs one active page/API attempt and turns every response
  status, parse exception, invalid shape, and explicit 404 shell into the same
  empty list without outcome telemetry.
- `:400-422` returns the bulk rows when present, but does not retain the
  page-derived property ID or actual response identity on each row.

Required row behavior is to use native `unit_id` as the property-scoped
canonical anchor and retain native floor-plan ID, public apartment label,
building, page-derived property ID, exact request URL, and response provenance.
Do not change the currently exact values or dates.

Required recovery behavior is to classify an explicit 404 shell separately
from a bulk API failure, record bulk status/exception/body shape, and perform a
bounded fresh canonical-page/API retry before declaring no data. A canonical
URL discovery rule must be fixture-backed; do not guess a portfolio sibling or
manufacture a property ID. Regression must include all eight retained 404
shells, Belcarra's valid page plus a failed bulk response, and at least three
current successful controls through final formatting.

#### Implemented and revalidated locally on 2026-08-02

The adapter now selects the page's `PropertyName + PropertyId` pair against the
configured community before accepting either a captured or active bulk
response. This matters because a valid Essex page embeds both the global 404
route and a portfolio-wide property catalogue: `itemPath=/404` alone is not a
soft-404 signal, and the first property ID is not necessarily the configured
community. A shell is classified `SOURCE_404_SHELL` only when it has no
configured-name property boundary and no non-404 community route. Captured API
property-ID mismatches fail closed.

Each page and bulk attempt now records requested/final page URL, page-derived
property ID/name, HTTP status, exception class, response shape/hash, row count,
and one mutually exclusive outcome. An explicit shell or retryable bulk failure
gets exactly one no-cache request to the configured property page and its exact
page-derived API; no portfolio sibling or manufactured ID is tried. Paid Web
Unlocker is explicitly disabled on this production path.

Rows now use native Essex `unit_id` as canonical identity and retain
`essex_unit_id`, `essex_floorplan_id`, and `essex_property_id` plus public unit
label, building/floor, request payload, page binding, and exact unit-producing
response provenance. The complete 27-property adapter-to-final replay remains
340 source / 340 internal / 340 final rows, with 340 unique native IDs, 234
future dates, 27 response-provenance records, and zero mismatches for identity,
plan, public label, beds, baths, area, floor, building, rent low/high, or date.
The retained eight-shell set and Belcarra's forced non-200, invalid-JSON,
invalid-shape, and authoritative-empty outcomes are regression-covered. Local
Essex plus source-ID-registry tests pass 85/85; the focused GCP canary remains
the release gate.

## 35. FortressTech is stable, but a trusted 282-sq-ft plan is falsely clamped

### Verdict

The complete 10-property FortressTech cohort is current, deterministic, and
property-scoped. Every marketing page exposes one exact org/property UUID
pair, all ten exact availability widgets return their SSR unit query, and the
current source contains exactly the same 170 public apartment labels as the
strict canary. All 170 label joins carry the same native `unitId` UUID in both
captures; there is no current native-ID rotation, public-label collision, row
loss, or cross-property response.

The complete source-to-final replay is clean except for nine real areas:

| Complete current-cohort check | Source | Final |
|---|---:|---:|
| Physical rows | 170 | 170 |
| Stable unique native UUIDs | 170 | 170 retained in `source_ids` |
| Explicit future dates | 91 | 91 exact |
| Rent / plan / bed / bath / date mismatches | — | 0 / 0 / 0 / 0 / 0 |
| Exact square-foot values | 170 | 161 exact; 9 discarded |

Vivo Living Port Royal's first-party FortressTech roster publishes nine
distinct Beaufort apartments. Every row independently carries
`floorPlanBeds=1`, `floorPlanBaths=1`, and `floorPlanSquareFeet=282`, plus a
unique native UUID, rent, and availability date. The adapter correctly emits
`sqft="282"`; `post_process` then changes it to null on all nine rows because
the shared bedroom-relative sanity rule assumes every one-bedroom apartment
must be at least 350 square feet. Final output therefore emits `area=-1` and
labels a value the operator explicitly published as not captured.

This is not a reason to remove global area sanity. It is a direct counterexample
to applying a heuristic intended for ambiguous/LLM extractions to a typed,
first-party unit field. The absolute 150-square-foot lower bound already accepts
282; only the bedroom-relative pass rejects it.

Identity itself is source-backed. Public unit labels and native UUIDs are each
unique on every current property, and native UUIDs are retained under
`source_ids.fortresstech_unit_id`. No canonical-ID migration is justified by a
current collision. The property-binding provenance is weaker than the source,
however: the exact org/property UUIDs live in the unit-producing URL, but the
final property record's `unit_source` is empty and row source IDs do not retain
those property UUIDs. The adapter also receives the real iframe status as
`_status` and then records a hard-coded 200 in its synthetic response record.
All ten current widgets really did return 200, so no current status mismatch is
claimed; the code must still retain the measured status instead of fabricating
one on a future response.

The configured `Resia Tributary` URL currently redirects to the renamed Sylvan
Tributary marketing site, but its exact Fortress property UUID and all 68
native apartment UUIDs match the canary. That is evidence of a rename, not
contamination; update the configured display name separately if desired.

### Reachable code path

- `ma_poc/pms/adapters/fortresstech.py:223-283` maps the typed 282-square-foot
  value and correctly retains native unit UUID.
- `ma_poc/extraction/sanity.py:120-138` applies a 350-square-foot heuristic to
  every one-bedroom row regardless of source confidence; `:683-707` nulls all
  area aliases before the formatter can retain the raw value.
- `ma_poc/pms/adapters/fortresstech.py:390-391` receives the actual widget
  status, while `:433-439` discards it and records 200 plus a placeholder body.

Required behavior is to preserve an in-range typed area from this exact
first-party roster (or provide an equivalent narrowly source-qualified bypass
of the bedroom-relative heuristic) while keeping absolute bounds and ambiguous
source checks. Preserve the pre-sanity raw value and reason in final output.
Also retain org UUID, property UUID, exact response URL/hash, and actual status
as property/unit provenance. Do not change the currently exact rent, date,
plan, bed, bath, row, or native-unit-ID behavior.

### Implementation and independent recheck

Implemented locally on the consolidated Codex branch. The exception now
requires all of: the exact FortressTech availability host/path, matching org
and property UUIDs in the URL, matching row source IDs, a native unit UUID, the
exact `TIER_1_SSR_FORTRESSTECH` tier, and the typed
`floorPlanSquareFeet` marker/value. It bypasses only the bedroom-relative
heuristic; the absolute 150–10,000 bound still runs first. Final rows expose
`area_pre_sanity_value`, `area_sanity_decision`, `area_sanity_reason`, and
`area_sanity_source`. The adapter also disables paid Web Unlocker explicitly,
retains the measured response status/hash/URL, and carries org/property UUIDs
on every row and in `unit_source` identity.

The complete ten-property live adapter-to-final replay on 2026-08-02 remains
170/170 rows. All 170 rows have the native unit UUID plus org/property binding,
all ten properties have one exact response-provenance record, and mismatch
counts are zero for public identity, plan, beds, baths, area, rent low/high, and
availability date. Vivo now preserves 282 square feet on 9/9 rows; the other
161 areas are unchanged. The source currently publishes 151 future dates
(inventory moved after the earlier 91-date capture), and all 151 survive
exactly; the strict retained capture's 91 dates are the later focused-canary
comparison gate. Negative controls prove that a missing marker, an LLM tier, an
untrusted host, a mismatched property UUID, or an absolute-out-of-range value
still clamps. The focused FortressTech/sanity/schema/source-ID suite is green
at 441 tests. GCP has not yet been launched; the consolidated focused canary
remains the release gate.

## 36. ResidentServices365 drops visible unit fields and multiplies one plan catalogue

### Verdict

The complete ten-property ResidentServices365 cohort was live-probed through
the same public server-rendered floor-plan and unit-detail pages used by the
adapter. All ten `/Marketing/FloorPlans` pages returned 200, all 72 linked
detail pages were within the adapter's 16-page/property bound, and the current
source reproduced the same 108 physical apartments as the strict canary. The
108 native apartment GUIDs are retained and unique; no current row loss,
cross-property response, or public-unit-label collision was found.

The rows are nevertheless semantically incomplete because the parser ignores
fields that are visible inside every exact unit block:

| Complete current-cohort check | Source | Strict-canary final |
|---|---:|---:|
| Physical apartments | 108 | 108 |
| Native apartment GUIDs | 108 | 108 retained |
| Exact visible floor-plan names | 108 | 0 exact |
| Published lease terms | 108 | 0 |
| Visible floor numbers | 34 | 0 |
| Explicit future availability dates | 65 | 65 exact |
| Visible `Now` / `Today` rows | 43 | 41 stale historical dates; 2 dates happen to equal capture date |

Every final floor-plan name is synthetic URL text: 65 rows say `Units`, while
43 contain a title-cased plan GUID. For example, Greenarch apartment S407
visibly says `Floor Plan: Greenwood` and `Floor: 4`, but final output calls the
plan `6f852f38 Fad8 41dc A594 Dda77320fc32` and emits no floor. Oxford 3103
visibly says plan `A1a`, but final output calls it `Units`. The adapter has not
failed to reach the plan name; it deliberately emits an empty name after
discarding the parent plan association. The shared URL-slug fallback then
mistakes either the literal `/Units/` segment or `/floorplan/{guid}` for a
human plan name.

Availability has the same source-selection defect the user asked to audit.
All 43 current-source rows whose UI says `Now` or `Today / Available` carry an
embedded epoch on or before the capture day. The parser always chooses that
epoch instead of the visible state. Forty-one therefore ship dates weeks or
months in the past with `historical_embedded` provenance. Village Square
021927-402, for example, visibly says `Today / Available` while the output
ships `2026-06-18`; Greenarch's currently available rows show the same shape.
This is not the tolerated one-day scrape-time difference. The 65 rows with an
explicit future UI date retain that date exactly and must remain unchanged.

Telfair Lofts exposes an additional deterministic rent-selection error on all
29 current apartments. Each unit has a visible `Best Value` control whose
structured popover supplies one matched triplet: per-month rent, lease term,
and move-in date. The parser instead selects the first hidden pricing span in
DOM order. All 29 selected rents differ from the visible best-value rent and
all 29 selected lease terms are discarded. Apartment 1114 publishes best
value `$1,406`, term 13, move-in `2026-09-12`; final output selects the first
hidden 18-month price `$1,558`, emits no term, and happens to keep the same
date. On seven currently available Telfair rows, even the best-value move-in
date differs from the historical embedded epoch selected by the parser. The
fix must preserve one coherent rent/term/move-in tuple rather than combining
fields from different pricing options.

The plan-only fallbacks expose a separate merge problem:

- The Vue currently publishes exactly ten plan cards and no physical units.
  Final output contains 25 plan rows: the ten source plans plus five JSON-LD
  and ten generic-text restatements. Those are 15 semantic duplicate extras,
  not 25 distinct plans.
- Westshore publishes four plan cards, each explicitly saying one unit is
  available. Its four retained JSON-LD rows preserve dimensions and rent but
  lose all four counts and emit `UNKNOWN` state.
- Rustic Woods publishes four plan cards. Final plan rows preserve the four
  names/rents/areas, but drop the explicit two-bedroom value on Concorde and
  Chateau because a generic tier wins those rows even though the dedicated
  parser has the dimension.

### Reachable code path

- `ma_poc/pms/adapters/residentservices365.py:248-280` extracts only detail
  URLs and discards the already-known parent-plan association.
- `:320-425` reads the first rent span and embedded epoch, maps no visible
  plan/floor/lease-term/best-value fields, and explicitly emits an empty plan
  name.
- `ma_poc/pms/adapters/_parsing.py:938-975` treats both `/floorplans/{segment}`
  and `/floorplan/{segment}` as human plan slugs; `:1226-1234` applies that
  fallback to the deliberately empty unit name, producing `Units` or a GUID.
- `ma_poc/pms/adapters/residentservices365.py:501-518` returns immediately
  when physical rows exist, so there is no later adapter-local plan-name join.

Required unit behavior is to retain the parent plan identifier/name on every
detail URL, parse visible plan and floor as a cross-check, preserve a coherent
selected rent/term/move-in option, normalize visible `Now`/`Today` to the
capture date with explicit provenance, and preserve every explicit future date
exactly. Required plan behavior is to reconcile plan candidates by source plan
identity/specification before output, prefer the dedicated RS365 card fields,
and emit each of The Vue's ten source plans once. Regression must replay the
complete ten-property cohort, including all 108 current apartments, all 43
visible-current cases, all 29 Telfair pricing tuples, and the three plan-only
or mixed controls above.

### Implementation and independent recheck

Complete locally on 2026-08-02. The adapter now keeps the authoritative parent
plan GUID/name on every detail route, cross-checks it against the visible unit
label, preserves the native unit GUID, floor, building, lease term, and exact
unit-producing response provenance, and fails closed on a plan mismatch. It
selects Telfair's structured Best Value rent/term/move-in tuple while retaining
the separate visible `Now`/`Today` availability fact. The shared URL fallback
now rejects the literal `Units` route and exact UUID slugs. Plan output is
channel-separated and keyed by `rs365_floorplan_guid`; the hop merge narrowly
prefers an RS365 plan over a generic same-name restatement without changing the
general rent-sensitive identity rule.

A fresh complete-cohort adapter-to-final replay fetched all ten current public
catalogues and their 72 exact detail routes, built an independent DOM ledger,
then ran the adapter from the captured responses. It retained **108/108**
physical apartments, **108/108** native unit GUIDs, **108/108** exact plan
names, **108/108** lease terms, and **34/34** visible floors. Current inventory
has shifted slightly to **42** visible-current rows and **66** explicit future
rows; all 42 emit the capture date with `available_now`, and all 66 future
dates remain exact. All **29/29** Telfair rows match the independently parsed
Best Value rent, term, and move-in tuple. The adapter emits exactly **72/72**
source plans across the cohort: The Vue 10, Westshore 4 with state/counts, and
Rustic Woods 4 with both two-bedroom dimensions intact. There were zero
apartment-field mismatches and zero nonzero plan-field mismatches; 13
unpriced plan cards explicitly carried source rent `0`, which the standard
schema correctly represents as null. Focused local tests are green; the
strict affected-property GCP canary remains the release gate.

## 37. RentalAddress plans are exact, but inquiry-only cards receive a fake date

### Verdict

The complete current RentalAddress cohort is one property, Cedar Ridge. Its
public `/floor_plans` page was downloaded once and replayed through the exact
adapter parser. The source contains two plan cards and no apartment roster.
Final output preserves both plans and every published value exactly:

| Current complete-cohort check | Source | Strict-canary final |
|---|---:|---:|
| Plan cards | 2 | 2 |
| Exact plan / bed / bath / area / rent rows | 2 | 2 |
| Physical apartments or native unit anchors | 0 | 0 |
| Explicit availability dates / `Available Now` labels | 0 | 0 |
| Capture-date availability values | 0 | 2 |

The source says only `Check Availability` and links to `/apply_online`; it does
not say either plan is available now and publishes no date. The adapter
correctly emits `availability_status="UNKNOWN"`, no unit number, and no date.
The floor-plan formatter nevertheless gives both rows the capture date with
`capture_date_default` provenance because the shared unit formatter sees rent
before the floor-plan wrapper clears synthetic identity. The wrapper removes a
manufactured date only when it rewrites status to `UNAVAILABLE`; an `UNKNOWN`
inquiry-only plan therefore keeps the false date.

The property telemetry is independently contradictory. The property verdict
is `SUCCESS_PLAN_LEVEL (2 plans)`, while `publish_ceiling` calls the same record
`EXTRACTION_MISS`, reports `n_plan_summaries=0`, and says eight rent tokens with
zero units prove a miss. `_meta.provenance.data_quality.plan_level_units` is
also zero because that counter scans only `units[]`, not the two emitted
`floor_plans[]`. This does not alter the two plan values, but it makes a valid
plan-only source look like a failed unit extractor and can misdirect recovery
cost and coverage reporting.

### Reachable code path

- `ma_poc/pms/adapters/rentaladdress.py:104-157` correctly parses both cards,
  emits no unit identity/date, and explicitly marks state `UNKNOWN`.
- `ma_poc/scripts/runners/jugnu.py:2836-2874` formats a plan through the unit
  formatter first, clears identity afterward, and removes an invented date
  only for final `UNAVAILABLE`, not `UNKNOWN`.
- `ma_poc/scripts/runners/jugnu.py:2098-2135` assesses publish ceiling from a
  separate extraction object; the retained record proves that object's plan
  summaries did not match the two summaries later written at `:2642-2771`.
- `ma_poc/scripts/runners/jugnu.py:2404-2426` counts plan-level evidence only
  inside `units[]`, so a correct `floor_plans[]`-only property reports zero.

Required behavior is to leave `available_date` null on a plan card with only
rent plus an inquiry CTA and `UNKNOWN` state. Preserve the two exact plans and
their honest plan-level success. Publish-ceiling and provenance accounting
must consume the same final plan-summary collection used by the formatter, so
the property reports two plans and never calls this exact capture an extraction
miss. Because this is a one-property platform cohort, the regression must use
the complete retained/current Cedar Ridge source rather than generalizing a
multi-property family rule.

### Implementation and independent recheck

Complete locally on 2026-08-02. Both V2 floor-plan formatters now clear a
manufactured date and label its provenance `missing` whenever a no-anchor plan
finishes in a state other than explicit `AVAILABLE`; source-published
`Available Now` and explicit future dates still survive. RentalAddress marks
only rows from its complete bounded `.floor_plan_list` surface. Publish-ceiling
now reads the final `result.plan_summaries` collection and allows that exact
proof to explain its own rent tokens, while unverified plan rows remain behind
the Madrid rent-token guard and any unit-bearing embed/vocabulary still vetoes
the proof. Provenance counts the separate plan-summary channel without adding
those rows to physical-unit identity counts.

A fresh direct fetch of Cedar Ridge returned HTTP 200 and independently
reproduced the complete two-card source: `1 Bedroom/ 1 Bath`, 598 sq ft,
$1,475 and `2 Bedroom/ 2 Bath`, 880 sq ft, $1,750. Both core and production
formatters preserve those values and `UNKNOWN` state with null date and
`missing` provenance. The current page contains ten rent-signal tokens; the
final two-plan proof now reports `CONFIRMED_PLAN_ONLY`, `n_plan_summaries=2`,
and provenance reports `plan_level_units=2` / `plan_summary_count=2` while
`unit_count=0`. Explicit-now, future, negative, Madrid-guard, and unit-vocab
negative controls are green. Focused GCP canary remains pending.

## 38. AspenSquare's Knock fallback loses public identity and reverses availability

### Verdict

The complete eight-property AspenSquare cohort was reconciled against all
current first-party Aspen floor-plan pages, all eight exact Knock property
responses, and the strict-canary final output. The route is property-bound and
its native identity is stable, but its public identity and availability
semantics are not:

| Complete-cohort check | Result |
|---|---:|
| Current Knock rows | 100 |
| Rows passing the production hidden/leased/reserved/rent gate | 87 |
| Strict-canary physical rows | 87 |
| Canary native UUIDs repeated in the current exact property responses | 87 / 87 |
| Eligible source rows with `available=true` | 87 / 87 |
| Eligible source rows with `occupied=true` | 42 / 87 |
| Source-available rows emitted as `UNAVAILABLE` | 42 / 42 |
| Those contradictions carrying an explicit future date | 41 / 42 |
| Final rows missing the public apartment label | 87 / 87 |
| Final rows missing a building that the Knock building map resolves | 87 / 87 |

`occupied` describes the unit's current tenancy; it does not negate a
published future availability. The shared Knock parser nevertheless makes it
the status authority. All 42 occupied rows are simultaneously unhidden,
unleased, unreserved, priced, and explicitly `available=true`. The clearest
rendered controls are The Avenue units `13 - 1321` and `1 - 126`: the current
marketing table displays them with future availability on August 13 and August
15, while final output marks both `UNAVAILABLE` solely because Knock says they
are presently occupied. Country Manor shows the same source contradiction on
unit `63 - 44`: the current Knock response says available August 15 while
occupied, and final output says unavailable.

Aspen's current Next.js floor-plan pages also expose a richer first-party
structured roster with a human `floorPlanName`, internal Knock-style plan
code, `unitNumber`, `buildingNumber`, asset/unit/floor-plan IDs, base rent, and
vacant/made-ready dates. Sixty-four current Aspen rows joined deterministically
to the exact Knock property responses by public unit label, building, and
internal plan; 63 also existed in the older canary capture. On all 63 retained
joins:

- Aspen's human plan name differs from the internal layout text shipped as
  `floor_plan_name`;
- the public apartment label is dropped even though the parser extracted it;
- the building is dropped even though `units_data.buildings` resolves the
  unit's `buildingId`; and
- Aspen `madeReadyDate`, Knock `availableOn`, and the final explicit date agree.

The date extraction is therefore clean on these joins; the presentation of
current readiness is not. Twenty-seven of the 63 dates were already reached by
the August 1 capture, and all 27 remain stale historical dates in final output.
Rendered controls prove the marketing UI converts this state to `Available
Now`: Adley at 72nd units `G - G301` and `G - G201`, and The Avenue units
`4 - 425` and `13 - 1323`, all display `Available Now` while final output keeps
May/July dates with `historical_embedded` provenance. The other 36 joined dates
are future dates and remain exact. The required change is therefore narrow:
normalize an explicit visible current state, while preserving every explicit
future date exactly.

The rendered controls also prove that rent cannot be repaired by blindly
choosing one existing field. Aspen exposes base rent, Knock price, and a
move-in/term matrix; the displayed `Starting At` price can differ from both and
changes with move-in date and lease term. No Aspen rent defect is asserted in
this finding until the product contract chooses a coherent visible tuple.

### Fallback reconciliation gaps

The direct Aspen parser still targets the former
`.aspen-c-full-width-card`/`.aspen-c-unit-row` markup. The current pages expose
Next.js structured data and a rendered `Available Apartments` table, so the
production L1 path falls through to Knock instead of preserving Aspen's richer
fields.

Two exact controls show why that fallback cannot be accepted without a
marketing-source reconciliation:

- Edgewood Court's current `The Willow` and `The Adler` pages both say `Call
  For Pricing` and publish no available-apartment roster. Knock still emits a
  priced `Common 1` row with a historical September 2025 date, and the canary
  publishes it as a current unit. This does not prove the physical apartment
  is nonexistent; it proves the fallback-only row is not corroborated by the
  operator's current published roster.
- At the live Country Manor control, `The Sunrise` rendered an empty
  available-apartment table while the exact Knock endpoint still returned
  unit `44` as available August 15. The route conflict must be recorded and
  resolved, rather than whichever response runs last silently winning.

Adley at 72nd separately demonstrates degraded plan output. Its exact current
catalogue contains `The Duke`, `The Essex`, and `The Monarch`, while the canary
adds only two generic plan rows named `2 Bedroom` and `3 Bedroom`, both missing
bath and area and carrying a capture-date default. Those plan rows must be
reconciled with the exact three-plan catalogue rather than published beside the
seven Knock units as unrelated evidence.

### Reachable code path

- `ma_poc/pms/adapters/aspensquare.py:56-106` searches only the legacy card and
  unit-row selectors; the current Next.js structured roster is ignored.
- `ma_poc/pms/adapters/aspensquare.py:330-353` makes the Knock response the
  winning L1 output whenever it returns eligible rows.
- `ma_poc/pms/adapters/knock.py:282-287` builds a layout map but ignores the
  response's complete building map; `:382` reads only a null per-unit
  `buildingName`.
- `ma_poc/pms/adapters/knock.py:321-342` extracts the public label, then marks
  every occupied row unavailable despite an explicit source-available flag.
- `ma_poc/scripts/runners/jugnu.py:3326-3327` lets the native UUID replace the
  public label as `unit_id`, while `:3594-3603` retains a public label only if
  the adapter separately populated `unit_name`.

Required behavior is to keep the stable Knock UUID as canonical identity while
also retaining Aspen's public apartment label, building, human plan name, and
source asset/unit/floor-plan IDs. An explicit Aspen/Knock future date remains
byte-for-byte exact. A rendered `Available Now` state uses the capture date and
records that provenance. `available=true` plus an eligible future offering
cannot become unavailable merely because the current tenant has not yet moved
out. A fallback-only roster must be marked unverified or withheld when the
exact marketing plan publishes an explicit empty/waitlist/call-for-pricing
state. Regression must cover the complete eight properties and separately
protect non-Aspen Knock behavior from a broad status-rule change.

### Local implementation and independent replay (2026-08-02)

The fix is implemented on the Codex branch and replayed through the actual
adapter and production Jugnu formatter against all eight current first-party
pages and all eight exact Knock responses. The current mutable source now has
29 exact plans, 64 displayed apartments, and 87 eligible Knock rows:

| Current complete-cohort result | Count |
|---|---:|
| Stable eligible Knock UUIDs before reconciliation | 87 |
| Admitted unique physical UUIDs | 86 / 86 |
| Explicitly withheld behind Edgewood's exact empty roster | 1 |
| Exact Aspen apartment joins by building + label + internal plan | 63 |
| Knock rows outside Aspen's capped display window, explicitly flagged | 23 |
| Admitted rows with public label, resolved building, and human Aspen plan | 86 / 86 |
| Displayed reached dates emitted with `available_now` provenance | 27 / 27 |
| Admitted explicit future dates preserved exactly | 52 / 52 |
| Remaining historical dates | 7, all fallback-only and not rewritten |

The join deliberately requires building, public label, **and** internal plan.
Waters Edge currently has five distinct Knock UUIDs sharing the same visible
`building 11 / unit 103` pair across sibling layouts; a two-field join falsely
copied one Aspen record five times, while the three-field rule produces the
source-proven 63 exact joins and leaves the other rows marked as outside the
public display window.

The shared Knock change is also live-controlled outside Aspen. Bridgepoint,
Signal Pointe, Arbor Park, and Rockbrook Creek currently expose 77 eligible
rows total. All 77 retain the public label and building; all explicitly say
`available=true`; and the 40 presently occupied future offerings now remain
`AVAILABLE` instead of having current tenancy reverse source availability.
False/null behavior is unchanged and pinned by separate regressions. Focused
GCP canary remains the release gate.

## 39. EdificeCMS keeps exact values but reverses future status and mixes sibling plans

### Verdict

All five attributed EdificeCMS properties were live-probed through their
current marketing HTML, exact floor-plan catalogue, every active per-plan unit
response, and strict-canary final output. The dedicated adapter's identity,
row, and value preservation are clean across the complete cohort:

| Complete current cohort | Source | Strict-canary final | Exact |
|---|---:|---:|---:|
| Floor-plan catalogue entries | 70 | — | — |
| Plans with a unit roster | 28 | represented by units | 28 / 28 |
| Physical apartments | 89 | 89 | 89 / 89 native `UnitID`s |
| Plans with zero units | 42 | 42 source-ID-bearing rows | 42 / 42 |
| Unit plan / beds / baths / area | 89 | 89 | 89 / 89 |
| Unit building / floor / rent | 89 | 89 | 89 / 89 |
| Unit availability date | 89 | 89 | 89 / 89 |
| Empty-plan name / dimensions / rent / state | 42 | 42 | 42 / 42 |

The current floor-plan response names also bind to the configured properties
on all five. The HTML-entity decode correctly finds ResMan UUIDs behind
`&amp;`, and the direct unit provenance records a `MATCH` for every winning
catalogue. No cross-property physical apartment is present in the 89-row
roster.

The status calculation contradicts that same source. Thirty of the 89 rows
have `UnitOccupancyStatus="occupied"`, `UnitLeasedStatus="on_notice"`, an
explicit future `AvailDate`/`MadeReadyDate`, and membership in a plan whose
`UnitsAvailable` count exactly includes the returned roster. These are
future-leasable listings, not unavailable units. Final output preserves all 30
future dates exactly but marks every one `UNAVAILABLE`:

| Property | Future on-notice rows emitted `UNAVAILABLE` |
|---|---:|
| Metropolitan at Cityplace | 3 |
| Newport Village Apartments | 5 |
| Cobblestone | 4 |
| Turtle Dove I | 5 |
| Villas At Wylie | 13 |
| **Total** | **30** |

Concrete current examples include Metropolitan unit `4204` (September 8),
Cobblestone unit `2009` (August 16), Turtle Dove I unit `135` (August 9), and
Villas at Wylie unit `1714` (October 8). This is not a date-extraction defect:
the future dates are exact. It is a state defect caused by treating present
occupancy as mutually exclusive with future availability.

Thirty-four other current source rows say `Move in Today!`. The adapter uses
their structured made-ready date and all 34 survive the source-to-final replay.
Their capture-boundary behavior should remain covered, but no missing-future-
date claim is made for them.

### Turtle Dove I plan-channel contamination

Turtle Dove's marketing site explicitly hosts two subproperties and embeds two
vendor UUIDs: Turtle Dove I and Turtle Dove II. The configured property is
Turtle Dove I. The dedicated adapter selects the exact Turtle Dove I response,
whose 24-plan catalogue yields five apartments across four active plans plus 20
correct zero-unit plan rows.

Final output nevertheless contains 49 `floor_plans[]` rows:

- 20 are the correct Edifice rows with Turtle Dove I plan IDs;
- 27 are entry-page generic-API rows with no plan name, bed, bath, rent, or
  source ID—only an area;
- two are generic DOM buckets with no plan name and a manufactured available
  state/date.

The 27 area-only rows reproduce every Turtle Dove I catalogue area, so 24 are
degraded duplicates. Three additional areas—650, 675, and 900 square feet—do
not exist in Turtle Dove I's exact catalogue and match only Turtle Dove II
plans `A1`, `A2`, and `B2`. The generic entry-page pass saw the combined
two-property JavaScript, and the later link-hop union reintroduced sibling
plan shapes after the dedicated adapter had correctly selected Turtle Dove I.
This is plan-channel contamination; the five physical apartments remain bound
to Turtle Dove I.

Newport Village is the important multi-UUID control. Its page also embeds two
phase UUIDs, but the first current response is an aggregate five-plan catalogue
and the second is a strict two-plan subset with the same property name. The
current 26-unit final roster exactly matches the aggregate response and carries
no extra generic plans. A fix must therefore use response containment and
source plan IDs rather than blindly rejecting every second UUID or blindly
unioning every same-name response.

### Reachable code path

- `ma_poc/pms/adapters/edificecms.py:135-143` performs the required HTML-entity
  decode; `:498-541` tests candidates but stops at the first name match.
- `ma_poc/pms/adapters/edificecms.py:246-260` says the returned roster is
  leasable, then makes `occupied` authoritative and converts all 30 future
  on-notice rows to `UNAVAILABLE`.
- `ma_poc/pms/adapters/edificecms.py:566-611` follows only plans with positive
  `UnitsAvailable`; the exact current responses prove those counts include the
  30 future on-notice rows.
- `ma_poc/pms/scraper.py:5367-5432` merges plan rows by displayed values without
  source authority or property identity.
- `ma_poc/pms/scraper.py:7564-7589` explicitly unions entry-page and winning-hop
  plans on the assumption that both channels are real. Turtle Dove disproves
  that assumption when a combined sibling page precedes an exact property-
  bound catalogue.

Required behavior is to treat an `on_notice` row returned inside the exact
positive-availability roster with an explicit future date as `AVAILABLE` while
preserving that date exactly. Truly leased/rented rows without a published
future offering remain negative. When a property-bound hop wins, plan rows with
vendor plan IDs and matching catalogue identity outrank generic area-only/DOM
candidates. Low-confidence entry rows may enrich an exact plan only after an
unambiguous join; they may not be unioned as independent plans. Multi-UUID pages
must record whether a candidate is a distinct sibling, an aggregate, or a strict
subset before any rows are combined.

### Local implementation and independent replay (2026-08-02)

The fix is implemented and replayed through the actual adapter and production
formatter against every current first-party catalogue and per-plan unit
response. The complete source remains 70 plans, 89 physical apartments, and 42
empty plans. All 89 native IDs and mapped values survive; all 42 empty plans
remain exact and negative; and all 30 source rows that are simultaneously
occupied, `on_notice`, future-dated, and included in a positive availability
roster now finish `AVAILABLE` with the date unchanged:

| Property | Future on-notice source rows | Wrong after fix |
|---|---:|---:|
| Metropolitan at Cityplace | 3 | 0 |
| Newport Village Apartments | 5 | 0 |
| Cobblestone | 4 | 0 |
| Turtle Dove I | 5 | 0 |
| Villas At Wylie | 13 | 0 |
| **Total** | **30** | **0** |

The candidate selector now fetches every identity-matched UUID before choosing
a catalogue. Newport's five-plan response is proved to be an aggregate strict
superset of its two-plan phase response and wins deterministically. Turtle Dove
II is rejected by the existing phase-aware identity proof, leaving the exact
24-plan Turtle Dove I catalogue. Once an exact Edifice result wins, the shared
plan merge retains only source-ID-bearing empty plans; generic area-only/DOM
entry rows cannot re-enter as independent plans. The 152-test focused suite is
green. Focused GCP canary remains the release gate.

## 40. MarketApts units are exact, but the shared plan merge turns deposits into rent

### Verdict

All 29 MarketApts-attributed properties were replayed against current public
source, covering the complete strict-canary cohort and every winning template:
one Template A property, 21 Template B properties, two Template C properties,
and five Template D properties. Twenty-seven server-rendered sources replayed
from the downloaded first-party HTML. Embarc at West Jordan and Mountain Ridge
Manor returned a non-semantic shell to direct HTTP, so their rendered plan pages
and all six active unit-detail pages were read in Chrome.

The dedicated adapter's physical-unit and authoritative empty-plan channels are
clean across that complete cohort:

| Complete current cohort | Current source | Strict-canary final | Exact |
|---|---:|---:|---:|
| Retained physical apartments | 188 | 188 | 188 / 188 native labels |
| Unit plan / beds / baths | 188 | 188 | 188 / 188 |
| Unit area semantics / rent / status | 188 | 188 | 188 / 188 |
| Explicit future availability dates | 99 | 99 | 99 / 99 |
| Authoritative no-unit plan rows | 53 | 53 | 53 / 53 |

Sandpiper's 11 current unit rows publish no square footage; final output keeps
area absent rather than inventing a dimension. Immediate `Now`/`Today` rows
move with the capture boundary, which explains the accepted one-day difference
between the July 31 capture and the current page. No explicit future date moved.
Sunrise Station currently publishes one additional apartment—unit `231`, plan
`2x1A`, 740 square feet at $1,910—which appeared after the canary and is therefore
a current inventory change, not a missed canary row.

The six remaining `floor_plans[]` rows do not come from the dedicated
MarketApts plan channel. They are `TIER_3_DOM_GENERIC_PLAN_LEVEL` rows retained
beside the exact winner:

- Ellis Midtown gains four generic rows (`1x1`, `2x1`, `Studio`, and `Studio A`).
  Each says rent is $200, while the current HTML labels every matching $200
  value **Deposit**. The generic `2x1` row also says two baths although the
  current source and exact unit route say one, and all four receive a synthetic
  capture-date availability.
- Riverbank gains generic `Plan1` and `Plan2` rows at $1,000. Current HTML labels
  $1,000 **DEPOSIT** and separately labels the two starting rents **FROM
  $1,420** and **FROM $1,625**. Both generic rows lose beds and area. `Plan2`
  additionally receives the capture date even though its two current physical
  offerings are explicitly future-dated September 4 and September 7.

This is a plan-channel defect only. Ellis's seven and Riverbank's four physical
apartments remain exact, and all other 53 MarketApts plan rows retain their
source names, dimensions, rents, and negative state exactly.

### Reachable code path

- `ma_poc/pms/adapters/marketapts.py:771-887` reads the drill row's rent and
  explicit date separately and produces the exact unit roster demonstrated
  above.
- `ma_poc/pms/scraper.py:5407-5432` de-duplicates plan rows only when every
  displayed identity field agrees; a degraded generic row therefore cannot
  collide with its authoritative counterpart.
- `ma_poc/pms/scraper.py:7564-7590` explicitly unions entry-page and winning-hop
  plan rows on the assumption that both are real. Ellis and Riverbank disprove
  that assumption: the lower-authority entry parse reinterprets a labeled
  deposit as rent after the exact unit route has already won.

Required behavior is to make the winning property-bound adapter authoritative
for both unit and plan semantics. A generic entry row may enrich a winning plan
only after an unambiguous identity join and only with correctly labeled fields;
it may not survive as an independent degraded duplicate. A value labeled
`Deposit` can never populate rent. Plan-level availability/date must be derived
from the authoritative roster or remain unknown, never manufactured merely
because a generic row contains a dollar amount.

### Local implementation and independent replay (2026-08-02)

The shared hop merge now treats a successful `TIER_1_DOM_MARKETAPTS*` result
with physical units or dedicated no-unit plans as authoritative and retains
only its own plan channel. Replaying the retained strict-canary rows through the
real merge removes all four Ellis generic rows and both Riverbank generic rows,
while preserving Ellis's seven physical apartments and four exact empty plans
and Riverbank's four physical apartments unchanged.

The generic DOM parser now walks all dollar candidates and rejects any amount
whose adjacent source label says `Deposit`, on either side of the amount. A
fresh first-party replay confirms the current labels and values: Ellis publishes
four `$200` deposits beside asking rents of $1,450, $1,425, $981, and $1,125;
Riverbank publishes `$1,000` deposits beside `FROM` rents of $1,420 and $1,625.
The corrected parser selects the six asking rents and never the deposits. The
unchanged dedicated adapter currently emits 11 exact Ellis rows (seven physical,
four empty plans) and four exact Riverbank physical rows. The 136-test focused
MarketApts/generic-plan/hop-merge suite is green. Focused GCP canary remains the
release gate.

## 41. MRI preserves native rows and dates but collapses rent ranges; its Knock fallback is lossy

### Verdict

The complete nine-property attributed cohort was replayed through current
source. Eight properties are direct MRI ProspectConnect winners. Each current
portal passed the provider-code, property-name, street, city, state, and ZIP
identity gate, and each ordinary stateful index GET plus search POST returned
HTTP 200.

| Complete direct MRI cohort | Current source | Strict-canary final | Exact |
|---|---:|---:|---:|
| Property-scoped portals | 8 | 8 | 8 / 8 identity gates |
| Physical apartments | 91 | 91 | 91 / 91 native `building:unit` IDs |
| Unit label / building / plan | 91 | 91 | 91 / 91 |
| Beds / baths / area / low rent / lease term / state | 91 | 91 | 91 / 91 semantic values |
| Explicit future availability dates | 52 | 52 | 52 / 52 |
| Published high rent | 91 | 91 | 82 / 91 |

Elmtree's three studio rows say `Studio / 1 Bath`; the parser initially leaves
the numeric bed field empty and the source-backed downstream normalization
emits zero. That is a correct normalization, not one of the nine differences.

The nine actual differences are also all at Elmtree Park. Its search table is
explicitly headed `Rent Range (USD)` and publishes two numbers for every
current row: three studios at `$695.00 – $865.00`, five one-bedrooms at
`$905.00 – $1,205.00`, and one two-bedroom at `$1,005.00 – $1,305.00`.
The hidden term selectors corroborate that these are base-rent ranges across
offered lease terms. Final output keeps all nine low values exactly but sets
`rent_high` equal to the low, losing $170 or $300 from each published range.
The other 82 current MRI rows publish one value and remain exact.

### Bridgepoint's attributed Knock fallback

Bridgepoint I is the ninth property attributed to MRI, but MRI did not win.
The marketing page exposes Knock community `91011ebb76019d4d`; current Knock
metadata binds it to `(BRI) Bridgepoint`, the exact 1500 Monument Road address,
and an explicit MRI application route
`harborgroupmanagement.mriprospectconnect.com/BRI`. The current Knock roster and
the current MRI search both describe the same sole offering: 576 square feet,
one bed/one bath, $995, available August 10.

The sources add decisive context:

- Knock says `available=true`, `occupied=true`, public unit name `807`, stable
  UUID `6f34b85e-3733-4d79-8391-f742a7bf33c7`, and internal layout `1X1`.
- MRI says unit `807`, building `8`, human plan `The Hampton`, 12-month term,
  and `AVAILABLE` on August 10.
- Final fallback output keeps the UUID, dimensions, rent, and date, but drops
  public unit `807`, building `8`, human plan `The Hampton`, and lease term,
  while changing the offering to `UNAVAILABLE` solely because Knock says the
  current occupant has not yet left.

The direct MRI identity gate currently rejects this otherwise property-bound
route because the configured name `Bridgepoint I` contributes the extra token
`i`, while the marketing site, Knock metadata, and MRI heading all say
`Bridgepoint`. The exact address, provider code, and two independent current
vendor responses make this a verified phase-suffix alias, not permission to
relax name matching generally.

### Reachable code path

- `ma_poc/pms/adapters/mri_prospectconnect.py:183-191` returns only the first
  numeric token, and `:223-251` assigns that value to both rent bounds even
  when the source attribute explicitly contains a range.
- `ma_poc/pms/adapters/mri_prospectconnect.py:154-180` requires every configured
  name token, so the otherwise exact Bridgepoint route fails on the isolated
  suffix `I`.
- `ma_poc/pms/adapters/knock.py:321-352` reads public unit name and UUID, then
  makes current occupancy override the source's positive future-availability
  signal. Bridgepoint is a non-Aspen current reproduction of the status and
  public-context loss already scoped in finding 38.

Required behavior is to preserve both endpoints of MRI's labeled rent range;
single-value rows remain unchanged. A property-scoped MRI route published by
already-bound Knock metadata may be considered only after provider code,
address, city/state/ZIP, and normalized name stem all agree. A controlled phase
suffix must not become a general fuzzy-name bypass. The exact MRI result should
outrank the lossy Knock fallback. If Knock remains the only route, a source-
available future offering cannot become unavailable merely because it is still
occupied today, and its public unit/building context must survive alongside the
stable UUID.

### Local implementation and independent replay (2026-08-02)

The MRI parser now reads the first and last values from the provider's labeled
`data-rent-range`, preserves a single value as equal bounds, and rejects an
inverted range. A source-to-production-formatter regression proves the upper
bound survives final output. The identity gate now permits only a trailing
Roman phase suffix (`I` through `V`) to be absent from the provider heading,
and only after the existing provider code, street number/name, city, state, and
ZIP checks all pass; an additional configured stem token still fails closed.

A fresh live replay of all eight original direct properties plus Bridgepoint
returns 92/92 property-bound native rows with zero adapter errors. The original
eight remain 91 rows. Elmtree's nine rows now retain the three published ranges:
three at $695–$865, five at $905–$1,205, and one at $1,005–$1,305; all other 82
original direct rows remain equal-bounded. Bridgepoint now passes the exact MRI
route and emits native `8:807`, public unit `807`, building `8`, plan `The
Hampton`, 576 square feet, 12-month term, `AVAILABLE` August 10, and the current
provider range $995–$1,240. The 32-test MRI/Knock focused suite is green.
Focused GCP canary remains the release gate.

## 42. RentCafe layout-tab's whole-roster shortcut is neither whole nor semantically safe

### Verdict

The complete 12-property attributed cohort was replayed against current
first-party source: all 12 marketing pages, all 12 `/availableunits` pages, all
12 `/floorplans` pages, all 59 discovered plan drills, and Black Hawk's exact
SecureCafe roster. Eleven properties are direct layout-tab routes; Black Hawk
is the canary's SecureCafe fallback control. Every current page is bound to the
configured property by its first-party host, title/name, and address context.
No cross-property row was found.

The code-only path calls `/availableunits` a whole roster and returns as soon as
that page produces one row. Current source disproves both assumptions:

| Complete current cohort | `/availableunits` shortcut | Exact plan-drill union | Difference |
|---|---:|---:|---:|
| Property-scoped physical apartments | 89 | 187 | 98 omitted |
| Explicit future-dated apartments | 45 | 96 | 51 omitted |
| Omitted `Available Now` rows | — | — | 20 |
| Omitted capture-day rows | — | — | 27 |

Nine of 12 properties undercount. The three controls where the shortcut is
complete are Vista Del Sol, Tudor Place, and 27Seventy Lower Heights. The
largest current gaps are Woodland Hills (1 shortcut row versus 29 drill rows),
Wildwood (10 versus 35), Franklin Marlboro (2 versus 18), Goldstone Place (9
versus 19), Jasper House (2 versus 8), and Northview-Southview (5 versus 9).
Black Hawk is 3 versus 9 on the vanity route, Broadway Towers 1 versus 2, and
Harbisons Dairy 2 versus 4. These are not inferred from RP or from a row-count
expectation: every additional apartment has a native displayed label, an
`applyGAClick` row, a property-bound application link, and matching plan-page
context.

The shortcut can also corrupt the rows it does retain. On six properties, 13
current shortcut rows disagree with their exact plan drill while unit label,
area, rent, date, and application identity agree:

| Property | Rows | Shortcut semantics | Exact plan-drill semantics |
|---|---:|---|---|
| Northview-Southview | 5 | `Studio`, 0 beds | `1BR/1BA`, 1 bed |
| Woodland Hills | 1 | `Spruce`, 1 bed | `Bradford`, 2 beds |
| Broadway Towers | 1 | `Broadway Studio`, 0 beds | `Broadway 1 Bedroom`, 1 bed |
| Harbisons Dairy | 2 | `Studio | 1 Bath`, 0 beds | `1 Bed | 1 Bath`, 1 bed |
| Jasper House | 2 | `Studio`, 0 beds | `1 Bed 1 Bath Plans 1-4`, 1 bed |
| Franklin Marlboro | 2 | `CANAL` | `BAYOU` |

The exact drill is authoritative in these conflicts: its URL names the plan,
its visible heading publishes the same plan and bed/bath dimensions, and the
unit's application link carries that plan's vendor identifier. The generic
`/availableunits` page is the sole contradictory surface. The strict canary
already contains the same wrong shortcut semantics for all 13 rows. Broadway
unit `516` is especially diagnostic: final output retains both the correct
one-bedroom row and the conflicting studio row, changing raw unit `516` into
synthetic IDs `f2b83d13-516` and `4c05d75c-516`. The final-ID duplicate counter
therefore reports zero even though one physical apartment was emitted twice.

The retained rows have two additional semantic losses:

- 65 canary apartment rows have no bath even though their exact source plan
  section publishes it: Northview 5, Vista Del Sol 5, Wildwood 25, Woodland
  Hills 1, Broadway 2, Tudor Place 14, Goldstone 9, Jasper House 2, and Franklin
  Marlboro 2. The handler carries plan, beds, area, rent bounds, and unit label;
  bath is adjacent in the plan header, but the parser hard-codes it empty.
- Seven Vista Del Sol rows lose the exact names `2 Bed 1 Bath` or `2 Bed 2
  Bath`, even though the current source handler and plan section publish them.

Date handling on admitted physical rows is not the defect. The canary has 65
explicit-future rows in this cohort; 61 remain in current source and all 61
dates match exactly. The other four apartments are no longer published. The
problem is coverage: the premature shortcut omits 51 additional current
future-dated apartments before their dates can be emitted. Tudor Place's 14
rows currently publish no date, so their capture-date default is not being
misrepresented here as an overwritten future date.

The 31 final `floor_plans[]` rows are a separate shared-plan-channel downgrade.
Current first-party plan cards publish exact names for all 31: eight Woodland
plans (`Spruce` through `Bradford`), two Goldstone plans, ten Harbisons plans,
and eleven Franklin water-themed plans. Final output replaces every name with a
generic `N Bedroom / M Bath` label. Seventeen current cards also publish a rent
range, while the generic rows collapse high to low. All 31 rows receive the
capture date despite retaining `availability_status=UNKNOWN`; the source does
not publish that date as their availability.

Black Hawk's fallback is the clean control. Its current property-bound
SecureCafe page returns the same nine native unit labels, plans, bed/bath
dimensions, areas, and rent bounds as the canary, and both explicit future
dates remain exact. Seven immediate dates advanced by one day on the live page,
which is the expected moving availability boundary rather than a lost future
date.

### Reachable code path

- `ma_poc/pms/adapters/rentcafe_layout_tab.py:486-534` asserts that
  `/availableunits` contains every apartment and immediately returns on any
  non-empty result. The exact drill walk at `:640-657` is therefore unreachable
  in the nine current undercount cases.
- `ma_poc/pms/adapters/rentcafe_layout_tab.py:205-240` parses the handler but
  sets `bathrooms=""`, ignoring the source plan header.
- `ma_poc/pms/adapters/rentcafe_layout_tab.py:314-400` appends browser-plan rows
  without a run-global native-unit reconciliation. The code-only path has a
  separate display-unit dedupe at `:685-695`; Broadway proves the two paths can
  disagree and that post-processing can preserve a conflict by inventing two
  final IDs.
- `ma_poc/pms/scraper.py:5407-5432` and `:7564-7590` union lower-authority
  generic plans beside the winning route, which is how all 31 exact plan names
  become degraded independent rows rather than authoritative source plans.

Required behavior is to treat `/availableunits` as one candidate surface, not
as proof of completeness. Discover the bounded exact plan drills, reconcile by
native property-scoped apartment identity, and let the plan-specific route win
plan/bed/bath conflicts. Preserve both rent bounds and every explicit date.
Use the visible plan header to fill bath only after an exact plan join. Apply
the same reconciliation to browser and code-only paths, so one raw apartment
cannot survive twice under synthetic IDs. Exact plan cards must keep their
source names/ranges; lower-authority generic rows may enrich only after an
unambiguous join and may not manufacture a capture date for an unknown plan.
The clean SecureCafe fallback and the three complete-shortcut controls must
remain unchanged.

### Local implementation and independent replay (2026-08-02)

The layout-tab adapter no longer returns on the first non-empty vanity roster.
It unions the vanity route, any property-linked SecureCafe roster, listing
rows, and every bounded exact plan drill by the native apartment label. Exact
drills have highest semantic authority, SecureCafe is second, and the vanity
surface remains a lower-authority additive fallback. Browser and code-only
paths now share that reconciliation. The exact drill header supplies bath,
source URLs are joined without duplicating `/floorplans`, and every admitted
source response is represented by a body hash, sanitized URL, and admitted
unit count. Rent-only empty plans are `UNKNOWN`; the production floor-plan
formatter leaves their date absent. The shared link-hop merge suppresses
generic plan harvests once this property-bound adapter wins.

A fresh live replay of the complete 12-property cohort through the modified
adapter returns 187/187 native apartments, 187 distinct property-scoped
identities, 187 published baths, zero duplicates, and source-response
provenance accounting for all 187 admitted rows. Woodland Hills uses its
already-vetted warm route `woodlandhillsirving.com`; its obsolete configured
domain now serves a Hover parking page. The other 11 are direct current
marketing routes. All 12 finish with no adapter error.

An independent source-to-final replay over the complete saved first-party
corpus (all 59 plan drills) reproduces the same 187/187 set. It preserves all
96 explicit-future dates, classifies 31 visible immediate rows as
`available_now`, retains 46 source dates equal to capture as
`explicit_capture_date`, and defaults only Tudor Place's 14 genuinely
date-less physical apartments. All 13 measured shortcut conflicts resolve to
the exact drill semantics; Broadway `516` appears once as `Broadway 1
Bedroom`. Replaying the actual strict-canary plan channel suppresses all 31
generic downgraded rows. The broad RentCafe/SecureCafe suite is green at 409
tests; the focused source/reconciliation/formatter suite is green at 129
tests. Focused GCP canary remains the release gate.

## 43. Wix's rigid card grammar misses both visible plans and a linked unit roster

### Verdict

Confirmed coverage defects across the complete three-property
`wix_floor_plans` cohort. The two direct Wix properties finish
`FAILED_NO_DATA` despite current property-bound source data. The third property
is a clean cross-property boundary control that succeeds only through the
AppFolio recovery route.

| Complete current cohort | Current property-bound source | Strict-canary result |
|---|---|---|
| Westerville Park (`47909`) | Three visible plan cards | `FAILED_NO_DATA`, 0 rows |
| The Bellagio (`262964`) | Six plan categories, 25 plan codes, and one current physical unit in the explicitly linked map | `FAILED_NO_DATA`, 0 rows |
| Vestawood (`220345`) | 35 AppFolio cards: 18 Vestawood and 17 Green Springs siblings | `SUCCESS`, exactly 18 Vestawood units |

Westerville's current first-party page has a clearly labeled `View Floor
Plans` section. It publishes `1 Bedroom Garden` (1 bed/1 bath, starting at
$965), `2 Bedroom Garden` (2 bed/1 bath, starting at $1,075), and `2 Bedroom
Townhome` (2 bed/2 bath, starting at $1,390). Each says `Contact for
Availability`. The page does not publish square footage or a unit roster, so
the justified recovery is three inquiry/unknown plan rows—not invented physical
apartments and not `AVAILABLE` plans. The adapter rejects all three solely
because its grammar requires a pipe-delimited square-foot field.

Bellagio provides stronger unit-level evidence. The August 1 first-party
floor-plan capture publishes 25 named codes (`X01` through `X25`) and six
priced plan categories:

| Published category | Current rent range |
|---|---:|
| Studio | $1,499-$1,699 |
| 1 Bedroom & 1 Bath | $1,599-$1,999 |
| 2 Bedroom & 2 Bath | $1,899-$2,399 |
| 3 Bedroom & 3 Bath | $2,799-$3,199 |
| 2 Bedroom Penthouse | $1,999-$3,199 |
| 1 Bedroom Penthouse | $1,699-$2,399 |

The same page twice labels and links an external map as `View Map of Available
Units`. The link contains property ID
`640c40c9-6c72-4c16-b6d3-4a996fcff013`; the rendered property map currently
publishes one apartment: unit `509 - Mountain Scenic View`, plan `X09`, 1
bed/1 bath, 892 square feet, $2,089, available `14 Aug`. Captured on August 1,
2026, that is an explicit future offering. The unit route is not an inferred
portfolio link: the exact Bellagio page labels it as its available-unit map,
and plan `X09` is also present in Bellagio's own 25-code catalogue. The canary
nevertheless returns no plan or unit rows.

Vestawood proves that broad link following is not safe and that the existing
property boundary can work. The current nested AppFolio widget explicitly says
it may include nearby sister properties and returns 35 listings: 18 headed
`Vestawood Apartments - Vestavia Hills, AL` and 17 headed `Green Springs
Village Apartments`. The canary keeps exactly the 18 Vestawood listing UUIDs,
addresses, rents, bed/bath values, areas, and the three explicit future dates,
while excluding every Green Springs card. The remaining 15 source rows say
`NOW`; their moving capture-date representation is not evidence of a missed
future date here.

### Reachable code path

- `ma_poc/pms/adapters/wix_floor_plans.py:45-76` accepts a card only when it
  contains `Starting at`, pipe-delimited bed/bath fields, and square footage.
  Westerville has the same labeled semantics in a colon-based card without
  area, while Bellagio publishes category ranges rather than that one template.
- `ma_poc/pms/adapters/wix_floor_plans.py:85-135` repeats the same mandatory
  area/single-starting-rent assumptions in the Python parser.
- `ma_poc/pms/adapters/wix_floor_plans.py:186-214` reads only the current DOM
  payload and never considers an explicitly labeled, property-bound available-
  unit link. The module-level claim that Wix sites do not typically publish a
  unit roster is therefore not a safe route boundary.
- `ma_poc/pms/adapters/_appfolio_embed.py:520-580` is the clean Vestawood
  control: it scopes the mixed widget by its operator-published property group,
  retains stable `listable_uid`, and rejects the 17 sibling cards.

Required behavior is to parse bounded Wix plan sections with labeled
bed/bath/rent fields even when area is absent or rent is a range. Preserve
missing area as absent and map `Contact for Availability` to inquiry/unknown,
not `AVAILABLE`. Discover an outbound unit route only when the exact
first-party property page explicitly labels it as that property's availability
surface; bind the returned source back to the configured property and catalogue
before admitting rows. Add a narrowly identified 3DPlans route for the exact
map shape rather than treating every Wix link as inventory. The Vestawood
property-title/UUID scope is a mandatory regression control and must remain
18-of-35, never account-wide.

### August 2 source drift and local remediation

A fresh August 2 first-party replay found that Bellagio now exposes two Wix
pages with the same six category names but different ranges. The nav-labeled
`PRICING` page publishes `$1,799-$1,899`, `$1,899-$2,299`,
`$2,399-$2,599`, `$3,299-$3,499`, `$2,899-$3,699`, and
`$2,399-$2,599`; the nav-labeled `FLOOR PLANS` page still contains the six
older ranges in the table above, plus the 25 authored codes and the labeled
3DPlans link. The current 3DPlans roster still publishes native unit `989`
(`509 - Mountain Scenic View`), plan `X09`, 1 bed/1 bath, 892 square feet,
$2,089, available `2026-08-14`. That unit falls inside the current 1-bedroom
pricing range and outside the older floor-plan-page range. Source-role
reconciliation therefore uses `PRICING` for duplicate category rents and
`FLOOR PLANS` for the catalogue/map boundary; it emits six categories, never
twelve.

The remediated adapter now emits Westerville's exact three plans as
`UNKNOWN`, with no area or date and with a verified plan-only proof marker.
It discovers only operator-labeled same-site floor-plan/pricing pages and only
an exact labeled 3DPlans map. A map row must match the linked GUID, returned
property id/name/address/marketing host, configured property identity, and an
authored Wix plan code before admission. Bellagio's current unit retains
native unit/floor-plan/property/location ids, its explicit future date, and
the exact unit-producing response hash/count/identity provenance. Vestawood's
Wix route remains empty, and the unchanged AppFolio property-group test keeps
exactly 18 target listings while rejecting all 17 Green Springs siblings.
The combined Wix/AppFolio suite is green at 48 tests and the source-id registry
at 50 tests. Focused GCP canary remains pending.

## 44. Camden reports a suggestion sample as unit inventory; its older expansion fabricates rows

### Verdict

Confirmed a complete-cohort unit-coverage defect in the registered Camden
adapter and a separate semantic defect in the older reachable Camden fallback.
All 16 attributed properties were read from current first-party source: 16
landing pages, all 16 `/available-apartments` catalogues, and every one of the
182 exact plan-detail pages linked by those catalogues. All requests returned
current property-bound data with zero fetch or parse failure.

| Complete current cohort | Landing-page suggestions | Exact plan catalogues | Exact plan-detail union |
|---|---:|---:|---:|
| Plans/representatives | 130 | 182 | 182 |
| Physical apartments | 130 emitted | — | 531 |
| Explicit future dates | 130 | — | 531 |
| Floors / lease terms | 0 / 0 emitted | — | 531 / 531 published |

The current registered adapter therefore omits 401 of 531 physical apartments
(75.5%). The gap has two independent parts:

- 52 current plan categories with 141 physical units are not present in the
  landing page's capped `suggestedFloorPlans` array at all.
- Within the 130 suggested plans, exact drill pages publish 390 physical units;
  the adapter keeps one representative per plan and omits the other 260.

This is current first-party evidence, not an RP row-count comparison. Each of
the 531 drill rows publishes a property-bound plan, public unit label, native
unit ID, `realPageCommunityId`, base monthly rent, explicit move-in date,
square footage, floor, and lease term. The source has 531 unique public unit
labels and 531 unique `realPageCommunityId:unitId` composites. Every published
date is after the August 1 capture boundary.

The 130 representatives themselves are value-clean in the current snapshot:
all 130 native IDs, plans, rents, dimensions, and dates agree between the
landing-page suggestion and its exact detail row. The strict canary also
contains 130 rows, and 117 still agree byte-for-byte with the current moving
inventory; the other 13 are ordinary live changes. Thus this is not a claim
that retained representative rents or dates are wrong. It is a claim that a
suggestion sample is being mislabeled as the physical roster. The adapter also
drops all 130 source-published lease terms. On Fairview, Ashburn Farm,
Overlook, Manor Park, and Farmers Market, 33 representative rows additionally
replace a building-qualified source label such as `8726 - 3D` with bare `3D`.

Fallsgrove plan `1.1E` is a compact exact reproduction. The landing page keeps
only representative unit `4140`, $2,189, available August 3, native ID `104`.
Its exact first-party plan page publishes both that row and unit `2041`, which
is $2,229 with native ID `44`. The current Fallsgrove catalogue has 14 physical
apartments across six plans; the adapter emits six.

Camden North End proves that bare vendor IDs are not sufficient across a
multi-community property. Three current `unitId` values collide while the
community-qualified identities remain unique: for example, unit ID `100` is
both community `4700479` apartment `1236` and community `4282428` apartment
`10209`. Four floor-plan IDs are also reused across the two communities: IDs
`2`, `7`, `8`, and `13` each name two different plan slugs. Exact drill rows
bind these correctly through `realPageCommunityId`, plan slug, and property
route; output currently preserves none of the community identity.

### The older reachable Camden fallback is not a safe shortcut

`ma_poc/pms/adapters/_camden.py` expands each suggestion's
`availableUnitIds`, but copies the one representative's rent, date, and
`realPageUnitId` onto every expanded label. Replaying that function against the
same current 16 landing pages produces 396 rows. Exact plan drills show:

| Reachable fallback replay | Rows |
|---|---:|
| Expanded rows | 396 |
| Labels that belong to the claimed plan | 386 |
| North End labels assigned to the wrong/missing plan | 10 |
| Rows carrying the wrong native unit ID | 263 |
| Rows carrying the wrong base rent | 232 |
| Rows carrying the wrong availability date | 215 |
| Rows losing a building-qualified public label | 112 |
| Duplicate extras in repeated representative source-ID pairs | 266 |

The same fallback sets `rent_high` from `totalMonthlyRent` even though its own
code identifies that field as fee-inclusive. On 394 of the 396 current replay
rows it is greater than base monthly rent. This route did not win the strict
Camden canary, so these 396 rows are a reachable-risk replay, not canary output.
It must be removed or redirected to exact detail parsing rather than promoted
as the coverage fix.

### Reachable code path

- `ma_poc/pms/adapters/camden.py:59-116` reads exactly one
  `unitNumber`, rent, date, and native ID from each landing-page suggestion and
  ignores both `availableUnitIds` and source `leaseTerm`.
- `ma_poc/pms/adapters/camden.py:126-170` parses only the already-fetched
  landing body. It never follows the first-party `/available-apartments`
  catalogue or its bounded exact plan pages.
- `ma_poc/pms/adapters/_camden.py:188-255` performs the unsafe cross-product:
  every available label receives the representative's values and source ID,
  and fee-inclusive total becomes rent high.
- That older parser remains reachable through
  `ma_poc/pms/adapters/generic.py:2845-2878` and
  `ma_poc/pms/adapters/generic_plan_text.py:1508-1543` when the dedicated route
  does not win.

Required behavior is to treat `suggestedFloorPlans` only as discovery, never
as roster completeness. Fetch the exact property-bound
`/available-apartments` catalogue, walk its bounded unique plan slugs, and
parse `props.pageProps.data.floorPlan.units` from each detail response. Use
`realPageCommunityId:unitId` as the property-scoped native anchor; retain the
community, native floor-plan ID, slug, full public unit label, floor, lease
term, rent, and explicit date. Keep `monthlyRent` as base rent and record any
fee-inclusive total separately. Remove the representative cross-product, and
fail or report a partial roster explicitly if a required detail page cannot be
validated.

### Local remediation status (2026-08-02)

Implemented on the Codex branch. The registered adapter now fetches the exact
catalogue and all advertised plan details with a hard 28-plan bound and four-
request concurrency cap. Catalogue and detail responses must agree with the
configured Camden host/route, returned city/community slugs, community
name/address, plan slug/name/native ID, and exact response path. A configured
identity mismatch, redirect, malformed plan, duplicate composite, failed
detail, or post-process loss suppresses the entire physical roster and emits
explicit incomplete-walk telemetry.

The old `_camden` representative expansion now returns no rows, and both
generic call sites were removed. Base `monthlyRent` is equal-bounded; an
independent `totalMonthlyRent`, if Camden starts publishing it on detail-unit
rows, is retained only as `rent_including_fees`. New source provenance keeps
the community-qualified unit ID, community-qualified plan ID, raw plan ID,
plan slug, exact detail-response union hash/count, and property binding.

Complete captured-cohort adapter-to-production-formatter replay is exact:
16/16 properties, 182/182 plan responses, and 531/531 unique physical units,
with zero identity, plan, label, dimensions, base-rent, date, or lease-term
differences from source. All 531 dates remain `explicit_future`; all 160
building-qualified labels survive; 520 positive floors publish exactly and
the 11 provider `floorNumber=0` sentinels correctly normalize to null. The
focused 71-test Camden/source-ID suite is green. Fresh direct live checks on
Fairview, Fallsgrove, and multi-community North End returned 10/14/63 unique
exact rows with all dates/floors/terms and no errors. Focused GCP canary remains
pending.

## 45. Squarespace's fixed-route recovery misses two authored rosters and admits a SightMap placeholder

### Verdict

Confirmed on the complete six-property attributed cohort. `squarespace_nopms`
is a shell adapter rather than one inventory family, so each exact downstream
surface was checked independently against current first-party source. Four
properties have a valid physical-unit route; two current property pages expose
recoverable inventory that the canary misses or replaces with a synthetic plan.

| Property | Current property-bound source | Strict canary | Current disposition |
|---|---:|---:|---|
| Cricket Flats (`241432`) | 8 own-page apartments | 8 units + 4 supplemental plan rows | The eight physical rows, rents, descriptive plan names, and visible availability tokens are exact. The supplemental generic plan rows are not additional source apartments and receive synthetic capture dates. |
| The Landmark (`56903`) | 5 own-page apartments | `FAILED_NO_DATA` | Confirmed extraction miss. |
| Tribeca (`57195`) | LeaseLeads unit roster | 21 units | Current rendered source confirms the 15-plan catalogue and exact sampled native units, dates, dimensions, floors, and lease-term prices. |
| 30Sixty Apartments (`68505`) | 1 scoped AppFolio apartment | 1 synthetic plan-level row | Confirmed route miss and wrong result shape/value. |
| 250 High (`61950`) | 11 physical SightMap apartments + 12 legitimate empty plans | 11 units + 13 plan rows | All physical rows and legitimate plan-presence rows replay exactly; the thirteenth plan is an internal `TEMP` placeholder. |
| Town Center Apartments (`280355`) | 17 scoped ManageBuilding apartments | 17 units | Exact native listing IDs, labels, dimensions, rents, and all 11 future dates; 15 plan names are correctly joined by exact bed/area and two unmatched shapes remain unnamed. |

This cohort also closes the primary availability-date question without using
RP as truth. Across Cricket Flats, 250 High, and Town Center, all 22 current
explicit future dates replay exactly. Cricket's two `Available Now` rows and
Town Center's six visible August 1 rows are represented by the moving capture
boundary; that one-day UTC/local edge is not a missed future date. Tribeca's
current Barclay 1 drill independently confirms units `344WT`, `343WT`, and
`244WT`, their September/November dates, best-price rents, floors, and lease
terms. Selecting six months in the current UI reproduces the canary's high
rent of $2,598 for `344WT` and $1,995 for `244WT`, proving those high values
are term-range prices rather than fee contamination.

### Exact failure reproductions

The Landmark's current first-party page groups each apartment in one
`figure.sqs-block-image-figure` caption. The five complete captions are:

| Unit | Beds / baths | Area | Rent | Visible availability |
|---|---|---:|---:|---|
| `203` | 1 / 1 | 860 | $2,600 | Immediate |
| `212` | 1 / 1 | 936 | $2,600 | Immediate |
| `218` | 1 / 1 | 973 | $2,575 | after 6/15/26 |
| `304` | 1 / 1 | 864 | $2,626 | Immediate |
| `317` | 1 / 1 | 973 | $2,700 | on or after 6/15/26 |

The canary fetched a valid rendered body with five rent signals but emitted
zero rows. The dates on 218 and 317 are now historical page content and should
be preserved with `historical_embedded` provenance; they are not future-date
claims.

30Sixty's exact authored `/availability-copy` page initializes
`carterres.appfolio.com` with property group `30Sixty Apts`. The current scoped
SSR roster contains listing `554`: apartment `522`, 2 beds/2 baths, 883 square
feet, $3,100, available now, at the configured 3060 W. Olympic Blvd address.
The canary never follows that authored route and instead reports a synthetic
plan named `Labeled Property Rent`, $1,940, with no beds, baths, area, date, or
source ID. That row is neither the current apartment nor a source floor-plan
card.

250 High's exact page embeds SightMap `rxwjjqlxw1e`; its current API identifies
asset `24278`, name `250 High`, and returns 11 physical units plus 22 historical
floor-plan records. The current parser correctly deduplicates those plans by
name and emits 12 legitimate names with no current unit. It also emits provider
record `442397`, whose entire semantic payload is name `TEMP`, 0 bedrooms,
0 bathrooms, no unit, no area, no rent, and no date. That is an internal
placeholder, not a client-facing floor plan.

### Reachable code path

- `ma_poc/pms/adapters/squarespace_nopms.py:39-64` delegates to the shared
  universal recovery and otherwise declares the site syndication-only.
- `ma_poc/pms/adapters/_avail_table_recovery.py:241-299` accepts only a
  `white-space:pre-wrap` paragraph that itself starts with `Unit <digits>` and
  contains rent. That correctly handles Cricket but cannot see Landmark's
  `Landmark # <unit>` figure captions, where dimensions, rent, and availability
  are separate paragraphs inside the same property-bound figure.
- `ma_poc/pms/adapters/_appfolio_embed.py:104-133` probes a fixed list of
  guessed subpaths. It does not follow 30Sixty's exact same-origin authored
  `/availability-copy` link, so it never sees the AppFolio host or property
  group.
- `ma_poc/pms/adapters/generic_plan_text.py:1077-1094` can then turn a bare
  labeled price into `Labeled Property Rent`, allowing the wrong plan-level
  fallback to mask the missed unit route.
- `ma_poc/pms/adapters/sightmap.py:330-367` emits every remaining named
  unitless floor-plan record. It has no degenerate/internal-name gate, so
  `TEMP` survives as an unavailable plan.
- The clean controls are intentionally strict: LeaseLeads binds UUID, domain,
  name, street, city, and ZIP before admitting units; ManageBuilding requires
  one authored tenant, exact account/property label, and matching city/state/
  ZIP on every card.

Required behavior is to discover at most a bounded set of same-origin,
operator-authored inventory links from the captured Squarespace navigation,
then run the existing downstream identity gates on those exact pages. Add a
narrow figure-caption parser requiring unit label, dimensions, rent, and
availability inside the same Squarespace figure for Landmark's shape. A known
physical route must outrank the generic labeled-price fallback. Reject a
SightMap plan-presence record when it is a provable internal placeholder such
as `TEMP` with zero dimensions and no unit, area, rent, or date. Preserve the
Cricket, LeaseLeads, and ManageBuilding controls exactly, including future-date
provenance and property identity.

### Local remediation and validation status

Implemented on `codex/consolidated-canary-2026-08-01`. The Squarespace owner
now performs a maximum-two, visible-label, same-host authored-route pass before
the ordinary universal chain. Only `Availability`, `Available Apartments`,
`Pricing`, or `Floor Plans` anchors qualify; the fetch is direct HTTP with the
paid unlocker/proxy path disabled, validates the redirect host, caps the body
at 3 MB, and passes the resulting page through the existing AppFolio,
LeaseLeads, SightMap, or ManageBuilding property-identity gate. A physical win
returns before generic price text can create `Labeled Property Rent`.

The own-page parser now accepts Landmark's figure only when the *same figure*
contains all five independent signals: `Landmark #<unit>`, beds/baths, area,
positive monthly rent, and `Immediate` or an explicit date. It emits no floor
plan name because the source publishes none. The SightMap catalogue pass now
rejects only an unjoined plan whose exact provider name is `TEMP`, whose beds
and baths are explicitly `0/0`, and which has no area, rent, or date field;
`TEMP` by itself is deliberately insufficient.

The complete pinned first-party replay is exact after the change:

| Property | Post-fix pinned replay |
|---|---:|
| Cricket Flats | 8 physical / 0 supplemental plans |
| The Landmark | 5 physical / 0 plans |
| Tribeca | 21 physical / 0 plans |
| 30Sixty | 1 physical (`522`, listing `554`, 883 sqft, $3,100, `NOW`) / 0 synthetic plans |
| 250 High | 11 physical + 12 legitimate empty plans; provider record `442397` absent |
| Town Center | 17 physical / 0 plans |

The independent direct live adapter replay on 2026-08-02 also passes all six:
Cricket `8`, Landmark `5`, Tribeca `21`, 30Sixty `1`, and Town Center `17`.
250 High's live source legitimately changed from the pinned split to `12`
physical + `11` empty plans while retaining the same `23` total records; the
formerly empty plan now has a current apartment, and no `TEMP` record appears.
This is labelled as live source drift, not credited as a parser gain. Landmark
source-to-production-formatter tests prove `Immediate -> available_now` and
`6/15/26 -> historical_embedded`; the provider controls retain their raw
future-date values.

The complete focused provider regression is green (`601 passed` across all
AppFolio, LeaseLeads, ManageBuilding, SightMap, Squarespace unit-block, new
authored-route, and new Landmark-figure suites). Ruff, Python compilation, and
the full saved-payload replay are green. Strict focused GCP canary remains the
acceptance gate.

## 46. ThinkReside misses its current towncommunity card shape and erases `Now` provenance

### Verdict

Confirmed on the complete two-property attributed cohort. Indy Flats is an
exact physical-roster control for the dedicated adapter; Deer Run is a current
plan-only shape that the dedicated adapter rejects and the shared fallback
then overproduces.

| Property | Current property-bound source | Strict canary | Current disposition |
|---|---:|---:|---|
| Indy Flats (`271195`) | 46 physical apartments across 14 plan pages, plus 7 exact no-unit plans | 46 units + 7 plan rows | All 46 `(plan slug, apartment label)` identities and all current plan, area, rent, and date values replay exactly. Eight explicit future dates remain exact. The 38 visible `Now` tokens are normalized too early and mislabeled `explicit_capture_date` instead of `available_now`. |
| Deer Run (`51921`) | 4 plan cards, no physical roster and no availability statement | 7 plan rows | Confirmed duplication, area loss, and fabricated availability dates. |

Indy Flats' current source has 38 `Now` rows and eight explicit future rows:
five dated August 25, plus one each on August 4, August 8, and August 29. The
final output preserves all eight future dates exactly. The 38 immediate rows
become the UTC capture date, August 2, even though the Chicago capture boundary
was still August 1, and their provenance becomes `explicit_capture_date`.
This is not a missed future-date defect, but it is exactly the timezone and
provenance ambiguity the shared availability fix is intended to remove.

Repeated apartment labels at Indy Flats are not evidence of row
contamination. Labels `201`, `205`, `207`, `210`, and `308` occur on distinct
source plan slugs; the canary preserves all 46 rows and both source components.
The named Barbee, Jordan, Sherwood, and Windsor plan families remain visible
in `floor_plan_name`, so this control must stay row-complete while availability
provenance is corrected.

### Exact Deer Run reproduction

The current first-party `/floorplans` page publishes exactly four cards:

| Source plan | Beds / baths | Source area | Source rent |
|---|---|---:|---:|
| 2 Bdrm 1.5 Bath -Ranch or Split Ranch Style | 2 / 1.5 | 1,050 | $1,430 |
| One Bedroom - Ranch Style | 1 / 1 | 728 | $1,225 |
| Two Bedroom 1.5 Bath - Garden Style | 2 / 1.5 | 1,150 | $1,420-$1,430 |
| Two Bedroom 2 Bath - Ranch or Garden Style | 2 / 2 | 1,050-1,150 | $1,450 |

Neither the index nor any of the four exact detail pages publishes a unit
table, available-unit count, date, `Now`, `Available`, or other affirmative
inventory statement. The truthful output is therefore exactly four
plan-presence rows with source dimensions/rent ranges, stable plan slugs,
`UNKNOWN` availability, and no date.

The strict canary instead emits the four named rows plus three generic rows:
two copies of `2 Bedroom / 1.5 Bath` and one `2 Bedroom / 2 Bath`. All seven
have area `-1`, no source ID, and `capture_date_default=2026-08-02`. The four
named rows are marked `AVAILABLE` solely because a positive starting rent is
present; the three generic rows remain `UNKNOWN` but still receive the same
fabricated date. The retained events show the dedicated route ending
`TIER_1_DOM_THINKRESIDE_NO_PLANS`, followed by the winning shared
`TIER_3_DOM_GENERIC` fallback.

### Reachable code path

- `ma_poc/pms/adapters/thinkreside.py:221-367` implements only Pattern A
  `<li data-beds>` and Pattern B `.floorplan-item` cards. Deer Run's current
  Pattern C cards are `li.floorplan` with dimensions and rent in child
  elements, so the documented towncommunity shape returns no plans.
- `ma_poc/pms/adapters/thinkreside.py:619-640` retries `/floorplans` through
  the same unsupported parser and then returns `THINKRESIDE_NO_PLANS`, which
  permits the shared generic plan fallbacks to win and overlap.
- `ma_poc/pms/adapters/thinkreside.py:472-516` treats any positive plan rent as
  proof of `AVAILABLE`; a catalogue price is not evidence that a physical
  apartment is currently available.
- `ma_poc/pms/adapters/thinkreside.py:151-163` converts the visible `Now`
  sentinel to `datetime.now(UTC).date()` inside the adapter. By the formatter
  boundary the source token is gone, so the output cannot distinguish
  `available_now` from an explicit ISO capture date.

Required behavior is to parse Deer Run's property-bound `li.floorplan` cards
directly, preserving exact names, ranges, dimensions, detail slugs, and source
order. A plan without an available-unit count or physical row must remain
`UNKNOWN` and undated; positive rent alone is not availability. Once the
dedicated current shape succeeds, suppress overlapping generic plan rows.
For physical unit tables, preserve the raw `Now` sentinel or explicit
availability provenance through the formatter so normalization uses the run's
capture date and records `available_now`; explicit future dates must pass
through unchanged.

The focused existing ThinkReside suite remains green (`23 passed`), but it has
no current Deer Run Pattern C fixture and asserts only the already-normalized
ISO value for `Now`, so it does not cover either confirmed defect.

### Implemented and independently replayed

The dedicated adapter now parses the current `li.floorplan` shape only when a
card has `div.floorplan-details`, an exact authored name, at least two
structured dimensions, and exactly one same-property `/floorplans/{slug}`
link. Cross-property and ambiguous recommendation links are rejected. The
parser retains source order, the exact visible area string (including
`1,050 - 1,150`), numeric area bounds, both rent bounds, and the exact detail
slug. Catalogue price without a physical row or explicit available-unit count
now remains `UNKNOWN`; it does not imply availability. The internal first-party
fetch explicitly sets `unlocker=False`.

The availability boundary now passes the raw `Now` token to the production
formatter. The complete saved-source replay returns Indy Flats as exactly 46
physical rows plus seven legitimate empty plans, with 46 unique `(plan slug,
apartment label)` identities. The five repeated labels remain distinct (`201`
2x, `205` 3x, `207` 2x, `210` 2x, and `308` 2x). All 38 immediate rows become
the run capture date with `available_now` provenance, and the eight explicit
future dates remain exact (`2026-08-04` 1x, `2026-08-08` 1x,
`2026-08-25` 5x, `2026-08-29` 1x).

The same replay returns Deer Run as exactly four plan rows and no physical
units or generic supplement. Final rows preserve the four names and slugs,
areas `1050`, `728`, `1150`, and `1050` (with the last source range retained
in `area_raw`), and rent bounds `$1,430`, `$1,225`, `$1,420-$1,430`, and
`$1,450`. Every plan is `UNKNOWN`, undated, and has `missing` date provenance.

Direct first-party HTTP replay, with proxy and Web Unlocker credentials
explicitly absent, matches the saved evidence on Indy Flats and Deer Run. A
third current ThinkReside control, Ridge at Perry Bend, emits five exact
undated `UNKNOWN` plan rows (`Apex`, `Element`, `Fusion`, `Solo`, `Vertex`).
The former Orchard Ridge control no longer carries a ThinkReside fingerprint.
Upton Oxmoor still carries the marketing-platform fingerprint but is correctly
routed at higher confidence to its authored RentCafe/SecureCafe portal, so its
ThinkReside `NO_PLANS` result is not an adapter miss.

The expanded ThinkReside suite is green (`29 passed`); the combined
ThinkReside, availability-contract, zero-inventory, and plan-level boundary
set is green (`149 passed`). Ruff and compilation are green. Strict focused
GCP canary remains the acceptance gate.

## 47. `wix_nopms` mixes clean provider routes with lossy plan parsing and four recoverable misses

### Verdict

Confirmed on the complete 18-property attributed cohort. Wix is only the
marketing shell: seven properties publish a structured provider route, four
publish plan-level Wix content, and seven emitted no data. Those three groups
must be judged independently rather than assigning one semantic meaning to
`wix_nopms`.

| Current source group | Properties | Current source | Strict canary | Disposition |
|---|---:|---:|---:|---|
| AppFolio | Stadium (`19538`), Harbor Vista (`67150`), The Marq (`69203`), Eagle Harbor II (`217343`), Allure (`240745`) | 55 current public cards | 55 units | Native listable UIDs and every mapped value replay exactly, but The Marq includes one application-only “waiting list” card that is not a physical apartment. |
| DoorLoop | Park Place (`254556`) | 9 address-bound apartments | 9 units | Clean current control: all native listing/property IDs, labels, dimensions, rents, and dates are exact. |
| SightMap | The Parkline (`276351`) | 3 physical apartments plus 14 unitless provider plan records | 3 units + 14 plans | The three physical units are exact. One unitless record is an internal `TEMP` placeholder, and shape-based marketing enrichment assigns the wrong rent range to plans `H` and `M`. |
| Wix plan content | Arcos (`23963`), Constellation Ranch (`34523`), Stoney Creek (`36268`), Gentry's Landing (`37805`) | 27 authored plan cards | 26 plans | Confirmed plan-name/identity loss and property-specific value errors. All 26 output rows also receive a capture date despite no source availability statement. |
| Recoverable failed output | Indian Village (`23494`), Westgate Village (`71345`), Allen Ranch (`282696`), Millennium on Monroe (`271721`) | 5 current plan rows plus 4 physical apartments | `FAILED_NO_DATA` on all four | Confirmed misses. The property-bound source is current and sufficient for honest plan/unit output. |
| Defensible current no-data | East Hampton (`46179`), 16 Bennett (`118965`), Hoyt Tower (`263732`) | No current published inventory | `FAILED_NO_DATA` on all three | Current no-data result is defensible; do not manufacture rows from bedroom-mix prose, parking prices, or application material. |

The provider-backed total is 67 canary unit rows. Sixty-six are current
physical apartments; the remaining row is The Marq's waitlist application.
Parkline accounts for the cohort's other 14 provider plan rows. The four
Wix-plan sites account for the remaining 26 canary plan rows.

### Provider-route controls and exact defects

The five exact current AppFolio URLs reproduce all 55 canary
`appfolio_listable_uid` values with zero mapped field differences. The Marq's
UID `96f414c2-063b-44b9-8d1e-d01bd95ee172` is nevertheless not an apartment:
its listing title is `Apply for our 1brm waiting list`, it has no apartment
label, and numeric listing ID `2385` is synthesized as the unit number. The
final row is marked as an available physical 1-bedroom unit. The code intends
to reject waitlist cards, but `_WAITLIST_RE` recognizes `wait list` and
`wait-list`, not the current phrase `waiting list`.

Park Place's exact DoorLoop company route currently returns nine listings, all
bound to `305 W Jack Finney Blvd., Greenville, TX 75402` and source property
name `Park Place Luxury Apartments`. Current and canary native listing IDs,
property IDs, values, and dates match exactly. This is the clean provider and
property-identity control.

Parkline's authored page embeds SightMap `r5v51orjwny`, whose exact current API
returns apartments `210`, `417`, and `520` with the same source IDs, plan
names, areas, rents, and dates as the canary. The API also contains plan ID
`410367`, name `TEMP`, 0 beds, 0 baths, and no unit, area, rent, or date; the
canary emits it as a plan. This independently reproduces the placeholder defect
in finding 45. The marketing page publishes seven category ranges, but the
generic join gives plan `H` (0 bed/1 bath) the 1-bedroom range
`$2,500-$3,486` instead of the studio range `$2,242-$2,359`, and gives plan
`M` (1 bed/1.5 bath) that same 1-bedroom range instead of the published
1.5-bath range `$3,000-$3,999`. These are current source-to-final
contradictions, not judgments against RP.

### Wix plan-content reproductions

The current Arcos Wix data store publishes three stable CMS records:

| Plan | Beds / baths | Area | Deposit | Starting rent |
|---|---|---:|---:|---:|
| `A1` | 1 / 1 | 534 | $500 | $1,199 |
| `A2` | 1 / 1 | 560 | $500 | $1,199 |
| `S1` | Studio / 1 | 424 | $500 | $999 |

The canary emits only two generic `1 Bedroom / 1 Bath` plans, uses the $500
deposit as rent, loses the `A1`/`A2` identities, and drops `S1` entirely.
The exact CMS rows remain in `appsWarmupData.dataBinding.dataStore`, including
their stable `_id`, `unitName`, `price`, `deposit`, `sqFt`, bed, and bath
fields; the loss is in extraction rather than the source.

Constellation currently publishes nine named cards: Electra; Vega; Vega With
Garage; Hudson; Loadstar; Neptune; Galaxy; Hercules; and Hercules With Garage.
The canary preserves all nine bed/bath/area/rent combinations but replaces the
names with generic bed/bath labels. Distinct source plans consequently share
the same synthetic `floor_plan_id`, have no source ID, and receive fabricated
capture dates.

Stoney Creek currently publishes nine named phase/style cards. The canary
emits ten generic plans, loses all phase/style identity, and cross-associates
values: three 1-bedroom rows all use 944 square feet although the source areas
are 944, 768, and 900; three 2-bedroom/1-bath rows all use 1,128 although the
source areas are 1,128, 988, and 1,100; and two false 2-bedroom/2-bath rows
replace the one 1,536-square-foot den plan. Its Wix data stores expose the
phase, plan title, price, and exact descriptive area within the same record.

Gentry's current authored page publishes six plan/style records and both
unfurnished rent ranges and furnished rents. The canary emits five generic
bedroom categories, drops the second 1-bedroom convertible, loses style names,
collides repeated IDs, and reduces every range to its lower unfurnished value.

None of these four current sources publishes a physical apartment label or an
affirmative availability date/count. Their correct ceiling is therefore
source-faithful, `UNKNOWN`, undated plan-level output—not unit conversion and
not `AVAILABLE` rows.

### Four exact failed-output misses

- Indian Village currently publishes `1 Bedroom Apartment`, 535 square feet,
  $1,000 rent and $1,200 deposit, plus `2 Bedroom Apartment`, 740 square feet,
  $1,300-$1,500 rent and $1,500 deposit. The truthful result is two undated
  plan rows.
- Westgate's exact authored `/onebedroom` and `/twobedroom` routes currently
  publish 1 bed/1 bath, 680 square feet, `$1,150-$1,200`, and 2 bed/1.5 baths,
  1,297 square feet, `$1,600-$1,850`. `Deposit: Varies` and accessory parking,
  garage, washer/dryer, cleaning, admin, and application charges are not rent.
  The truthful result is two undated plan rows.
- Allen Ranch currently says `Now Available!` for a 3-bedroom/2.5-bath,
  1,360-square-foot plan at $1,400 per month with an $800 refundable deposit.
  It supports one currently available plan-level row, not a fabricated
  physical unit.
- Millennium's exact `/properties-for-rent` page embeds a same-site
  `filesusr.com` component, which publishes the authored
  `newmpm.appfolio.com/listings` index. The current index contains 11 cards;
  exactly four match configured address `2002 N Monroe St`: apartments `102`,
  `405`, and `307` available now, plus apartment `409`, 2 beds/2 baths,
  1,045 square feet, $2,190, available 8/9/26. The other seven cards are
  different addresses and must be rejected. The canary itself resolved this
  AppFolio tenant in retained telemetry but emitted no rows.

Millennium's component omits `propertyGroup`, so the current AppFolio bridge
declines it before applying the already-required configured-address boundary.
The safe recovery is not to admit the 11-card portfolio. Treat this exact
operator-authored index as published route evidence, require the configured
street/city/state/ZIP on every accepted card, require stable listable UIDs, and
record both the four admitted and seven rejected identities in provenance.

### Defensible no-data controls

East Hampton's rendered `FLOOR PLANS` section currently contains a `Table
Master` iframe with no `src`, no `srcdoc`, and no document body after the page
settles. The page publishes only the broad 1/2/3-bedroom marketing mix and
instructs visitors to contact leasing. 16 Bennett's rendered `AVAILABILITY`
section has the same empty widget state; its only prices are $250/$400 parking
charges. Hoyt Tower's current home, actual application
`/webinar-registration`, and community material publish only a studio/1/2-bed
mix, contact/application fields, and amenities—no plan dimensions, unit
identity, rent, or availability roster. These are negative controls for every
Wix recovery change.

### Reachable code path

- `ma_poc/pms/adapters/wix_nopms.py:35-64` delegates to universal recovery and
  otherwise returns `SYNDICATION_ONLY_WIX`; Wix itself has no inventory
  semantics.
- `ma_poc/pms/adapters/_appfolio_embed.py:215` uses
  `\bwait\s*-?\s*list\b`, which cannot match The Marq's current `waiting list`
  text. Lines 500-577 therefore classify that card as a physical listing.
- `_appfolio_embed.py:353-370` requires a `propertyGroup` inside a Wix
  component. Millennium's operator-authored component supplies the exact
  AppFolio host/index but no group, so the address-filterable route is dropped.
- `ma_poc/pms/adapters/_html_extract.py:2937-3030` flattens a card to text,
  explicitly returns an empty `floor_plan_name`, and loses source field
  boundaries. An amount-before-label deposit such as Arcos `$500 ... Deposit`
  falls outside the label-first deposit exclusion. The final generic DOM
  records therefore lose authored names/IDs and can bind the wrong money or
  area field.
- `ma_poc/pms/adapters/_html_extract.py:3764-3774` deduplicates unnamed rows by
  mutable rent/area/bed values before a source plan identity exists. The
  formatter later synthesizes the same plan ID for different source plans
  sharing a generic name/shape.
- `ma_poc/pms/adapters/sightmap.py:330-367` admits every remaining named
  provider plan, including the provable `TEMP` placeholder already covered by
  finding 45.

Required behavior is to route by the authored provider first; reject waitlist
applications and cross-property cards before post-processing; and preserve the
actual unit-producing response as provenance. For native Wix plan content,
parse bounded repeated records/cards with their field labels and stable CMS or
card identity instead of flattening the whole card. Source plan name/ID,
deposit, rent low/high, area/range, beds, and baths must stay associated inside
one record. A plan without physical inventory or affirmative availability must
remain `UNKNOWN` and undated. Keep East Hampton, 16 Bennett, and Hoyt at honest
no-data unless their current sources begin publishing qualifying inventory.

The focused AppFolio embed, Wix iframe, HTML extraction, generic plan-text, and
SightMap suites remain green (`187 passed`), but they do not contain The Marq
`waiting list` phrase, Millennium's no-`propertyGroup` authored component,
the four current Wix record/card shapes, or the three rendered no-data
controls.

### Implemented and locally validated on 2026-08-02

The consolidated Codex branch now covers each omitted fixture explicitly. The
waitlist classifier rejects `waiting list` while retaining the rejected UID;
the no-`propertyGroup` Wix bridge treats the authored AppFolio index only as
route evidence and then admits four exact-address Millennium cards while
rejecting seven sibling addresses; the bounded Wix record parser preserves all
27 authored plan identities and values; Indian Village, Westgate, and Allen
Ranch produce the five expected plan rows; and the three no-data controls
remain empty. The focused AppFolio/Wix/SightMap set is green (`81 passed`). A
strict focused GCP canary is still required before this finding is release-
verified.

## 48. Yotta preserves its apartment roster and future dates but loses plan, floor, and `Today` semantics

### Verdict

Confirmed on the complete three-property attributed cohort and on a fresh
end-to-end replay of all three exact public DBA routes. The property boundary
is strong and the physical inventory is clean: all 58 native `unitId` values,
public unit numbers, bed/bath counts, areas, rents, raw floor labels, and all
41 explicit future dates agree with the current provider responses. No source
row is missing or extra.

| Property | Exact DBA | Current/canary units | Provider plan types -> published plan IDs | Provider `Today` rows | Explicit future dates |
|---|---:|---:|---:|---:|---:|
| The Redland (`35349`) | `200` | 27 / 27 | 7 -> 4 | 7 | 20 / 20 exact |
| Verandah at Lakepointe (`15049`) | `201` | 18 / 18 | 5 -> 3 | 9 | 9 / 9 exact |
| Pepper Tree (`34785`) | `55` | 13 / 13 | 5 -> 3 | 1 | 12 / 12 exact |
| **Total** | — | **58 / 58** | **17 -> 10** | **17** | **41 / 41 exact** |

The current `GetDBADetails` responses also exactly match each configured DBA,
property name, street, city, state, and ZIP. Fresh adapter-to-formatter runs
made six ordinary public API calls, admitted the same 27/18/13 rows, and
returned no errors. This rules out RP assumptions and a stale canary-only
explanation.

Three output losses remain:

1. Every current row publishes `floorLevel` as `First Floor`, `Second Floor`,
   or `Third Floor`. All 58 labels reach `floor_raw`, but all 58 published
   `floor` values are null because the formatter recognizes digits only.
2. Yotta publishes stable `dbaUnitTypeId` and `dbaUnitTypeCode` on every row.
   The adapter keeps only the generic `dbaUnitType` text and only
   `yotta_unit_id` in `source_ids`. Redland's `A1`, `A2`, and `A3`, for
   example, all become `1 Bed/1 Bath` and the same final hash. Across the
   cohort, 17 provider plan types collapse to 10 published plan IDs. The
   stable DBA ID is assigned to transient `source_property_id` but is also
   absent from final `source_ids`.
3. Seventeen current rows explicitly say `dateAvailable: Today`. The adapter
   prefers Yotta's dynamically generated ISO `MoveInDateAvailable` and drops
   the semantic token. In the retained canary, all 17 consequently appear as
   `historical_embedded`; in a fresh same-UTC-date replay they appear as
   `explicit_capture_date`. Neither records the source's actual
   `available_now` meaning. This does not affect the 41 source-backed future
   dates, all of which survive exactly.

The final property provenance also reports an empty `unit_source` list even
though the adapter used the exact `GetFloorPlans/{dba}/1` response. The API
URL is present only in adapter-time state; the actual unit-producing response
is not recorded in the emitted property or unit rows.

### Reachable code path

- `ma_poc/pms/adapters/yotta.py:148-163` requires the provider DBA ID plus
  configured name, street, city, state, and ZIP before inventory is fetched.
  All three current properties pass that exact boundary.
- `yotta.py:173-182` selects `MoveInDateAvailable` before
  `dateAvailable`, so a literal `Today` token is discarded before availability
  provenance is classified.
- `yotta.py:206-208` selects generic `dbaUnitType` before the distinguishing
  `dbaUnitTypeCode`; lines 218-236 preserve only `yotta_unit_id`, while
  `dbaUnitTypeId` and the DBA ID never reach final `source_ids`.
- `ma_poc/scripts/runners/jugnu.py:3421-3433` recomputes plan identity from
  property/name/beds/baths rather than a provider plan anchor. Distinct Yotta
  codes with the same generic label therefore collapse.
- `jugnu.py:4731-4750` parses only a numeric floor token. The provider's
  exact word ordinals remain recoverable in `floor_raw` but publish as null.
- `YottaAdapter.extract` records sanitized API-response telemetry but never
  populates `unit_source_provenance`, which explains the empty emitted
  `unit_source` list.

Required behavior is additive and property-bound: retain `yotta_dba_id`,
`yotta_floor_plan_id`, `yotta_floor_plan_code`, and `yotta_unit_id`; use the
provider plan anchor so distinct source layouts remain distinct; recognize
bounded word-ordinal floors without deleting the raw label; preserve `Today`
through the formatter so it becomes the capture date with `available_now`
provenance; and record the exact unit-producing response URL/count/identity
verdict. Explicit future dates must remain byte-for-byte unchanged.

The existing Yotta suite remains green (`9 passed`) and covers property
identity, native unit IDs, rents, and future dates. It does not assert the
three provider plan IDs/codes, word-ordinal floor output, `Today` provenance,
DBA provenance, or the final unit-producing response.

### Implemented and independently replayed on 2026-08-02

The adapter now retains the DBA, unit, plan ID, plan code, and separate plan
description; uses the property-qualified provider plan anchor; preserves a
literal `Today`; recognizes bounded word-ordinal floors in both production
formatters; and records only sanitized/hash response provenance. A fresh direct
adapter-to-production-formatter replay of DBAs `200`, `201`, and `55` returned
27/18/13 units, 7/5/5 distinct plan IDs, 58/58 normalized floors with raw
labels, 17/17 `available_now` rows, 41/41 `explicit_future` rows, and one
property-MATCH response record per DBA covering every admitted row. The
combined Yotta/date/source-ID closure suite is included in the green 321-test
final-finding set. Strict focused GCP canary remains pending.

## 49. Non-registry recoveries preserve physical rows, but ShowMojo loses a future date and the static table drops five plans

### Verdict

Confirmed on the complete four-property cohort for every non-registry output
owner. Fresh exact-source recovery followed by the production formatter
reproduces every canary physical row with zero differences in identity, plan
name, beds, baths, area, rent, final date, or status. That canary equality is
not a clean-source verdict by itself: it also reproduces two current
source-to-final omissions.

| Output owner / property | Current source boundary | Current/canary physical rows | Exact result | Confirmed omission |
|---|---|---:|---|---|
| BetterNOI / Vista Pointe (`55709`) | One page-published client UUID and three published floor-plan UUIDs; exact street/city/state boundary | 9 / 9 | All native unit IDs, plan IDs/names, dimensions, rents, statuses, and dates are exact | Emitted `unit_source` is empty despite the exact API response. |
| NestHub / Annaberg (`1765`) | Configured unavailable listing -> exact community -> 33-row manager roster -> exact-address detail revalidation | 1 / 1 | Only native listing `602`, unit `E7`, survives; plan, dimensions, $1,160 rent, and 8/19/26 date are exact | Emitted `unit_source` is empty despite the exact detail response. |
| ShowMojo / Park Northside (`38378`) | Configured identity -> reciprocal manager -> 52-row official account; 13 accepted and 39 rejected | 13 / 13 | All 13 native listing UIDs, addresses, dimensions, and rents are exact | One future date is replaced by the capture date; 12 current tokens lose provenance; emitted roster responses are absent. |
| Static residence table / 1515 Park Place (`261580`) | One exact property-bound server-rendered availability table | 3 / 3 | Residences `102`, `101`, and `103`, their bed/bath values, and $3,000/$4,500/$4,300 rents are exact | Five plan/stack rows and all floor-plan links are discarded; emitted page provenance is absent. |

### ShowMojo future-date contradiction

The current official ShowMojo card and its native detail page both say
`Available September 7th` for listing UID `e7c39f1061`, address
`1617 Brookfield St`. The source capture occurred on 2026-08-01 in
America/Chicago, so this is the next September 7 and is unambiguously future
inventory. The canary publishes `2026-08-02` with
`capture_date_default`. The other 12 accepted Park Northside rows explicitly
say `Available now`; their date value is correctly the capture date, but the
output also labels them `capture_date_default` rather than `available_now`.

The adapter already extracts each exact `availability_text`, then deliberately
passes an empty `availability_date` to `make_unit_dict`. The raw token is kept
only on an adapter-time key that the production formatter does not read or
emit. Passing the source string unchanged would still not be sufficient:
the current shared date parser treats `Available September 7th` as
`available_now`, while the same text without the ordinal suffix parses to
`2026-09-07`. The fix therefore needs both source-token plumbing and bounded
ordinal normalization.

### Static-table plan loss

The current 1515 Park Place table publishes eight bounded rows. Three are
physical residences and replay exactly. Five are clearly unitless stack/plan
summaries with distinct source identity and plan-image links:

| Source stack | Beds / baths | Published rent | Exact plan asset |
|---|---|---:|---|
| `205-805` | 1 / 1 | $2,300-$2,450 | `/images/floorplans/1bed/205.jpg` |
| `206-806` | 1 / 1 | $2,300-$2,450 | `/images/floorplans/1bed/206.jpg` |
| `303-803` | 1 / 1 | $2,200-$2,300 | `/images/floorplans/1bed/303.jpg` |
| `307-807` | 2 / 1 | $2,750-$3,000 | `/images/floorplans/2bed/307.jpg` |
| `201-801` | 3 / 2 | $3,500-$3,700 | `/images/floorplans/3bed/201.jpg` |

The current parser correctly refuses to manufacture these ranges as physical
units, but then discards them instead of routing them to `floor_plans[]`.
They support five source-faithful, undated, `UNKNOWN` plan rows with stable
stack/asset identity. The physical rows should remain exactly three; positive
catalogue rent does not make a stack an available apartment.

### Reachable code path

- `ma_poc/pms/adapters/_showmojo_public.py:_parse_card` extracts the exact
  `availability_text`, but `recover_showmojo_public` passes
  `availability_date=""` and stores the token only on a private recovery row.
  The formatter consequently manufactures a capture-date default.
- `ma_poc/core/schema_v2.py:_format_date` recognizes the non-ordinal
  `Available September 7` shape but currently misclassifies
  `Available September 7th` as `available_now`.
- `ma_poc/pms/adapters/_static_residence_table.py:181-184` recognizes a
  numeric stack range and immediately `continue`s. Its exact bed/bath, rent
  range, stack code, and floor-plan link never reach the plan channel.
- BetterNOI retains native unit/client/floor-plan IDs on every row, ShowMojo
  retains its account/listing/application IDs, and NestHub retains its native
  listing ID. However, these row-list recovery helpers return bare unit lists,
  and the winning path does not populate `unit_source_provenance`; all four
  final property records therefore report `unit_source: []`.

Required behavior is narrow: preserve the ShowMojo token, normalize bounded
English ordinal month/day forms against capture time, and classify `now`
explicitly; route the five static stack records to the plan channel without
changing the three physical units; and bridge each winner's already-known
exact response URL/count/property verdict into sanitized unit-source
provenance. BetterNOI and NestHub require no current row/value/date change.

The focused non-registry suites remain green (`42 passed`). They cover the
property boundaries and current row parsers, but do not assert the production
formatter result for ShowMojo's ordinal future token, the static plan channel,
or final property-level unit-source provenance.

### Implemented and independently replayed on 2026-08-02

The shared date formatter now handles bounded English ordinal suffixes without
destroying the untouched raw token; ShowMojo passes its exact availability
text; the static-table recovery emits separate physical and plan channels; and
all four bare-list owners bridge only their exact unit-producing response into
sanitized/hash provenance. A fresh direct Park Northside source-to-production-
formatter replay returned 13/13 unique native listings: 12 `Available now`
rows became the 2026-08-02 capture date with `available_now`, while UID
`e7c39f1061` preserved `Available September 7th` and emitted `2026-09-07` with
`explicit_future`. Two MATCH roster hashes account for all 13 admitted rows.
The retained/live BetterNOI, NestHub, and 1515 Park Place replays preserve
their physical rows; the static result additionally carries all five exact
`UNKNOWN`, undated stack plans. The Yotta/non-registry closure set is green
(`321 passed`). Strict focused GCP canary remains pending.

## Evidence and safety notes

- Direct Avalon and UDR pages were fetched without CAPTCHA solving or stealth.
- RentCafe Applicant endpoints returned Cloudflare 403 to plain HTTP and were
  read through isolated Hyperbrowser test sessions with proxy enabled,
  stealth disabled, and CAPTCHA solving disabled.
- Entrata marketing pages returned 403 to plain HTTP and were rendered in
  isolated Hyperbrowser test sessions with proxy enabled, stealth disabled,
  and CAPTCHA solving disabled.
- Lake Haven, Rose Park Commons, Birch Pond, the three ResMan/Razz controls,
  the three SightMap controls, the four Apts247 controls, and the three Funnel
  Spaces controls, plus all six RealPage GetUnits controls, were reachable by
  ordinary first-party HTTP or Playwright. The Repli360 controls used their
  public first-party-authored widget endpoints, the MAAC controls used MAAC's
  public first-party JSON and embedded state, and the Jonah controls used
  exact first-party server-rendered resource/unit-data JSON. Twelve Irvine
  pages and all 13 Irvine rank responses were direct; Santa Clara Square's
  current 403 page shell was read in one bounded clean Hyperbrowser session
  with proxy enabled, stealth and CAPTCHA solving disabled. No bypass tier was
  used for findings 19-33 beyond that explicitly recorded page-shell fetch.
- Equity finding 33 used ordinary first-party HTTP for all 26 current page
  checks. Fifteen pages exposed 181 current server-rendered source rows; ten
  current 403 pages remain explicitly unexercised rather than being treated as
  clean or defective, and one page remains a non-unit redirect.
- Essex finding 34 used ordinary first-party page and bulk-API requests for all
  27 current properties. Nine omitted raw canary artifacts were downloaded
  once into the local audit cache; eight are explicit 404 shells and one is a
  valid property page. No bypass service was used.
- FortressTech finding 35 used ordinary first-party marketing pages and their
  exact linked SSR widgets for the complete ten-property cohort. No bypass
  service was used.
- ResidentServices365 finding 36 used ordinary first-party server-rendered
  floor-plan and unit-detail pages for the complete ten-property cohort. All
  72 detail pages were public and within the production adapter's existing
  bound; no bypass service was used.
- RentalAddress finding 37 used one ordinary first-party HTTP download of the
  complete current Cedar Ridge plan page. No bypass service was used.
- AspenSquare finding 38 used all 29 current first-party plan pages, all eight
  exact public Knock property responses, and rendered controls on Adley at
  72nd, Country Manor, and The Avenue. No CAPTCHA, proxy, or bypass service was
  used.
- EdificeCMS finding 39 used all five current marketing pages, all five exact
  primary catalogues, both multi-UUID control catalogues, and all 28 active
  per-plan unit responses. The public vendor API was reached directly; no
  CAPTCHA, proxy, or bypass service was used.
- MarketApts finding 40 downloaded the complete 29-property current cohort and
  every reachable first-party unit drill into the local audit cache. The two
  JavaScript-rendered D-template shells were read in the user's ordinary Chrome
  session. No CAPTCHA, proxy, unlocker, or evasion mechanism was used.
- MRI finding 41 used ordinary public GET/POST sessions for all eight direct
  property-scoped portals and Bridgepoint's exact MRI control, plus the public
  Knock community/unit responses linked by Bridgepoint's current page. No
  CAPTCHA, proxy, unlocker, or evasion mechanism was used.
- RentCafe layout-tab finding 42 downloaded the complete 12-property current
  cohort, all 59 exact first-party plan drills, and Black Hawk's property-bound
  SecureCafe roster into the local audit cache. Requests were direct; no
  CAPTCHA, proxy, unlocker, or fingerprint rotation was used.
- Wix finding 43 used all three current first-party Wix pages, Vestawood's
  nested public AppFolio widget, and Bellagio's explicitly linked public
  3DPlans map. Direct HTTP was used for the pages and ordinary Chrome for the
  two rendered widgets. No CAPTCHA, proxy, unlocker, or fingerprint rotation
  was used.
- Camden finding 44 downloaded all 16 current landing pages, all 16 exact
  availability catalogues, and all 182 property-bound plan-detail pages into
  the local audit cache. Every request was ordinary first-party HTTP; no
  CAPTCHA, proxy, unlocker, browser evasion, or fingerprint rotation was used.
- Squarespace finding 45 downloaded all six current first-party entry pages and
  their exact authored inventory pages into the local audit cache. Cricket,
  Landmark, 30Sixty/AppFolio, 250 High/SightMap, and Town Center/ManageBuilding
  were read by ordinary public HTTP. Tribeca's rendered LeaseLeads UI was
  inspected in the user's ordinary Chrome session. No CAPTCHA, proxy,
  Hyperbrowser, unlocker, fingerprint rotation, or browser evasion was used.
- ThinkReside finding 46 downloaded both current first-party entry pages, both
  exact floor-plan indexes, all 21 Indy Flats plan drills, and all four Deer
  Run plan drills into the local audit cache. All requests were ordinary
  public HTTP; no CAPTCHA, proxy, Hyperbrowser, unlocker, fingerprint rotation,
  or browser evasion was used.
- Wix-no-PMS finding 47 audited all 18 current first-party entry pages and
  their exact authored provider or plan routes from the local cache. Ordinary
  public HTTP replayed the AppFolio, DoorLoop, SightMap, and static Wix
  sources; the user's ordinary Chrome session verified the four
  JavaScript/iframe states. No CAPTCHA, proxy, Hyperbrowser, unlocker,
  fingerprint rotation, or browser evasion was used.
- Yotta finding 48 downloaded all three exact current `GetDBADetails` and
  `GetFloorPlans` responses to the local audit cache, then replayed every row
  through the live adapter and production formatter. All requests were
  ordinary public property-bound API calls; no browser, CAPTCHA, proxy,
  Hyperbrowser, unlocker, fingerprint rotation, or evasion mechanism was used.
- Non-registry finding 49 downloaded the four configured pages, Vista's exact
  floor-plan/API responses, Park Northside's official manager/ShowMojo pages,
  Annaberg's first-party NestHub chain, and 1515 Park Place's static table to
  the local audit cache. All requests were ordinary public HTTP; no browser,
  CAPTCHA, proxy, Hyperbrowser, unlocker, fingerprint rotation, or evasion
  mechanism was used.
- No CAPTCHA, unlocker, fingerprint rotation, or browser-evasion mechanism was
  added to production code.
- No production profile or run output was changed as part of this audit.
