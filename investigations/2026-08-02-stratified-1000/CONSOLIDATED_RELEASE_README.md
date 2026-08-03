# Consolidated scraper recovery, output, and audit README

Date: 2026-08-02/03
Branch: `codex/recovery-replay-hardening-2026-08-02`
Base benchmark: `origin/main` at `02369d2` when this worktree was created

This is the top-level handoff for the failed-no-data, plan-to-unit,
availability-date, adapter-quality, warm-profile, output-schema, diagnostic,
and canary work completed in this branch. It separates locally proved
behavior, GCP canary evidence, and remaining gaps so a favorable sample is not
mistaken for fleet certification.

## What is complete

- Both stopped worker streams are preserved with their scripts, decisions,
  ledgers, negative controls, and retained response evidence.
- All 47 registered adapters and every non-registry output owner observed in
  the Aug 1 benchmark have a coverage disposition.
- The adapter audit's **49 numbered, evidence-backed findings** were
  implemented with focused regression coverage.
- A deterministic 386-property affected manifest covers findings 1–49.
- Availability dates are normalized through one contract and keep raw value,
  normalized value, and provenance.
- Property identity is checked before a detached API/link-hop roster can be
  accepted or persisted as a reusable route.
- Unit identity, building identity, rent/area ranges, and response lineage are
  surfaced and persisted without replacing the source unit ID.
- Content-addressed raw responses and immutable pre-format/final extraction
  snapshots make most future investigations replayable without a live probe.
- A deterministic, stratified 1,000-property GCP canary completed and its
  immutable output was downloaded once for an offline audit.
- The six post-canary output-contract clusters were fixed and covered by a
  deterministic affected-property verification cohort.

## Finding denominator: 49, not 59

The phrase “about 59 adapter defects” was an approximation. The source audit
contains exactly **49 numbered findings**. They are the rows represented in
the 386-property affected manifest and in the 1,000-property canary's
`finding-validation.csv`.

The 1,000-property output audit then found six additional cross-cutting
output/runtime clusters:

1. avoidable synthetic IDs despite a retained natural apartment number;
2. duplicate parallel Entrata rosters;
3. capture-date availability attached to an explicit negative status;
4. `DEAD_URL`/failure verdicts despite recovered physical units;
5. incomplete timeout diagnostics; and
6. a unit-producing ManageBuilding response missing from the source archive.

Those are tracked separately from the 49 adapter findings. Counting issue
rows, affected properties, or individual adapters as “defects” would produce
larger numbers but would not be a stable engineering denominator.

## Evidence and run lineage

| Stage | Scope and result | Provenance |
| --- | --- | --- |
| Failed-no-data discovery | 244/344 strict local admissions (70.93%); 243 in the reconciled ledger plus one separately admitted 3× replay | `../2026-08-01-consolidated-canary/README.md` and `worker-archive/failed-no-data/` |
| Plan-to-unit discovery | 365/549 strict local counter (66.48%); last single reconciled ledger has 330 rows | `worker-archive/plan-to-unit/` |
| Plan cohort GCP canary | 303/549 strict unit successes (55.19%) | `gs://jugnu-canary/runs/2026-08-01-plan60-full549-v2/` |
| Aug 1 fleet benchmark | 4,982 properties; 4,238 `SUCCESS` (85.06%); 4,226 with at least one real ID (84.83%) | `gs://jugnu-canary/runs/2026-08-01-consolidated-strict-fa1afb7/` |
| Affected-386 canary | 386/386 terminal; 352 strict unit successes (91.19%) | `jugnu-affected386-33864eb-2bwp8` |
| Stratified release canary | 1,000/1,000 terminal; 868 unit, 53 plan, 43 failed-no-data, 23 unreachable, 9 dead URL, 4 no-data-published | `jugnu-strat1000-ff7b377-gxtvm` and `launch-manifest.json` |
| Initial post-fix verification | 28/29 terminal; every exercised fix cluster passed; Clearview (`52697`) was the sole missing output | `verification-v1/interim-28-analysis/` |
| Isolated timeout retry 1 | Reproduced the Clearview event-loop wedge; no terminal output; disproved the in-process-only fix | `verification-v1/retry-pid52697/` |
| Isolated timeout retry 2 | 1/1 task; reproduced the wedge and materialized a traceable terminal row, snapshot, and source manifest | `verification-v1/retry-pid52697/attempt-2-823ea7c/` |
| Combined post-fix verification | Exact 29/29 properties; 0 critical/high issues, 0 avoidable synthetic IDs, 0 unresolved-area rows, 29/29 snapshots | `verification-v1/post-run-verification/` |

The stratified sample contains all 386 finding-linked properties, all 47
registered adapters, 48 observed output owners, 105 observed
adapter-by-prior-result strata, all six prior result/property types, and 50
geographic buckets. Its deterministic sample SHA-256 is
`0a51ad268034ed333e67bbcc7b51855c16c66a448091c71ea6bef2c6c936909d`.
The run used Hyperbrowser with proxy enabled and LLM disabled. CAPTCHA solving,
Web Unlocker, FlareSolverr, fingerprint rotation, and the datacenter proxy tier
were disabled.

## Main implementation areas

### Adapter and merge fixes

The numbered audit covers exact source-to-final defects rather than general
adapter intuition. Major fix families include:

- preserving provider-native property, building, floor-plan, and apartment
  IDs instead of replacing them with display-derived identities;
- property-bound response admission for RentCafe/SecureCafe, SightMap, Knock,
  Edifice, OneSite, AppFolio, Funnel, AMLI, and other detached/link-hop routes;
- reconciling repeated or parallel roster surfaces by source authority and
  immutable apartment identity;
- keeping empty catalogue plans in the plan channel instead of manufacturing
  apartment rows;
- excluding waitlist, portfolio-wide, roommate, sibling-community, deposit,
  and recommendation-card sentinels from physical inventory;
- retaining source rent, area, date, floor, bath, plan, and term fields where
  the first-party response proves them; and
- recording the actual unit-producing response rather than only the entry
  page or a guessed endpoint.

The complete adapter-by-adapter evidence, exact affected examples, fix
contracts, and implementation status live in:

- `../2026-08-01-consolidated-canary/ADAPTER_COVERAGE_MATRIX.md`
- `../2026-08-01-consolidated-canary/ADAPTER_DATA_QUALITY_FINDINGS.md`
- `../2026-08-01-consolidated-canary/ADAPTER_DATA_QUALITY_FIX_PLAN.md`

### Property identity and contamination controls

A candidate unit roster is accepted only when its returned metadata agrees
with the configured property by strong property-name or address evidence.
Explicit mismatches fail closed. Confirmed bad per-property routes are
quarantined, sibling recommendation cards are excluded, and Edifice UUIDs are
verified through their returned property metadata.

The gate deliberately permits safe branding/address variants and phase-number
normalization while rejecting one-token overlaps and explicit phase
conflicts. Actual response provenance records the identity verdict and
evidence. See
`../2026-08-01-consolidated-canary/PROPERTY_IDENTITY_AND_JULY_PROFILE_VETTING.md`.

### Availability dates

Adapters may supply either `available_date` or the legacy
`availability_date`; the write boundary now reads both. The contract is:

- preserve an explicit future calendar date exactly;
- map a visible positive “Available Now” state to the capture date;
- do not synthesize a date for negative/unavailable/waitlist status;
- preserve the calendar-date prefix of timezone-bearing source timestamps,
  avoiding a one-day timezone shift; and
- retain `available_date_raw` and `availability_date_provenance` such as
  `explicit_future`, `available_now`, `historical_embedded`,
  `capture_date_default`, or `missing`.

The affected-386 audit preserved every source future date and found no date
contradiction. The 1,000-property canary preserved 3,193/3,193 comparable
same-source future dates. The later negative-status defect was narrower: eight
rows had inherited a capture-date default despite an explicit negative status;
the post-canary fix suppresses that contradiction.

### How the one-by-one recovery probes were run

The failed-no-data and plan-to-unit workers did not admit a property from a
bulk hostname guess or from a visually plausible unit count. Properties were
resolved individually. Vendor clustering was used to avoid repeating the same
discovery work, but every numerator admission still has a property-specific
identity and unit gate.

The archived inputs, scripts, and ledgers referenced below live under
`../2026-08-01-consolidated-canary/worker-archive/failed-no-data/` and
`../2026-08-01-consolidated-canary/worker-archive/plan-to-unit/`.

The shared operating loop was:

1. **Freeze the exact denominator.** Failed-no-data used the 344 unique rows in
   `failed-no-data/failed344.csv` (SHA-256
   `f0f110b43fda0d269331afe683eaf76f57b0bf734af78500f2f84d25a9bb0b06`).
   Plan-to-unit used the 549 unique rows in
   `plan-to-unit/plan60_549.csv` (SHA-256
   `b40f11a8329c751e6d1ba4bf7eda16e8139eb3422c2acb3de0856dae8755e0c8`).
   Off-cohort properties could be controls, but could not enter the numerator.
2. **Start from the remaining ledger, not the favorable cases.** Each pass
   grouped unresolved properties by currently detected adapter, published
   page shape, known provider clues, and prior failure. The worker selected the
   next exact property from that ledger and preserved negative results as well
   as recoveries.
3. **Probe the configured property and its first-party chain.** The sequence
   was the configured marketing URL, current redirect/official property URL,
   published availability or application link, then the page's own API/XHR or
   embedded payload. Compliance-safe direct fetches were tried first.
   Hyperbrowser was used for a JavaScript-rendered or blocked route when
   needed. A detached API discovered from another property was never accepted
   merely because it returned apartments.
4. **Bind the response to the configured property.** Returned vendor property
   ID/site ID, property name, address, configured slug, and same-origin or
   official link relationship were compared. An explicit mismatch, sibling
   community, recommendation card, portfolio-wide roster, or unbound shared
   host failed closed. Rebrands/migrations needed address or other strong
   continuity evidence.
5. **Apply the physical-unit gate.** A row needed a provider-native apartment
   number/ID and could not be a floor-plan placeholder, waitlist, deposit,
   roommate/bed inventory, or inferred/synthetic plan row. The failed-no-data
   local ledger additionally required a positive published rent for the same
   native unit. The GCP plan-cohort gate required a `SUCCESS` verdict, a
   non-empty unit array, and `real_id_units > 0`; three known shape overcounts
   were removed from that numerator.
6. **Run the route through the configured end-to-end pipeline.** A raw API
   response alone was discovery evidence, not a recovery. The current detector,
   adapter, merge, formatter, verdict, and source-to-final identity path had to
   retain the native units for the configured property. Generalized fixes were
   probed on at least three members of the claimed cluster plus an end-to-end
   case before being treated as cluster-wide.
7. **Materialize an evidence row.** The artifact recorded property ID/name,
   configured and producing URLs, identity verdict, contamination verdict,
   unit count, native-ID count, native-ID-plus-positive-rent count, sample unit
   IDs, and local replay/test result. Repeated checks were recorded where a
   route was unstable; for example, the post-ledger Tuscany Hills admission
   required three successful configured end-to-end replays.
8. **Rebuild and deduplicate the numerator.** The failed-no-data builder uses
   an explicit artifact allowlist; a JSON file does not count simply because
   it exists. It admits only exact-cohort, strictly qualified rows, deduplicates
   by property ID, writes overlaps/rejections, and regenerates the remaining
   ledger. The plan worker similarly reconciled unique property IDs rather than
   adding probe counts. This prevented repeat probes and multi-lane overlaps
   from inflating conversion.
9. **Separate route learning from success.** A successful run profile was
   promoted only if it contained a concrete, reusable winning page/API/platform
   hint and passed property identity. Bootstrap-only profiles were retained as
   run evidence but were not called warm routes or promoted on that basis.

Production canaries for this work used Hyperbrowser with its proxy enabled.
They kept LLM, CAPTCHA solving, Web Unlocker, FlareSolverr, fingerprint
rotation, and the datacenter proxy tier disabled. Local diagnostic testing was
allowed to use a CAPTCHA solver, but strict admission depended on first-party
property identity and the unit-producing response, not on challenge-bypass
success.

#### Failed-no-data ledger and target accounting

The exact starting cohort was 344 properties. The worker first tracked the
60% intermediate gate (207 properties), then a 75% stretch target (258). For a
70% comparison, the integer threshold is `ceil(344 × 0.70) = 241`.

- `build_current_strict_ledger.py` is the reproducible admission/dedup builder.
- `strict_recovery_ledger_current.csv` is the 243-property reconciled ledger
  (SHA-256
  `475f209464568dbc9e8c4bf9ddccb694241b297e3d37b2820b4fbf2877f352f7`).
- `strict_recovery_remaining_current.csv` contains the 101 unresolved rows
  after that build, including their current adapter/disposition.
- `strict_recovery_ledger_current_summary.json` records artifact counts,
  rejected shapes, and target arithmetic.
- `post_ledger_strict_supplement.csv` contains one additional independently
  admitted 3× replay. It is kept separate so the ledger is not silently
  rewritten after the worker stopped.

The resulting local discovery count is **244/344 (70.93%)**, three properties
above the 70% threshold and 14 short of the later 75% target. It was
artifact-backed local evidence; the archived summary explicitly records
`paid_canary_run: false`, so it must not be described as a 244-property GCP
canary result.

#### Plan-to-unit ledger and target accounting

The exact starting cohort was 549 properties whose prior result was plan-level.
The worker originally pursued 60%, and the later discussion considered 80%.
For the requested 70% comparison, the integer threshold is
`ceil(549 × 0.70) = 385`.

- `propai-plan60-authoritative-ledger-2026-07-31.tsv` is the last fully
  materialized local ledger: 330 unique properties, exactly the 60% threshold,
  with SHA-256
  `b428382a1ff33375cca65f7088eff566b2a1be02792a8e5cbb33e50eff1afb6f`.
- The stopped worker's final strict counter was 365 after later recovery
  tranches. Those 35 tail admissions were implemented/tested but were not
  reconciled into one final row-level TSV before interruption; this limitation
  is preserved in `POST_LEDGER_STATE.md` rather than reconstructed from memory.
- `propai-plan60-remaining-opportunities-2026-07-31.tsv` preserves the
  unresolved opportunity classes at the last ledger checkpoint.
- `audit_full549_v2_gcs.py` and `full549_v2_strict_output_audit.json` preserve
  the independent GCP output audit. The corrected strict promotion gate in
  `reference_stale_profile_promotion.py` produced 303 strict properties after
  excluding three explicit shape overcounts.

Therefore the best frozen local counter was **365/549 (66.48%)**, 20 short of
70%; the independently audited strict GCP result was **303/549 (55.19%)**, 82
short of 70%. The README does not combine them or claim that this stream met
70%. Of those 303 strict GCP successes, the final generation-pinned classifier
found 243 actionable profiles and 60 bootstrap-only profiles.

The workers also retained production-shaped routes and added guarded replay
paths for exact winner URLs/API endpoints, reserved one Hyperbrowser call
inside the existing per-property cap for a validated route, kept Hyperbrowser
recovery independent of Web Unlocker, preserved one SecureCafe Path-B
opportunity, and hydrated focused inputs from the canonical property catalog
so identity gates have the same metadata as a full run. The integrated replay
contract and its tests are documented in
`../2026-08-01-consolidated-canary/RECOVERY_REPLAY_HARDENING_2026-08-02.md`.

## Output and persistence contract

### Identity and day-to-day merge fields

The source ID is not overwritten and a building ID is not appended to it.
Instead the output exposes separate, auditable companions:

| Field | Meaning |
| --- | --- |
| `unit_id` / `canonical_unit_id` | Existing collision-safe current-state identity |
| `source_unit_id` | Provider/display apartment ID before disambiguation |
| `building`, `building_id`, `building_id_source` | Human label, provider building identity, and its source path |
| `floor_plan_id`, `floor_plan_name`, `floor_plan_name_provenance` | Provider plan identity/label and trust evidence |
| `source_ids` | Adapter-specific native IDs retained without flattening |
| `unit_id_aliases`, `unit_id_alias_sources` | Proven leading-zero aliases and their source snapshots |
| `unit_history_key` | Property-scoped `unitsha_<sha256>` continuity candidate |
| `unit_history_key_basis`, `unit_history_key_quality`, `unit_history_key_version` | Exact input basis, strength classification, and algorithm version |

The history-key basis uses, in order: a registered stable provider-native ID;
otherwise property + optional building + optional floor-plan + source unit ID;
otherwise property + canonical physical unit ID. Synthetic/missing rows are
explicitly unjoinable.

Important: `unit_history_key` is indexed and persisted but is **not yet the
merge primary key**. Current state continues to upsert on property + canonical
`unit_id`. This avoids changing history identity before a two-day continuity
replay proves the SHA basis is stable across inventory and price changes.
Filesystem state tracks consecutive absence with a two-run grace; SQL state
records `disappeared_since` and `last_absent_date` when a prior unit is absent.
New, updated, unchanged, and disappeared buckets can then be aggregated by
floor plan for counts and min/max rent without erasing unit history.

### Area and rent

The backward-compatible legacy `area` field may still be `-1`, but new
consumers can avoid that sentinel:

- `area_sqft`, `area_low`, `area_high`, `area_range`, `area_range_raw`
- `area_value_type`, `area_is_published`, `area_provenance`,
  `area_source_url`
- `area_absence`, `area_absence_evidence`
- `area_pre_sanity_value`, `area_sanity_decision`, `area_sanity_reason`,
  `area_sanity_source`

Published ranges remain ranges; the pipeline does not invent a midpoint.
Missing area is classified as not applicable, not captured, not published, or
unknown only when the required evidence exists.

Physical units retain rent ranges through:

- `rent_low`, `rent_high`, `rent_range`, `rent_range_raw`
- `rent_is_range`, `rent_provenance`

A published range may fill a missing endpoint or repair a provably collapsed
numeric interval. A real conflict remains visible as
`numeric_fields_conflict_with_published_range`; it is not silently widened.

### Response lineage and diagnostic fields

Each unit may link to its producer through:

- `extraction_tier`
- `source_response_sha256`, sanitized `source_response_url`
- `source_record_locator`, `source_parent_record_locator`
- `source_asset_url`, `source_asset_sha256`
- `identity_quality`

Revision `0014_units_output_identity` persists the first-class identity,
building, area, rent, availability, and response-lineage fields. Additional
adapter fields survive in the unit `extra` JSON. Filesystem state, SQL state,
failed-run carry-forward, Excel output, and TypeScript SQL/JSON providers were
updated together.

## Raw response and log capture

The run output now preserves two complementary layers:

1. **Interpreted state:** final property/unit rows plus unit-level response
   pointers and provenance.
2. **Replay evidence:** content-addressed compressed payloads under
   `raw_sources/<kind>/<property_id>/<sha256>.*.gz`, per-property source
   manifests, and immutable extraction snapshots containing pre-format units,
   plans, final output, enrichment decisions, and verdict.

The manifest stores sanitized URL, status, kind, identity verdict, source and
stored hashes, redaction state, and archive path. Rejected bounded probes are
also retained because a mismatch, unsupported shape, non-200 body, or schema
drift explains why data was not admitted. `events.jsonl`, `issues.jsonl`,
`route_shadow.jsonl`, property reports, and the cost ledger remain available
per shard.

Timeouts have two levels of protection. Normal property-finalization creates a
zero-row snapshot for terminal failures. If the child event loop itself wedges,
the shard supervisor kills the subprocess, materializes a schema-valid
`FAILED_UNREACHABLE` record with both timeout reasons, invokes the same
snapshot/source-manifest finalizer, and then runs the normal sync/upload path.

Raw canary mirrors are intentionally git-ignored. Compact audit ledgers,
manifests, checksums, and human reports are committed.

## Warm-profile stores and mutation history

“A run profile exists” and “a safe reusable warm route exists” are different
claims.

- The Aug 1 benchmark had 4,226 strict successes with profile objects, but
  only 3,886 contained a concrete route before identity vetting; 340 were
  bootstrap-only.
- The earlier 303-success plan canary yielded 243 actionable profiles; 237
  were created and six generation-guarded field merges were applied to the
  shared root. Sixty bootstrap-only profiles were not promoted.
- The later identity-admitted Aug 1 set contained 1,824 profiles. Its guarded
  shared-root update created 1,504, field-merged 304, left 16 existing objects
  unchanged, and reported zero failures.
- Thirty independently matched failed-no-data profiles were promoted later:
  25 created and five field-merged.
- The exact cross-stream identity-admitted union was 1,996 unique profiles
  with 2,382 retained routes. The versioned seed used by the stratified run was
  `gs://jugnu-canary/profiles/strict-v2-fa1afb7/`.
- The 1,000-property run copied only the 462 sample-intersecting profiles into
  `profiles/strat1000-ff7b377/`. The 29-property verification copied 27 and
  created two isolated bootstraps. All retry prefixes are isolated.

The stratified and verification runs did **not** mutate the shared root.
Existing shared-root alternates are not automatically certified merely because
a safe route was field-merged; future jobs should seed from a versioned,
identity-admitted candidate rather than treating the whole historical root as
a clean oracle.

## Analysis artifact index

All reproducible analysis created for this effort is committed under four
roots. Raw downloaded GCP mirrors, generated bytecode, local databases, and
credential-bearing raw profiles remain ignored by design.

`ANALYSIS_ARTIFACT_MANIFEST.json` indexes and SHA-256 hashes all **680**
Git-tracked analysis files (35,601,761 bytes): 25 availability-date files, 19
focused failed-no-data files, 576 consolidated-canary/worker files, and 60
stratified/final-verification files. The generator admits only Git-indexed
paths and excludes the manifest itself to avoid recursive hashing.

| Root | Contents |
| --- | --- |
| `../2026-08-01-availability-date/` | RP comparison, adapter-wide date scan, source-only tiers, live audits, and final residual readout |
| `../2026-08-01-failed-no-data/` | focused failed-no-data investigation artifacts not duplicated in the worker archive |
| `../2026-08-01-consolidated-canary/` | both worker archives, 49 adapter findings, adapter matrix/fix plan, identity audits, warm-profile manifests, affected-386 manifest, hashes, and output-contract report |
| `.` | stratified sample, launch lineage, full offline audit, post-canary fixes, 28/29 interim audit, retry evidence, and final verification |

Start with these files:

- `../2026-08-01-consolidated-canary/README.md`
- `../2026-08-01-consolidated-canary/ADAPTER_COVERAGE_MATRIX.md`
- `../2026-08-01-consolidated-canary/ADAPTER_DATA_QUALITY_FINDINGS.md`
- `../2026-08-01-consolidated-canary/ADAPTER_DATA_QUALITY_FIX_PLAN.md`
- `../2026-08-01-consolidated-canary/AFFECTED386_OUTPUT_FIXES_2026-08-02.md`
- `../2026-08-01-consolidated-canary/RECOVERY_REPLAY_HARDENING_2026-08-02.md`
- `post-run-audit/POST_RUN_AUDIT.md`
- `verification-v1/interim-28-analysis/REPORT.md`
- `verification-v1/post-run-verification/REPORT.md` after the final retry

`SHA256SUMS.json` files under the consolidated archive, affected manifest,
stratified manifest, and verification package make the retained evidence
tamper-evident. `ANALYSIS_ARTIFACT_MANIFEST.json` is the cross-root inventory.

## Validation and honest remaining limits

- Finding-mapped gate before the 1,000 run: 984 passed, 2 declared skips.
- Adapter/output focused gate after the six canary defects: 4,188 passed,
  4 skipped; the narrowed post-fix gate passed 271 tests.
- Supervisor-timeout focused gate: 68 passed.
- Supported repository gate (`cd ma_poc && pytest -q tests`): 9,981 passed,
  49 skipped, 3 failed. The three failures are the known script-layout checks
  also present on the base branch; they are not extraction/output failures.
- Final timeout/Hyperbrowser focused gate: 29 passed. The live-disproved
  in-process cleanup change from `ed94525` was removed from the final tree;
  `git diff ed94525^ --` is clean for both files it changed. Only the
  independently live-proved process-supervisor boundary remains.

The original 1,000-property audit remains a **HOLD** artifact because it is the
pre-fix evidence that found the defects; it must not be rewritten to look
green. The combined 29-property audit is the post-fix gate. It found no new
output defect and has exact 29/29 property/snapshot coverage.

Its overall conclusion remains `PARTIAL_RUNTIME_COVERAGE`, rather than a false
all-green label: one dead-entry candidate and six non-Clearview timeout
candidates did not reproduce their original target route. Every route that did
occur passed, including the live Clearview supervisor-timeout fallback, and
there were no critical/high issues. The unexercised cases retain fixture proof
but are not reported as live-route passes.

Runtime exercise is also reported honestly: the initial 1,000 canary exercised
39 of the 49 numbered target routes, left nine target routes unexercised, and
found one output-contract failure. Fixture proof remains useful but is never
labeled as a live-route pass.

Finally, the day-to-day `unit_history_key` is prepared and persisted but not
yet promoted to current-state merge identity. A multi-day continuity replay is
the required next gate before that migration.
