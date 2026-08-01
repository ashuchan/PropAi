# Pedcor / BetterNOI unit-identity guardrail

Date: 2026-08-01  
Scope: exact 2026-07-31 `FAILED_NO_DATA` cohort  
Disposition: keep the four audited Pedcor properties at plan level; do not
credit them as unit-level recoveries.

## Why this note exists

Two plausible-looking fields and one plausible-looking query can manufacture
false unit coverage:

1. Pedcor's `window.floorplans[*].ApartmentId` looks like an apartment/unit ID,
   but it is a community-level ID repeated on every floor plan.
2. BetterNOI accepts a scalar `client_ids=<numeric-id>` query without applying
   that filter. The response is successful but portfolio-wide.

The second failure mode returned 39,710 available units, with foreign Riverdale,
Georgia rows first, for each of three unrelated Pedcor targets. A `200` response,
positive rent, unit UUID, and unit number are therefore insufficient evidence
unless the request is demonstrably property-scoped.

## Pedcor schema roles observed

Across PIDs `20262`, `45755`, `49921`, and `254122`, 14
`window.floorplans` rows had these roles:

| Field | Actual role | Unit-level? |
|---|---|---|
| `ApartmentId` | Repeated marketing community ID (`481`, `443`, `480`, or `487`) | No |
| `Id` | Floor-plan ID | No |
| `UnitCode` | Floor-plan/type code when present | No |
| `UnitTypeCode` | One or more floor-plan type codes | No |
| `has_availability` | Plan-level boolean | No |
| `NumAvailable` | Plan-level count; zero throughout this audit | No |

For Kings Mill, two plan rows said `has_availability=true`, while
`NumAvailable=0` and the public application surface exposed no native unit
roster. The boolean must not be promoted into a physical unit.

## BetterNOI parameter trap

The BetterNOI application UI uses an array parameter:

```text
client_ids[]=<numeric-client-id>
```

For the three Pedcor BetterNOI application pages, that exact UI-shaped request
returned zero rows. By contrast, this scalar form was silently ignored:

```text
client_ids=<numeric-client-id>
```

It returned the same 39,710-row portfolio universe for every target. Never use
the scalar form as a property boundary, and never treat a successful response
as proof that a server applied the supplied filter.

The application-page `key` is also not the public unit API's `client_uuid`.
Trying it as one returned zero and does not establish a mapping.

## Required fail-closed acceptance gates

A BetterNOI unit recovery is admissible only when all of the following hold:

- The exact property page publishes one unambiguous BetterNOI `client_uuid`.
- The page also publishes the expected floor-plan UUIDs (for example through
  `data-property` and `data-fpcode` markers).
- The request uses that exact `client_uuid`; no guessed ID or scalar
  `client_ids` fallback is allowed.
- Every returned row has a native unit UUID/number and positive rent.
- Every returned row's client UUID and floor-plan UUID belong to the
  page-published set.
- Every returned building address, city, state, and ZIP matches the configured
  property.
- Counts, uniqueness, and pagination are complete; mixed or foreign rows reject
  the entire recovery rather than being filtered opportunistically.

If any page binding or response-boundary gate is absent, retain only the plan
catalogue and report no unit-level recovery.

## Positive boundary control

Westwood Village (PID `42571`) demonstrates the valid path. Its exact floor-plan
page publishes one client UUID and two floor-plan UUIDs. The scoped public unit
request returned one native unit (`C-06`) with a unique UUID, rent `$1,230`, an
explicit `2026-10-02` availability date, and the exact Panama City, Florida
property address. This proves that the strict method succeeds when the required
property binding is genuinely published.

## Audit evidence

- Evidence artifact:
  `/private/tmp/propai-fnd-vBkmT9/pedcor_residual_parallel/pedcor_four_application_links_live_audit.json`
- SHA-256:
  `5e862b182e0c7001a68347cb3ab9d7a52e1c7be0d029169e692864ecd11d2fab`
- Audit conditions: direct HTTP only; no CAPTCHA solving, Web Unlocker,
  FlareSolverr, fingerprint rotation, Hyperbrowser, LLM, paid canary,
  authentication, or application submission.
