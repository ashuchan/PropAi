# MRI ProspectConnect waitlist-plan guardrail

## Finding

Princeton Management publishes exact, property-scoped MRI ProspectConnect
routes. The ordinary public flow (`GET /Search/Index/<community>` followed by
the CSRF-protected `POST /Search/Search`) can return priced floor-plan cards
even when it publishes no native unit inventory.

The plan-card identifiers (`data-unittypeid`) are floor-plan/type identifiers.
They must not be promoted to unit IDs. A strict unit requires a native
`button[data-unitid]` row, positive rent, dimensions, and the configured
property identity boundary already enforced by `mri_prospectconnect.py`.

## Live controls — 2026-08-01

| Configured property | Community | Plan cards | Waitlist buttons | Native `data-unitid` rows | Strict units |
|---|---:|---:|---:|---:|---:|
| Woodbridge Manor Apartments | `015` | 7 | 7 | 0 | 0 |
| Custer Crossing Apartments | `404` | 5 | 5 | 0 | 0 |
| Trafalgar Square Apartments | `065` | 5 | 5 | 0 | 0 |
| Fox Lane Apartments | `036` | 4 | 4 | 0 | 0 |

All four index and search requests returned HTTP 200. The configured marketing
pages published the corresponding MRI routes, and the provider headings and
community codes matched the intended properties. The negative result is an
inventory-depth result, not a reachability failure.

## Operational implication

- Keep these properties as plan-level/current-no-native-unit outcomes.
- Do not synthesize one unit per plan or use `data-unittypeid` as a unit key.
- Re-probing later is safe because the public MRI response can begin returning
  native unit rows without requiring an adapter-shape change.
