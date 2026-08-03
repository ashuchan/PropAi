# Affected-386 output fixes — 2026-08-02

## Evidence baseline

Source run: `jugnu-affected386-33864eb` / execution
`jugnu-affected386-33864eb-2bwp8`.

- 386/386 properties completed; 352 strict unit-level successes (91.19%).
- 9,729 rows were present in `units[]`.
- 78 rows across 12 properties had a non-empty `floor_plan_name_raw` but a
  null normalized `floor_plan_name`.
- Three `SIGHTMAP_PLAN_PRESENCE` catalogue rows across two properties carried
  `inferred_*` IDs inside `units[]` even though they were not apartments.
- 136 rows across 15 properties emitted legacy `area=-1`: 133 physical rows
  plus the three SightMap catalogue markers. Same-day captured source replay
  found exact or bounded published area evidence for 95 of the 133 physical
  rows before the Waterline alias collapse; 38 physical rows remain genuinely
  unresolved under the evidence gate.
- 1,156 physical-unit rows already carried different `rent_low` and
  `rent_high` values. This proves that a range is a valid unit-level source
  shape (lease term and move-in choices can vary) and must not be collapsed to
  one asking-rent scalar.
- 177 rows across nine properties had a collision-safe `unit_id` different
  from the source value in `unit_id_raw`.
- No row exposed a standalone `building_id`; 3,943 rows did expose a building
  label or provider building code through `building`.
- The required stage/identity diagnostic fields in
  `affected-property-manifest-v1/future_launch_contract.json` were absent.
- Source response bodies, record locators, and pre-format/final row snapshots
  were not linked by one offline replay manifest, so many formatter defects
  still required a new live probe.
- Availability-date behavior passed the audit: all 9,729 rows had provenance,
  all source future dates were preserved, and there were zero semantic
  contradictions. No availability-date transform was changed in this patch.

## Implemented changes

### Provider floor-plan labels

A closed provenance allow-list now preserves labels only when an
identity-gated adapter explicitly read the provider's label. The affected run
supports recovery of 68 of the 78 formatter-loss rows across nine properties:

- Camden exact detail: 50 rows
- MRI ProspectConnect: 6 rows
- RentCafe layout/available-units: 10 rows
- MarketApts: 2 rows

The remaining ten rows stay scrubbed deliberately: seven unproven Equity
numeric codes, two generic `70747` values, and one generic embedded `2.1`
without bounded provider provenance. This avoids weakening generic hygiene.

### Identity and daily history

The output now exposes:

- `source_unit_id`: the provider/display value before disambiguation
- `building_id` and `building_id_source`: standalone building identity
- `canonical_unit_id`: the existing collision-safe `unit_id`
- `unit_history_key`: property-scoped SHA-256 daily-merge key
- `unit_history_key_basis`, `unit_history_key_quality`, and
  `unit_history_key_version`: auditable, versioned key basis

The existing `unit_id` is not weakened or de-composed, so cross-building and
cross-plan duplicates remain collision-safe. An offline projection over the
affected-386 output produced 9,726/9,726 populated, unique history keys after
removing the three catalogue markers:

- 3,447 `provider_native_stable`
- 1,785 `building_and_floor_plan_scoped_source_id`
- 4,494 `floor_plan_scoped_source_id`

Revision `0014_units_output_identity` persists the identity fields as
standalone nullable columns in the current-state `units` table. Both the SQL
and filesystem state stores preserve them through readback and failed-run
carry-forward. The current `(canonical_id, unit_id)` primary key remains in
place: the history SHA is indexed for comparison, but will not become merge
identity until a two-day continuity replay proves that it is stable when
inventory and prices change.

The same persistence boundary now retains additive extracted-unit fields in
the existing `extra` JSON catch-all. This complements, rather than duplicates,
the full property-level raw-response archive below: `extra` describes the
latest interpreted unit; the content-addressed source manifest preserves the
producing response.

The Excel exporter also surfaces source/canonical/history identity, standalone
building identity, raw date + date provenance, plan provenance, and the
null-native area fields; it no longer writes the legacy `area=-1` sentinel into
the `area_sqft` column. The TypeScript SQL/JSON state providers and unit API
transform expose the same companions in camelCase rather than dropping them.

### Plan markers and area

Only exact `SIGHTMAP_PLAN_PRESENCE` rows move from `units[]` to
`floor_plans[]`. The two affected properties are Tisdale at Lakeline Station
and The Parker. Physical SightMap units are unchanged.

V2's legacy `area=-1` wire contract remains backward-compatible. New consumers
can use `area_sqft` (`null` for a range or when unpublished), `area_low`,
`area_high`, `area_range`, `area_value_type`, and `area_is_published`. A
published range is never replaced with a midpoint. The existing
`area_absence` taxonomy remains the explanation for missing sqft.

The evidence-gated replay over the 136 original `area=-1` rows now accounts
for every row:

- 83 raw rows receive exact source-published values. Waterline contributes 41
  raw matches because `321` and `0321` are a proven duplicate; the existing
  leading-zero alias rule then collapses those two identical rows, leaving 82
  unique exact physical units.
- 12 physical rows retain honest published ranges: Sandpiper 4, Fairmount 1,
  Coventry Square 5, and Mill Creek 2.
- Three SightMap plan-presence rows move to `floor_plans[]` and never receive a
  synthetic apartment identity.
- 38 physical rows remain null: Siena Villas 4, Westerville Park 3, Fenway 9,
  Cricket Flats 8, Irondale 6, three stale One Clinton rows, two stale
  Waterline rows, and three Coventry rows whose output bath shape conflicts
  with the current marketing catalogue.

Admission is deliberately narrow:

- An alternate apartment roster needs at least three collision-free display
  label joins, or a complete unique rent/bed/bath bijection of at least three
  rows. One Clinton, Waterline, and 70 Pine satisfy those gates.
- A plan surface can supply an exact value only from one matching plan; several
  valid plans of the same shape produce a min/max range. Sandpiper, Fairmount,
  and Coventry use this rule.
- 1515 Park Place's 101/102/103 values are bound to the exact visually verified
  floor-plan asset SHA-256 values (1,271 / 950 / 1,124), not OCR or filename
  inference.
- Mill Creek uses an Apts247 catalogue only after the community endpoint and
  every returned plan independently match the configured property identity. The
  provider exposes two 2-bed/1-bath plans (905 and 916 sqft) but no shared key
  to the On-Site unit plan label, so both units retain `905-916` rather than a
  guessed scalar.

The Apts247 rule was live-checked on three separate public properties before
generalization: Mill Creek Apartments, Crossings at Berkley Square, and Fox
Run. Sibling identity mismatch, malformed JSON, unsupported response shape,
and insufficient label evidence all fail closed and are archived for offline
inspection.

### Physical-unit rent ranges

The formatter now emits `rent_low`, `rent_high`, `rent_range`,
`rent_range_raw`, `rent_is_range`, and `rent_provenance` on physical units as
well as plan rows. The canonical range is derived from the numeric endpoints;
the original text survives separately.

The reconciliation rule is conservative. A published range fills a missing
endpoint and repairs only the lossy case where both numeric endpoints collapsed
to one value inside that interval. A conflicting numeric value is retained,
the original range remains in `rent_range_raw`, and provenance is
`numeric_fields_conflict_with_published_range` rather than silently widening
the rent. Reversed numeric endpoints are normalized. These fields persist
through filesystem state, SQL revision 0014, the TypeScript data providers,
the unit API transform, Excel, carry-forward, and extraction-result
diagnostics.

### Diagnostics

`_meta.provenance` now emits the required contract fields:

- `raw_source_count`
- `parser_count`
- `formatted_count`
- `final_admitted_count`
- `canonical_id_uniqueness`
- `property_identity_verdict`
- `availability_date_provenance`
- `unit_source_provenance`
- physical-unit rent-range count and rent-provenance distribution

Every adapter-retained API, authored HTML, and admitted binary asset response
is now stored outside `properties.json` under a content-addressed
`raw_sources/<kind>/<property_id>/<sha256>.*.gz` path. Rejected bounded area
probes are retained too: a schema drift, non-200 body, unsupported roster, or
property-identity mismatch is future diagnostic evidence even though it cannot
populate a unit. The per-property manifest records URL, HTTP status, response
kind, identity verdict, original source hash, stored-payload hash, redaction,
and archive path.

Each affected unit carries the minimum join back to that evidence:
`source_response_sha256`, sanitized `source_response_url`, and
`source_record_locator` (plus parent/asset pointers when relevant). No raw body
is duplicated on every unit.

A separate immutable extraction snapshot contains the complete pre-format
unit and floor-plan rows, the final formatted property, unit-source provenance,
area-enrichment decisions/non-admissions, and the verdict. This allows future
parser-versus-formatter diagnosis without another live probe. The primary
fetch keeps its existing latest pointer and also receives an immutable body
hash path. The legacy complete `raw_api/<property_id>.json.gz` artifact remains
for compatibility; API URL secrets and credential-like fields are redacted in
both archive forms.

## Verification

- Cross-layer schema, state, SQL, migration, persistence, TypeScript-contract,
  affected-adapter, area/rent, raw-archive, Excel, and sync selection: 528
  passed, 38 skipped because optional live PostgreSQL/dependency environments
  were not configured.
- The full Python suite reached 9,948 passed and 49 skipped. It initially had
  four failures: one expected-dict assertion needed the three newly exposed
  raw-HTML archive pointer fields and was fixed (its test module then passed
  8/8); the remaining three are pre-existing repository script-layout checks
  for uncategorized root scripts and one diagnostic module without a CLI main
  guard. They are unrelated to extraction/output behavior and were not changed
  in this scope.
- Revision 0014 isolated migration replay: upgrade, downgrade to 0013, and
  re-upgrade passed on SQLite. A clean replay of the entire authoritative
  migration tree remains blocked by the pre-existing dynamic-`Base` 0001 / 0003
  duplicate `property_reports` defect; the 0013-to-0014 production path is not
  affected.
- Offline persistence replay of the downloaded affected-386 output: all 9,726
  physical rows persisted; all 9,726 history keys were non-null and unique;
  3,943 rows retained standalone building identity; 7,331 retained a raw
  availability value; and 4,878 retained `explicit_future` provenance.
- The changed TypeScript data-provider/service files compile in isolation and
  the service test suite passes 5/5. The full services build still reaches the
  unrelated pre-existing `JsonFileRunService` interface error
  (`getLlmReport` / `getLlmPropertyDetail` missing).
- The new evidence-gated area module passes strict mypy in isolation. Ruff,
  Python compilation, JSON manifest validation, and `git diff --check` pass.
- Three independent public Apts247 probes and a direct Mill Creek end-to-end
  replay passed the property-identity and no-midpoint gates. No CAPTCHA solver,
  unlocker, or paid browser was required for those checks.
- No image build, deployment, GCP job, or paid canary was started.

The deterministic 32-property follow-up input is
`focused-output-contract-canary-v1.json`. It covers all nine observed
canonical-ID rewrite properties, all nine trusted floor-name-loss properties,
both SightMap catalogue-marker properties, every affected-386 `area=-1`
property, the conservative unresolved cases, unit-level rent ranges, and the
offline replay archive contract.
