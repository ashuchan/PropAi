# Strict warm-profile candidate v3

This directory freezes the evidence and admission ledger for the clean warm
profile seed prepared after the Aug 1 full benchmark and the Aug 2 stratified
1,000-property run. Raw response bodies and profile JSON are intentionally not
committed. The deployable profiles are stored in an immutable GCS prefix and
are content-bound here by `RELEASE_MANIFEST.json` and the per-property ledger.

## Result

| Measure | Count |
| --- | ---: |
| Full property cohort | 4,982 |
| Aug 1 strict unit-level successes | 4,226 |
| Aug 1 successes with an actionable route | 3,886 |
| Aug 1 bootstrap-only successes | 340 |
| Final identity-admitted profiles | **3,594** |
| Final retained identity-admitted routes | **4,572** |
| Aug 1 actionable successes covered | **3,269 / 3,886 (84.12%)** |
| Additional admitted profiles outside that intersection | 325 |
| Review / quarantine, not published | 1,366 / 22 |

The final profile-set digest is
`76a8173802d3841924c9bc17112d8d0337d0de81bc812ae1263585d0a616f93b`.
The immutable published seed is
`gs://jugnu-canary/profiles/strict-v3-f27a88e8-76a81738/`; all 3,594 objects
were created with generation-zero preconditions and then reconciled by object
name and stored hash. The historical mixed shared root was not mutated.
All 3,594 profile files passed Pydantic schema validation, canonical-ID and
ledger-hash agreement, retained-route presence, and the public-HTTP URL gate.
Re-materializing with every source and evidence input reversed produced
byte-identical profile files, ledger, and summary.

Code validation for this release:

- 45 focused identity-audit, materializer, and generation-guarded promoter
  tests passed.
- The full non-LLM suite reported 9,916 passed, 46 skipped, 3 deselected, and
  three repository-layout failures. The three failures concern eight legacy
  files already present in `origin/main` and
  `scripts/diagnostics/local_canary_attribution.py`; none is changed by the v3
  materializer/audit work.
- Ruff passes on every changed Python/test file. A repository-wide invocation
  reports 411 pre-existing violations, so it is recorded as a baseline rather
  than represented as a clean gate.

## What the 1,000-property run contributed

The post-fix stratified run wrote **1,000 run-local profiles** to
`gs://jugnu-canary/profiles/strat1000-ff7b377/`; those files remain available
for diagnostics and future re-evaluation. They are not all automatically safe
shared routes. Its archive and direct live identity evidence added **58 net-new
profiles** to the strict union. This distinction is intentional: successful
unit extraction proves that a response has units, not that the units belong to
the configured property.

The additional Hyperbrowser pass targeted only 395 still-unverified Aug 1
actionable profiles whose preferred route had failed an ordinary GET. CAPTCHA
solving, Web Unlocker, and FlareSolverr were disabled. Results were 212 `MATCH`,
133 `FETCH_FAILED`, 49 `UNKNOWN`, and 1 `MISMATCH`. Only the 212 matches were
admitted, lifting the clean candidate from 3,382 to 3,594 profiles.

## Source profile snapshots

The materializer field-unioned profile knowledge from these isolated stores:

- `gs://jugnu-canary/profiles/strict-v2-fa1afb7/`
- `gs://jugnu-canary/profiles/run-2026-08-01-consolidated-strict-fa1afb7/`
- `gs://jugnu-canary/profiles/affected386-33864eb/`
- `gs://jugnu-canary/profiles/strat1000-ff7b377/`
- `gs://jugnu-canary/profiles/verify-7f800ca/`
- `gs://jugnu-canary/profiles/verify-823ea7c/`

The newest `updated_at` snapshot owns current confidence, quality, and fetch
state. Older snapshots may contribute reusable routes. A content hash—not a
temporary download path—breaks equal-timestamp ties and identifies the source
set, so relocating the downloads cannot alter the release.

## Admission contract

A route is retained only when archived response metadata or a live response
independently yields a property-identity `MATCH`. Evidence may use an exact
vendor property ID, configured property slug, returned name/address/ZIP, or a
provider-specific property metadata response. Unit count, rent, comparison-feed
agreement, or scrape success alone never proves identity. `MISMATCH` dominates
a conflicting `MATCH`; unresolved routes are removed from admitted profiles.

For every admitted profile the materializer:

1. removes unresolved and mismatched winning pages, availability links, and
   endpoints;
2. clears unbound navigation/source hints and blocked endpoints;
3. validates the sanitized profile schema and canonical ID;
4. proves every retained route hash is in the positive identity set; and
5. writes the serialized profile hash to `strict-profile-ledger.jsonl`.

## Automatic learning after this release

The runner already pulls, learns, periodically flushes, and finally flushes
profiles to its configured `PROFILE_GCS_PREFIX`. Canary jobs use isolated
prefixes, which is why every run's learning survives without contaminating the
shared seed. The remaining production wiring should be a strict end-of-run
finalizer:

1. Always write learned profiles and unit-source provenance to the isolated
   run prefix.
2. Admit only the actual unit-producing response when it has a response hash,
   route hash, run/build ID, positive property identity, unit-level output, and
   no shape/contamination veto.
3. Field-merge admitted routes with the prior immutable clean seed; keep
   unknown evidence in review and mismatches in quarantine.
4. Publish a complete new immutable seed and atomically advance a small
   `current.json` pointer with a GCS generation precondition.
5. `PROFILE_AUTO_PROMOTE_STRICT=false` disables only promotion. Run-local
   learning and evidence capture continue.

Workers should never write directly to the current clean seed. If the
finalizer fails or is partial, the pointer remains on the prior known-good
release. The current manual promotion uses the same hash and generation guards;
automatic finalization should call this admission contract rather than define
a second one.

## Files

- `materialization/strict-profile-ledger.jsonl`: one URL-redacted decision per
  property, including source hashes, admitted/removed route hashes, and final
  serialized profile hash.
- `materialization/aug1-strict-actionable-ids.json`: exact 3,886-property
  denominator used for the 84.12% coverage statement.
- `evidence/*archive*`: identity extracted from archived GCP response metadata.
- `evidence/*direct*`: ordinary public-GET identity audits.
- `evidence/*hyperbrowser*`: the bounded 395-property access retry.
- `build_release_manifest.py`: revalidates a local materialization and hashes
  every committed evidence artifact.
- `GCS_RELEASE_MANIFEST.json`: URL-free per-object GCS generations and hashes
  for the immutable seed.
- `build_gcs_release_manifest.py`: validates and redacts the ignored reviewed
  promotion result into the committed GCS manifest.

Raw API/HTML bodies, public widget credentials inside profile JSON, and local
GCP mirrors remain outside Git. The committed ledgers retain hashes and bounded
identity metadata needed to reproduce each admission decision.
