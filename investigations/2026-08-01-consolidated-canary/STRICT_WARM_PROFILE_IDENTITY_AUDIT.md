# Strict warm-profile identity audit — 2026-08-01

## Current answer

This file records the initial 877-profile, self-describing-route wave. The full
archive-plus-live pass is now complete and supersedes its earlier safe-floor
estimate. It produced a local positive-only candidate of **2,566/3,449
(74.40%)**, while quarantining 51 profiles and withholding 832 for review. See
[`STRICT_WARM_PROFILE_CANDIDATE_V2.md`](STRICT_WARM_PROFILE_CANDIDATE_V2.md)
for the current policy, totals, reproduction command, and artifacts.

No candidate or historical GCS object was changed by either audit. No shared
profile pointer was switched and no full canary was launched.

## Complete first wave

The frozen 3,449 profiles split into these verification classes:

| Class | Profiles | Verification path |
|---|---:|---|
| Self-describing vendor API | **877** | Live vendor name/address metadata |
| Cross-host, not self-describing | **1,523** | Lightweight landing/API identity fetch |
| Same-host or relative route | **1,049** | Same-site title/address/route binding |

The first class contains 1,345 distinct retained routes because some Knock
profiles retain both community and numeric property IDs, and seven profiles
retain two different numeric property IDs. Every distinct route was checked;
the audit did not stop after finding one good route for a profile.

| Live route result | Routes |
|---|---:|
| `MATCH` | **1,277** |
| `MISMATCH` | **63** |
| `FETCH_FAILED` | **5** |
| Total | **1,345** |

| Conservative profile result | Profiles |
|---|---:|
| Every retained route matched | **817** |
| At least one retained route mismatched | **55** |
| No mismatch, but one retained route was unresolved | **5** |
| Total | **877** |

The five fetch failures are obsolete Knock community hashes returning HTTP
404. Each profile also has a live numeric Knock property route that positively
matches its configured property. These profiles can therefore be made strict
by removing only the dead community route.

## Confirmed stale cross-property routes

Marketing-page checks separated obvious aliases/rebrands from genuine
cross-property routes. The following 20 routes across 14 profiles are
confirmed wrong for the configured property:

| Configured property | Wrong live identity | Wrong routes |
|---|---|---:|
| Abbey at Champions (`6356`) | The Bohemian | 1 |
| Collins Junction (`21370`) | Mercantile Wharf | 2 |
| Renaissance Villas (`23170`) | Granite Point | 2 |
| Village at East Riverside (`23791`) | Addison Landing | 1 |
| Mercantile Wharf (`33539`) | Collins Junction | 1 |
| Granite Point (`35846`) | Renaissance Villas | 1 |
| Trails at City Park (`40171`) | Belara Lakes | 1 |
| Addison Landing (`42510`) | Village at East Riverside | 2 |
| Shelard Village (`49598`) | Sumter Green | 1 |
| Park 7 (`218296`) | Vault Apartments | 1 |
| Retreat at Fairhope Village (`218987`) | Evergreen at River Oaks | 2 |
| The Pointe at Victoria (`222578`) | The Urban | 2 |
| Evergreen at River Oaks II (`240743`) | Retreat at Fairhope Village | 1 |
| Evolve at the Pines (`292538`) | Evolve Huntersville | 2 |

The reciprocal swaps (Collins/Mercantile, Renaissance/Granite,
Village/Addison, and Retreat/Evergreen) are especially strong evidence that
these are profile contamination rather than harmless rebrands.

Twelve of the fourteen affected profiles also retain at least one separately
verified matching route; removing only the wrong route preserves those
profiles. Shelard Village and Evolve at the Pines have no positively matching
self-describing route in this wave and must be excluded or re-probed before a
strict v2 admission.

## Conservative safe floor and review tail

Without accepting a single fuzzy-name alias, the 877-profile wave can already
yield **834 strictly clean profiles**:

- 817 profiles whose every route matched;
- 12 mixed profiles after deleting their confirmed wrong routes; and
- 5 profiles after deleting an obsolete 404 community route while retaining
  the positively matched numeric route.

Two profiles are confirmed exclusions. The remaining **41** are an explicit
review tail: most look like current rebrands or harmless spelling variants,
while several are phase/campus/combined-property routes that must not be
admitted on name similarity alone. A direct marketing-page screen returned a
normal page for all candidates and is useful for prioritisation, but it is not
being treated as strict admission evidence without an exact route/network or
address binding.

## Reproduction and security boundary

Run the first wave from the repository root:

```bash
python -m ma_poc.scripts.diagnostics.audit_warm_profile_identity \
  --profiles-dir investigations/2026-08-01-consolidated-canary/july-vetted-profile-snapshot-v1/profiles \
  --cohort-csv /private/tmp/properties_full_4982_2026-08-01.csv \
  --output-dir investigations/2026-08-01-consolidated-canary/warm-profile-identity-audit-v1 \
  --expected-profiles 877 --workers 8 --timeout 20 --restart
```

Artifacts:

- `warm-profile-identity-audit-v1/route-ledger.jsonl`
- `warm-profile-identity-audit-v1/summary.json`

The auditor strips `PROBE_PROXY_URL` and `WEB_UNLOCKER_KEY` from its process,
forces `unlocker=False`, never writes profiles, and never records endpoint
URLs. SightMap path tokens are represented only by a SHA-256 route fingerprint
and the non-secret numeric asset ID. Response bodies are not retained; only
their SHA-256, byte count, HTTP status, observed identity, and decision evidence
are stored.

## Leverage the archived July GCP run before probing again

The full July run retained substantially more evidence than the promoted
profiles themselves. A read-only inventory of
`gs://jugnu-canary/runs/2026-07-31-fetchfix-5k/` found 250 shard artifact sets
with:

| Historical evidence | Candidate-profile coverage |
|---|---:|
| Per-property Markdown report | **3,449 / 3,449** |
| Explicit winning-source URL in report | **2,916 / 3,449** |
| Captured API bodies embedded in report | **2,071 / 3,449** |
| Raw captured HTML | **2,995 / 3,449** |
| Standalone API sample | **1,091 / 3,449** |

The reports contain 2,452 captured API responses in total. Report bodies are
truncated to 3,000 characters by the report generator, so they are useful for
top-level property metadata but must not be assumed to contain every nested
field. Raw report/API material should be parsed in memory; it must not be copied
into git because endpoint credentials may be present.

This archive changes the economical audit plan. Of the 2,572 profiles outside
the completed live-metadata wave, **2,272 have archived raw HTML, an embedded
API body, or an explicit winning source**. Combining the archive with the 877
live checks leaves **300 profiles**, not 2,572, with no archived body/winner
evidence. All 300 have event-ledger rows, but only validation/output events—the
direct fast paths omitted URL, adapter, and winning-tier events—so those 300
still require a targeted route check.

The winning-source distinction is particularly valuable for contamination:

- Renaissance Villas (`23170`) captured both Renaissance and Granite Point
  Knock endpoints, but its report identifies the Renaissance endpoint as the
  actual winner. The Granite route can be removed without discarding the good
  profile.
- Evolve at the Pines (`292538`) identifies the Evolve Huntersville endpoint
  itself as the winner. That confirms a contaminated output/profile rather
  than a harmless unused alternate.

The next pass should therefore build an offline, property-keyed ledger from
the reports, raw HTML, API samples, and winning-source field. Live/browser
probing should be reserved for the 300 evidence-gap properties and whatever
phase/rebrand cases remain ambiguous after offline identity comparison.

## Historical next-pass plan

The following was the plan at the end of the initial 877-profile wave. The
archive parsing, direct winner checks, AppFolio address scoping, and local v2
materialization described here have since been completed.

1. Parse the persisted GCP reports/raw artifacts into the same identity ledger,
   retaining only the actual winning source and independently validated
   alternates.
2. Browser/network-bind the 41 first-wave alias/phase candidates; retain
   explicit phase/combined cases as unresolved unless the returned metadata or
   units distinguish the configured phase.
3. Check unresolved cross-host profiles provider by provider, starting with
   SecureCafe and portfolio-scoped vendor routes.
4. Check unresolved same-host routes with a cheap title/address/route-marker
   fetch; use a browser only for the unresolved JavaScript tail.
5. Materialize a new create-only v2 prefix from positive matches only. Do not
   mutate or relabel the v1 prefix.
