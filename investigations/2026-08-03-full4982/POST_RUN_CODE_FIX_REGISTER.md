# Full-4,982 post-run code-fix register

Date: 2026-08-03
Run: `jugnu-full4982-7577087c-v3-zrm2h`
Scope: findings discovered after the completed 4,982-property canary. These
items are separate from the already-gated 2026-08-02 consolidated release.

## Status key

- `CODE FIXED`: repository behavior and regression coverage updated locally.
- `DATA REPAIRED`: the saved run artifacts were replayed; no live extraction rerun.
- `BOUNDED CODE FIXED`: the confirmed property cohort is protected by an
  explicit audited rule; new properties still require evidence before being
  added because the rule is temporal/operator-specific.

## Register

| Priority | Finding | Measured run impact | Current treatment | Durable code work and gate | Status |
| --- | --- | ---: | --- | --- | --- |
| P0 | Knock internal UUID leaked into RP `unitid` | 12,104 rows / 502 properties | RP replay uses the operator-visible unit number; repeated labels are building-qualified | Preserve `unit_number` through both formatters; RP exporter uses public label while canonical UUID remains in `unit_id`/`source_ids`; test UUID retention and RP display | **CODE FIXED + DATA REPAIRED** |
| P0 | AppFolio address slug leaked into RP `unitid`; numeric-road precedence parsed `County Road 5, 208` as unit `5` | 1,614 delivered address-identity rows: 1,425 yielded public unit labels; 189 legitimately remained address-level identities and were rendered as readable addresses | Artifact replay applies corrected parser; target River Ridge row is `208` | Inter-comma unit token outranks street-number token; support `Apt.302` and hyphenated labels such as `A-101`; retain address as canonical storage identity | **CODE FIXED + DATA REPAIRED** |
| P0 | Collection page crosses configured property boundary (Novi Flats pattern) | 132 sibling-community rows across Novi Flats, Link 480, and Timber | Rows removed from the cleaned delivery | Fail-closed RentCafe/SecureCafe property-scope rules for all three verified collections; independent unconfigured/provider controls | **CODE FIXED + DATA REPAIRED** |
| P0 | Backend roster is broader/staler than current marketing inventory | 366 rows removed across Chartwell, SW 38th, Westchase, Tides on Park Lane, Duke Manor, and St. Johns Wood; Novi handled by boundary rule | Current-marketing identities used as bounded authority in cleaned delivery | The six audited properties now bypass non-empty Knock (including the WARM direct tier) and continue to the live RentCafe/SightMap recovery path. Counts remain live; none are hard-coded | **BOUNDED CODE FIXED + DATA REPAIRED** |
| P0 | Same physical apartment survives multi-source or multi-page accumulation | 222 duplicate rows across 23 confirmed properties | Property-scoped dedupe in cleaned delivery | Reconcile on provider-native/source apartment ID before formatted IDs; overlay ResMan availability even when the catalogue plan label is stale; preserve legitimate building-qualified repeats | **CODE FIXED + DATA REPAIRED** |
| P1 | Plan-level success not fully represented by the original available-only export | 349 `SUCCESS_PLAN_LEVEL` properties; original RP output had only four plan rows | Validated RP delivery contains normalized PLAN rows separately from UNIT rows | Workbook/RP builders now consume both `units[]` and `floor_plans[]`, include unavailable/waitlist PLAN evidence, dedupe legacy plan copies, and always leave plan `unitid` blank | **CODE FIXED + DATA REPAIRED** |
| P1 | Explicit not-available and waitlist placeholders can pass the marketable-row boundary | 202 Nollie rows and 17 River Oaks rows removed | Removed from validated delivery | SightMap maps explicit `Not Available` map polygons to UNAVAILABLE; Repli360's existing four-signal waitlist sentinel remains gated and covered | **CODE FIXED + DATA REPAIRED** |
| P1 | Raw-response observability gap on direct Knock tier | Knock extraction snapshots contain public labels, while sampled raw manifests have no API response body | Extraction snapshot was sufficient for this replay | Direct Knock now supplies metadata + unit bodies to the raw archiver and stamps every unit with response hash, URL, and record locator | **CODE FIXED; FUTURE RUNS CAPTURE RAW BODY** |

## Unit-identity contract established by this fix

1. `unit_id` / `canonical_unit_id` remain stable, collision-safe storage keys.
2. Provider-native opaque IDs remain in `source_ids` and history provenance.
3. `unit_number` is the operator-visible apartment label and must survive the
   production formatter.
4. RP `unitid` uses the public label. If a property repeats that label, it is
   qualified with building/street context; an opaque provider anchor is used
   only as a final collision suffix.
5. Address-only scattered-site listings keep an address identity, but exports
   render the readable marketing address rather than a slug.

## Verification completed for the unit-ID repair

- 250/250 shard property artifacts loaded; 4,982/4,982 properties present.
- 81,015 output rows retained: 77,943 UNIT and 3,072 PLAN.
- Knock UUID leakage after replay: 0 rows.
- AppFolio address-slug leakage after replay: 0 rows.
- Duplicate non-empty UNIT identities after qualification: 0.
- Expanded focused regression suite: 351 passed.
- `git diff --check`, compile, and Ruff gates: passed.
- Refreshed RP delivery: 77,943 UNIT + 3,072 PLAN = 81,015 rows;
  all four non-empty PLAN `unitid` values cleared.

## Rerun requirement

The validated CSV/XLSX were repaired from archived run evidence, so a full
4,982-property extraction rerun is not required for the current RP delivery.
The extraction changes take effect on subsequent runs. In particular, the old
direct-Knock raw manifests cannot be retroactively populated with bodies that
were not archived; the new provenance contract is verified by tests and will
be present on the next direct-Knock success.
