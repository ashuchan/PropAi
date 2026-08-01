# Easton North SecureCafe sibling-contamination guardrail

Date: 2026-08-01

## Verdict

FAILED_NO_DATA property 27080 (Easton North) is **not** a strict recovery.
The three apparent native units belong to a different Oxford Realty property,
Cedar Lane. They must not enter the recovery ledger.

## Exact property boundary

Configured cohort identity:

- Easton North
- 1126 Easton Avenue, Somerset, NJ 08873
- `https://www.oxfordrealtygroup.com/communities/easton-north/`
- Exact first-party leasing route:
  `https://oxfordrealtygroup.securecafe.com/onlineleasing/easton-north/oleapplication.aspx?stepname=floorplan`

The rejected roster came from:

- `https://oxfordrealtygroup.securecafe.com/onlineleasing/cedar-lane/availableunits.aspx`
- Units 190B, 114A, and 190D
- Cedar Lane's operator page identifies it as 100 Cedar Lane, Highland Park,
  NJ 08904.

Those city, ZIP, street, property name, and portal slug differences prove a
sibling-property leak rather than a rebrand or shared-property roster.

## Root cause

When the rendered property body contained no regex-visible SecureCafe link,
`_try_rentcafe_securecafe_probe` refetched only the scheme and host. For a
path-scoped portfolio property, that changed the fallback target from the exact
property page to the operator root. The root could publish several sibling
SecureCafe slugs; the adapter accepted the first sibling that happened to have
`AvailUnitRow` inventory.

The current scan artifact showing the false candidate is:

- `/private/tmp/propai-fnd-vBkmT9/rentcafe_remaining_after_current_fixes_scan.json`

Independent exact Easton North Hyperbrowser evidence is:

- `/private/tmp/propai-fnd-vBkmT9/hb_securecafe_residual5/27080.html`
- `/private/tmp/propai-fnd-vBkmT9/hb_securecafe_residual5/summary.json`

The exact Easton portal page identifies Easton North but currently exposes
waitlist/application floor-plan rows, not an `AvailUnitRow` unit roster.

## Guardrail implemented locally

The fallback now refetches the effective property URL with its path and query,
instead of collapsing it to the portfolio origin. This preserves an exact
property boundary and prevents the empty Easton route from falling through to
Cedar Lane.

The same exact-page/portal relationship was live-checked on three Oxford
portfolio members before generalizing the change:

1. Easton North -> `/onlineleasing/easton-north/`
2. Cedar Lane -> `/onlineleasing/cedar-lane/`
3. Forest Glen -> `/onlineleasing/forest-glen-7/`

Focused validation:

- 72 RentCafe/SecureCafe tests passed.
- The regression test proves the exact Easton page is refetched, the operator
  root is never requested, and Cedar Lane is never probed.
- No CAPTCHA solving, Web Unlocker, FlareSolverr, fingerprint rotation, LLM,
  paid canary, commit, or push was used.

