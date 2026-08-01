# RentCafe hosted-table wait-list sentinel guardrail

Date: 2026-08-01

Disposition: keep Cooper's Landing (cohort property `218786`) out of the
unit-level recovery ledger. Its 27 RentCafe-hosted rows are priced wait-list
application placeholders, not physical apartments.

## What looked recoverable

The exact RentCafe-hosted page is property-scoped to Cooper's Landing,
property ID `480033`, and exposes 27 `tr.fp-unit` rows with stable-looking
`data-unit-id` values, rents, bedrooms, bathrooms, and square footage:

`https://www.rentcafe.com/apartments/mi/kalamazoo/coopers-landing-apartments/default.aspx`

That shape normally represents real RentCafe inventory. Treating the native
ID plus rent as sufficient would therefore have admitted the property.

## Why the rows are not units

Every visible `data-unit-name` is a wait-list/application sentinel. Observed
examples include `WAIT1BD`, `WAIT1BDL`, `WAIT1_TH`, `WAIT2BED`, `WAITAPP`, and
`WAIT3BD`. No row exposes a physical apartment number. The IDs identify Yardi
application/wait-list records; they do not turn the labels into apartments.

The current hosted-table parser fails closed for any normalized unit label
beginning with `WAIT`. On the captured page it returns `0` physical units from
all `27` `fp-unit` rows.

## Evidence

- Hyperbrowser capture summary (CAPTCHA solving, stealth, and fingerprint
  rotation all disabled):
  `/private/tmp/propai-fnd-vBkmT9/hb_rentcafe_hosted_pair/summary.json`
- Captured exact-property HTML:
  `/private/tmp/propai-fnd-vBkmT9/hb_rentcafe_hosted_pair/218786_root.html.gz`
- Three-property hosted-table boundary audit (Tamarron positive control,
  Cooper's Landing wait-list negative, Spring Hill empty negative):
  `/private/tmp/propai-fnd-vBkmT9/rentcafe_hosted_table_lane/evidence_tamarron_34362_current_strict.json`
- Parser guard:
  `ma_poc/pms/adapters/_rentcafe_hosted_table.py`
- Regression test:
  `ma_poc/tests/pms/adapters/test_rentcafe_hosted_table.py`

## Reusable rule

Do not infer physical unit identity from a provider-native ID alone. For
RentCafe hosted tables, reject `WAIT*` labels even when the row has a positive
rent and apartment-like dimensions. Admit only a non-placeholder visible unit
number together with the native apartment ID and property-bound source route.
