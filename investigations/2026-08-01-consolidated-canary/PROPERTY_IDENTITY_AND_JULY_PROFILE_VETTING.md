# Property identity hardening and July profile vetting — 2026-08-01

## Outcome

The unit-acceptance path now rejects vendor/API rosters that identify a
different property, and the confirmed contaminated warm routes are quarantined
per property. Historical profile stores were not deleted or overwritten.

A separate versioned, create-only July profile namespace was created at:

`gs://jugnu-canary/profiles/july-vetted-2026-08-01-v1/`

No production job was switched to this prefix, and no paid full canary was
started as part of this pass.

## Identity policy

The shared matcher accepts a roster when either:

1. the vendor property name is an exact or safe multi-token match; or
2. the street address has the same house number and a strong street match.

The address rule deliberately preserves legitimate branding variants such as
`Ridgewood Apartments` versus `(RDG) Ridgewood Court` at the same street
address. A one-token brand overlap is not enough: `Novi Flats` does not match
`NOVI Rise`. Roman and Arabic phase numbers are canonicalized, so `Turtle Dove
I` matches `Turtle Dove 1`, while an explicit `I` versus `2` phase conflict is
a hard mismatch.

Detached warm SightMap and Knock replays require a positive identity match
when CSV identity is available. In-page SightMap captures reject explicit
mismatches and retain `UNKNOWN` as observable provenance if an old envelope
does not publish asset metadata. Every candidate Edifice UUID is queried and
checked against the returned `property` name before its catalog or units can be
accepted.

## Confirmed route quarantine

| Property | Confirmed bad route | Live identity / reason |
|---|---|---|
| Novi Flats (`264077`) | SightMap `yjp2415rvxl/sightmaps/104541` | `NOVI Rise` |
| Brookside Commons (`49364`) | SightMap `m9pzdr7mvk1/sightmaps/77845` | `Kelson Row` |
| Turtle Dove I (`222652`) | Knock property `2016765`; Edifice UUID `318beef3-c0ee-4d07-a9c7-a9624bb13238` | `The Onyx` in Las Vegas; `Turtle Dove 2` |
| Golfside Lake (`22187`) | McKinley `/ann-arbor/glencoe-oaks/` link | sibling-community recommendation |

The quarantine clears the matching winner, availability link, API endpoint,
LLM mapping, field patch, or cached API verdict only for that canonical
property. The route is also rejected at the direct-dispatch and profile-write
boundaries so it cannot immediately re-poison the store. The profile is
demoted to `COLD`; unrelated routes and all historical source namespaces are
preserved.

## Multi-property live controls

The policy was based on multiple controls rather than a single example:

- SightMap: Novi Rise (negative), Kelson Row (negative), Brookside Commons
  (positive), Modera Montville (positive), and Bowery West (positive).
- Knock: The Onyx/Turtle Dove (negative), Post House (positive), Chandler's
  Bay (positive), and Ridgewood Court (positive by address).
- Edifice: Turtle Dove 1 (positive), Turtle Dove 2 (negative phase), and
  Cobblestone Apartments (positive independent control).

The sibling-card scope filter was also tightened: off-scope community cards
are excluded, generic URLs such as `/contact-us` no longer manufacture a
sibling verdict, three-character property slugs are recognized, and a sole
surviving card is kept only when it has explicit in-property evidence.

## Actual response provenance

Accepted unit sources now emit:

- provider and response kind;
- sanitized source URL (credential-like query values are redacted);
- HTTP status;
- SHA-256 of the actual response body;
- unit count; and
- the property-identity verdict and evidence.

The body itself is not duplicated into provenance. Provenance survives
cross-page/link-hop merging and is surfaced under
`_meta.provenance.unit_source` in Jugnu output.

## July snapshot gate and exact results

Source artifacts:

- run: `gs://jugnu-canary/runs/2026-07-31-fetchfix-5k/`
- source profiles: `gs://jugnu-canary/profiles/fetchfix-warm/`
- cohort: 4,982 unique properties
- cohort SHA-256:
  `fc4959327f07a05e08a68cf1e1866ddf7968a916b9e967f9818391f3e154a8fa`
- run gate: `SUCCESS`, non-empty `units`, and `real_id_units > 0`

| Gate | Count |
|---|---:|
| Strict July outputs before confirmed exclusions | 3,891 |
| Confirmed wrong-property outputs excluded | 4 |
| Strict outputs after exclusions | 3,887 |
| Profiles with a concrete reusable route | 3,449 |
| Bootstrap-only profiles archived, not promoted | 438 |
| Target objects before write | 0 |
| Create-only writes | 3,449 |
| Merge/overwrite writes | 0 |
| Missing or unexpected objects after independent listing | 0 |
| Stored/source hash mismatches | 0 |

Audit records:

- dry-run manifest SHA-256:
  `4d08a3cbe2655b12b7d102aadccd323141976eb9a6132345943f37b465658063`
- execute manifest SHA-256:
  `b746d9e0092cd85ef0892683a82478f04af80e60d9018035794751e5c4a0bbde`
- local manifests:
  `july-vetted-profile-snapshot-v1/promotion_manifest.json` and
  `july-vetted-profile-snapshot-v1/promotion_manifest_execute.json`

The 3,449 historical profiles are **strict-output and known-contamination
vetted**, not retrospectively live-probed one by one under the new identity
gate. The next targeted/full canary is what will produce authoritative
per-response identity provenance for the whole cohort. Until then, the old
`fetchfix-warm` namespace remains historical evidence and the new prefix is a
reversible candidate store, not a production activation.

Follow-up on 2026-08-01 confirmed that this limitation is material: the first
877-profile live metadata audit found stale cross-property routes. The exact
results and conservative 834-profile clean floor are recorded in
[`STRICT_WARM_PROFILE_IDENTITY_AUDIT.md`](STRICT_WARM_PROFILE_IDENTITY_AUDIT.md).

## Validation

- Focused direct/adapters/profile/provenance gate: **336 passed**.
- Full local PMS + profile + services gate: **5,366 passed, 7 skipped**.
- Canonical repository suite (`ma_poc/pytest -q tests`): **9,662 passed,
  48 skipped**, with only the same three pre-existing script-layout failures
  already reproduced on clean `origin/main`.
- GCS guarded write: **3,449 created**, every object re-read, schema-validated,
  and byte-hash checked.
- Independent immediate-prefix listing: **3,449 expected, 3,449 actual**, with
  none of `264077`, `49364`, `222652`, or `22187` present.

The full paid canary remains intentionally paused.
