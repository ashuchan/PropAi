# Consolidated recovery handoff — 2026-08-01

This directory freezes the stopped worker streams before the consolidated GCP
canary. It is intentionally separate from the production code: the code gains
are integrated on `codex/consolidated-canary-2026-08-01`, while this directory
holds the admissions, negative controls, replay programs, hashes, and run
provenance needed to audit or resume the campaign.

## Frozen state

| Stream | Strict state before consolidated canary | Provenance |
|---|---:|---|
| Failed-no-data | **244/344 (70.93%)** | 243 rows in the generated authoritative ledger plus the separately admitted Tuscany Hills 3× replay supplement |
| Plan→unit | **365/549 (66.48%)** | local strict discovery counter; not a GCP canary result |
| Prior plan-cohort GCP canary | **303/549 (55.19%)** | `SUCCESS`, non-empty `units`, and `real_id_units > 0` |
| Prior canary reusable profiles | **243 actionable / 60 bootstrap-only** | generation-pinned read of the 303 strict profiles; includes three availability-link-only routes omitted by the older classifier |
| Shared profile promotion | **237 created / 6 field-merged / 0 missing** | guarded execution manifest plus independent post-write listing; shared numeric profile count is now 887 |

The plan worker's last single materialized ledger contains 330 rows. It predates
35 later strict admissions, including the final Spherexx/Hurston admission. The
365 counter is therefore retained as a worker-state fact, but it must not be
presented as a fully reconciled ledger or canary outcome. The consolidated
full canary is the authoritative reconciliation step.

## Post-freeze property-identity hardening

After this worker snapshot was frozen, the confirmed wrong-property routes
were closed with a shared vendor-metadata identity gate, property-scoped route
quarantine, sibling-community filtering, Edifice multi-UUID verification, and
actual unit-response provenance. A create-only July candidate profile prefix
was also materialized without switching production or starting the full paid
canary. See
[`PROPERTY_IDENTITY_AND_JULY_PROFILE_VETTING.md`](PROPERTY_IDENTITY_AND_JULY_PROFILE_VETTING.md)
for the exact 3,449-object write, hashes, controls, and limitations.

A read-only retrospective identity audit is complete. Its first,
highest-signal wave checked all 877 profiles whose SightMap, Knock, or Edifice
route publishes property metadata. The archive-plus-current-response pass then
materialized a local positive-only candidate of **2,566/3,449 (74.40%)**,
quarantined 51, and withheld 832 for review. Unit-row agreement and RP data are
not admission evidence; only independent source property identity can admit a
route. The shared profile store was not changed. See
[`STRICT_WARM_PROFILE_IDENTITY_AUDIT.md`](STRICT_WARM_PROFILE_IDENTITY_AUDIT.md)
for the first wave and
[`STRICT_WARM_PROFILE_CANDIDATE_V2.md`](STRICT_WARM_PROFILE_CANDIDATE_V2.md)
for the final local candidate, exact route ledger, and counts.

## What is preserved

- `worker-archive/failed-no-data/` contains the ledger builder, all worker
  replay/materializer programs, CSV ledgers, strict evidence JSONs, admissions,
  rejections, summaries, and negative-control documentation. Large raw HTML,
  response bodies, virtual environments, and bytecode are intentionally
  omitted; they are not required to reproduce an admission and would obscure
  the evidence set.
- `worker-archive/plan-to-unit/` contains the exact 549-property cohort, the
  last materialized local ledger, remaining-opportunity ledger, corrected
  303-success GCP audit, strict ID set, Cloud Run job definition/build helpers,
  and all 98 shard reports. The 98 raw property/event shard objects remain at
  the GCS run prefix recorded in the audit.
- `profile-snapshot/` is populated by
  `ma_poc/scripts/backfills/promote_strict_canary_profiles.py`. The manifest records GCS
  object generations, SHA-256 hashes, route signals, create/merge partitions,
  and write results. Raw profile JSON is locally retained for replay but is
  git-ignored because some vendor URLs carry public query credentials.
- The focused integration gate is **523 passed, 1 skipped**. The canonical full
  local suite is **9,640 passed, 48 skipped**, with only the same three
  script-layout failures reproduced on clean `origin/main`. A separate
  availability/property-boundary gate is **403 passed**. The consolidated GCP
  canary remains a separately labeled result.

## Warm-profile policy

Only strict unit successes with a concrete replay route are promotable. A
profile containing only entry/bootstrap metadata is archived but not written.
New profile objects are create-only. Existing profiles are generation-guarded
field merges: the organic winner and history remain authoritative, while new
canary winners/endpoints are appended as alternate routes. No plan-only,
contaminated, missing-real-ID, or explicit shape-overcount result can enter the
promotion set.

That policy has now been executed for the prior 303-success GCP run. The
reviewed dry-run manifest and execution manifest are separate immutable audit
records under `profile-snapshot/`; 237 profiles were created and six overlaps
were field-merged. All 243 target IDs were independently found after the write.
The 60 bootstrap-only profiles remain in the archive and were not promoted.

`shared_profile_coverage_audit.json` records the cohort intersection after the
write. The 243 promoted profiles belong to the plan cohort's strict GCP run;
they are not the failed-no-data ledger. Of the frozen 244 failed-no-data local
successes, 18 currently have a shared profile and 11 of those contain an
actionable route. The failed-no-data cohort and plan cohort do not overlap.

Local probe successes are not synthesized directly into the shared store when
their exact HTTP method/session contract is ambiguous. The consolidated canary
will generate production-shaped profiles for those wins; its strict outputs and
profiles will then pass the same gate before promotion.

## Important negative findings

The negative controls are part of the deliverable, not discarded failures.
They include the Pedcor portfolio-wide BetterNOI leak guard, RentCafe waitlist
sentinel, MRI ProspectConnect waitlist/plan guard, and SecureCafe sibling-property
boundary. These prevent a higher headline conversion rate from shipping
another property's or a portfolio's inventory.

Run `python build_archive_manifest.py` after adding evidence. It writes a
deterministic `SHA256SUMS.json` for every retained file except raw profile
payloads and the manifest itself.
