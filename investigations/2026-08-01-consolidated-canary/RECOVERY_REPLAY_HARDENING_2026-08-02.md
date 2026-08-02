# Recovery replay hardening — 2026-08-02

## Outcome and metric definitions

The full Aug 1 consolidated run remains the benchmark. No replacement canary
was launched during this hardening pass.

| Metric | Properties | Cohort rate | Exact meaning |
|---|---:|---:|---|
| Output verdict `SUCCESS` | 4,238 / 4,982 | 85.06% | Run verdict only; this does not prove every emitted ID is real |
| At least one real unit ID | 4,226 / 4,982 | 84.83% | Strict unit-level success used for profile coverage |
| Every emitted row has a real ID | 4,162 / 4,982 | 83.54% | Excludes the 64 successes mixing real and synthetic rows |
| Strict success with a persisted run profile | 4,226 / 4,982 | 84.83% | Every strict success has a profile object |
| Strict success with an actionable replay route | 3,886 / 4,982 | 78.00% | Winning URL, API route, or availability link was persisted |
| Strict success with bootstrap-only profile | 340 / 4,982 | 6.82% | Profile exists, but no concrete unit-producing route was saved |

Thus the Aug 1 run does have approximately 85% *profile-record* coverage. It
does not yet have 85% safely reusable warm-route coverage. Of the 4,226 strict
successes, 3,886 (91.96%) have an actionable route and 340 are bootstrap-only.
The distinction is intentional: a route is reusable only after its own source
response independently matches the configured property.

The benchmark source is
`gs://jugnu-canary/runs/2026-08-01-consolidated-strict-fa1afb7/`; its run
profiles are under
`gs://jugnu-canary/profiles/run-2026-08-01-consolidated-strict-fa1afb7/`.
The strict profile snapshot manifest SHA-256 is
`05759bfa6e7f0f0fff07b1398cc9ca1bb2e56789b35cb8483d3fddf141e6cef2`.

## Identity-gated replay state

The Aug 1 actionable profiles were rematerialized through the strict identity
gate:

| Verdict | Profiles | Meaning |
|---|---:|---|
| `ADMIT` | 1,824 | At least one retained route independently identifies the configured property |
| `REVIEW` | 2,048 | No mismatch proved, but route identity is not independently strong enough |
| `QUARANTINE` | 14 | A route identifies another property and no safe positive route remains |

The 1,824 admitted profiles retain 2,204 exact matched routes. Their summary
SHA-256 is
`f5e3d3e3dbffbd50b718c60b931c2aa3af07b707aa13e71dd67e5feaf1dfd935`.
A guarded shared-store update then created
1,504 profiles, field-merged 304 existing profiles, left 16 existing profiles
unchanged, and recorded zero write failures. The execution manifest SHA-256 is
`4f229a20ba29a11354189662dd10c0bb4a6410a1bbd2182d1d87f6458f5b1e97`.

The stopped recovery workers contribute additional independently matched
routes that the general Aug 1 gate did not cover:

- Plan-to-unit: 142 admitted profiles and 144 retained routes from the exact
  549-property cohort. The strict candidate summary SHA-256 is
  `027dcaf42b8d0dfb7d765836e503104e0383e8cad273395f5a883f0a718a2bc2`.
- Failed-no-data: 30 admitted profiles and 34 retained routes. Twenty-one
  required unresolved alternates to be removed. The strict candidate summary
  SHA-256 is
  `0b1d83442606e6f03bcce9a7e795a8f83b9cca2da1fa0c5524401af8fcafd180`;
  its shared-store promotion created 25 and
  generation-guarded field-merged five, with zero failures. The execution
  manifest SHA-256 is
  `697f351175409f0e8e8bd9213ecd2209944d3ffc15ec6a821cc3f3b6bc9846d7`.

The exact local union is 1,996 unique identity-admitted profiles with 2,382
retained routes. The union summary SHA-256 is
`1cbadb97ef367d3f68c55cc78c4087b372cce55b93233038f3f7dcd3af7f9a02`,
and its strict ledger SHA-256 is
`e5fca4512ab392b4eafcadc57149c3f59dbf2819abb92d0b016d208c12c573e5`.
It was not uploaded and did not launch a job.

Important limitation: older plan-worker profiles already present in the
shared root may still contain unresolved alternate routes. A field merge can
add independently admitted routes but cannot prove that legacy alternates are
safe or delete them. A future strict canary must therefore seed from the
versioned identity-admitted candidate, not treat the entire shared root as a
sanitized oracle.

## Evidence-backed code changes

1. **Save the actual unit-producing response route.** A successful response is
   promoted into profile replay fields only when it is 2xx, emitted units,
   carries a valid response hash, uses an HTTP(S) non-infrastructure URL, and
   its response-level property identity verdict is explicitly `MATCH`.
   `UNKNOWN`, mismatched, unhashed, infrastructure, quarantined, and
   contaminated responses remain non-reusable. Credential-like query values
   stay redacted in diagnostic output; the profile updater recovers the exact
   in-memory replay URL only when it sanitizes to the same matched evidence
   record. A redacted placeholder is never persisted as a route.
2. **Protect the exact recovery route inside the existing Hyperbrowser cost
   cap.** `HYPERBROWSER_RESERVED_PRIORITY_CALLS=1` reserves one of the existing
   per-property sessions for a validated/profile route. It does not increase
   `HYPERBROWSER_MAX_CALLS_PER_PROPERTY`. This addresses 1,526 observed cap
   exhaustion events in the Aug 1 logs, including exact plan-cohort misses
   whose saved `/availableunits` route was starved by discovery requests.
3. **Keep blocked-render Hyperbrowser recovery available independently of Web
   Unlocker.** Hyperbrowser is an approved production provider; Web Unlocker
   remains disabled. CAPTCHA solving, FlareSolverr, and fingerprint rotation
   remain out of production.
4. **Retain one SecureCafe Path-B attempt.** A fast handoff no longer consumes
   every recovery opportunity before the unit-producing path can run.
5. **Hydrate focused-cohort metadata from the canonical catalog.** A minimal
   `--csv` canary now fills blank property name/address fields from the same
   canonical property ID, without overriding explicit values or adding rows.
   This gives response identity gates the inputs used by the full run.
6. **Make materialization and promotion deterministic and guarded.** Multiple
   profile/evidence sources can be unioned, missing archive decisions become
   `REVIEW`, conflicts are mismatch-dominant both within and across archive/live
   sources, required-ID manifests are a hard gate, and GCS creates/merges are
   generation-pinned through the actual write with rollback inputs.
7. **Join worker successes only to exact response-route evidence.** Local
   worker success counts do not synthesize profiles. The route must occur in
   the production-shaped run profile and match the independently verified
   unit-producing response.

## Future focused canary contract

`affected-property-manifest-v1/` is the deterministic, zero-cost future launch
package. It contains 386 unique properties, 392 property/finding rows, and
explicit coverage for findings 1–49. The frozen contract requires:

```text
COMPLIANCE_MODE=1
ENABLE_UNLOCKER_TIER=false
FETCH_BACKEND=hyperbrowser
HYPERBROWSER_MAX_CALLS_PER_PROPERTY=3
HYPERBROWSER_RESERVED_PRIORITY_CALLS=1
```

At the time of this note, no image was built, no deployment was changed, no
canary prefix was uploaded, and no job was launched.

## Local verification

- Focused recovery/profile/adapter gate: **230 passed, 10 skipped**.
- Full local suite: **9,916 passed, 48 skipped, 3 failed**. The three failures
  are pre-existing script-layout assertions: all eight unexpected root scripts
  exist on `origin/main`, `local_canary_attribution.py` also lacks its main
  guard on `origin/main`, and the failing structure test is byte-unchanged from
  `origin/main`.
- Ruff lint and format checks pass for every changed Python file.
- The affected-property manifest rebuild passes its byte-determinism check.
- The mismatch-dominant rematerialization is byte-identical to the prior local
  union: 1,996 profiles, 2,382 routes, summary SHA-256
  `1cbadb97ef367d3f68c55cc78c4087b372cce55b93233038f3f7dcd3af7f9a02`,
  and ledger SHA-256
  `e5fca4512ab392b4eafcadc57149c3f59dbf2819abb92d0b016d208c12c573e5`.
