# Availability-date residual-tier audit (read-only)

Capture: 2026-08-01. Historical baseline: 2026-07-31 SurgeX canary export.
This is a local live audit, not a canary or production result. LLM was off.
Three clean Hyperbrowser sessions were used only for the selected Entrata
conventional pages; CAPTCHA solving was hard-disabled. No Web Unlocker,
FlareSolverr, fingerprint rotation, or paid canary was used.

## Denominator and integrity

- Exact separate denominator: **78 unique properties / 573 native unit rows**.
- Historical output: **571 capture-date rows, 2 blank rows, 0 other dates**.
- Category populations: RealPage OLL 36/232; Entrata API 26/182; OneSite API
  5/38; OneSite Workflow 2/24; AspenSquare 8/89; Squarespace 1/8.
- Exact RP/native-unit matches for these 78 properties: **0**. RP therefore
  was not used as proof; all conclusions below come from current published
  sources.
- Probe identity: **18/18 exact configured-property matches**. Every observed
  RealPage marketing property ID (9/9), OneSite site ID (2/2), AspenSquare
  numeric property ID (3/3), and AspenSquare community hash (3/3) was unique
  to one selected property. Entrata and Squarespace were checked by configured
  URL plus visible/structured name and address.

## Current live result

| Family | Historical properties / rows | Probed | Data / no inventory | Properties with an explicit future | Native explicit-future rows | Current adapter preserves | Production Jugnu preserves | Missed | Current classification |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| RealPage OLL API | 36 / 232 | 4 | 3 / 1 | 3 | 20 | 20 | 20 | 0 | Current local parser and formatter preserve exact dates. Historical parser response-envelope/date-key gap is corrected locally. |
| Entrata API family | 26 / 182 | 3 | 3 / 0 | 3 | 29 | 29 | 29 | 0 | Dates are visible on the current SSR conventional route and preserved. July's undated API winner should be merged with or superseded by this dated route. |
| OneSite API | 5 / 38 | 5 | 4 / 1 | 2 | 23 | 23 | 23 | 0 | Same RealPage `response.units` / `internalAvailableDate` parser correction as OLL. |
| OneSite Workflow | 2 / 24 | 2 | 2 / 0 | 1 | 2 | 0 | 0 | 2 | Confirmed route-selection loss at 946 MLK: dates exist on a same-origin marketing unit route, while WorkflowStartup has no date field. |
| AspenSquare | 8 / 89 | 3 | 3 / 0 | 2 | 24 | 24 | 24 | 0 | Knock emits `availability_date`; the current alias-tolerant Jugnu formatter preserves it. Two far-future sentinel values remain correctly outside the normal future bucket. |
| Squarespace | 1 / 8 | 1 (complete population) | 1 / 0 | 1 | 3 | 3 | 3 | 0 | Visible `Available M/D` is normalized with the capture year; visible `Available Now` becomes capture date with explicit provenance. |
| **Total** | **78 / 573** | **18** | **16 / 2** | **12** | **101** | **99** | **99** | **2** | **98.0% of observed explicit futures survive the current local adapter + production formatter path.** |

The production-Jugnu replay covered 396 parsed adapter rows with zero trace
errors. It preserved **99/101** explicit-future rows exactly. The two misses
are units 308 (`09-09-2026`) and 209 (`09-06-2026`) at configured property
283561, 946 MLK. They are not parser-key or formatter losses: the existing
OneSite Workflow route never selects the marketing AJAX inventory carrying
those dates.

There were **32 capture-date defaults** in the primary live evidence:

- Entrata: 8 rows whose source says only “Only 1 Unit Available!” and gives no
  date.
- OneSite Workflow: 24 rows whose WorkflowStartup source has UnitIds/counts
  but no availability-date field.

Those are source-no-date defaults, not proof of missed future dates. At
Squarespace, two visible `Available Now` rows correctly resolve to capture
date with `available_now` provenance and are also not defects.

## Opportunity estimate

The hard, non-extrapolated opportunity in today's sample is **101 explicit
future rows across 12 properties**. The current uncommitted local changes
already preserve **99 rows across 11 of those properties**; one additional
same-origin OneSite route would recover the remaining two observed rows and
the twelfth property.

Directional property opportunity, if the selected current incidence were
representative of the historical populations, is approximately:

- RealPage OLL: 3/4 probes, directionally about 27 of 36 properties.
- Entrata: 3/3 probes, directionally up to 26 of 26 properties, but route
  selection and daily inventory make this the least certain extrapolation.
- OneSite API + Workflow: all 7 historical properties were probed; exactly 3
  currently expose explicit future dates.
- AspenSquare: 2/3 probes, directionally about 5 of 8 properties.
- Squarespace: the only property was probed and exposes future dates.

The tier-stratified point estimate is roughly **62 of 78 properties**, but it
must not be presented as a measured fleet rate: the sample is small/non-random
and availability changes daily. The defensible numbers are the 12/18 exact
live confirmations and the 99/101 current-path preservation result.

## Minimal code levers

1. Keep the uncommitted RealPage parser support for `response.units` plus
   `internalAvailableDate`; it is required by both RealPage OLL and OneSite API.
2. Keep the uncommitted production Jugnu alias lookup (`available_date`, then
   `availability_date` and vendor aliases) and availability provenance.
3. For Entrata, prefer/merge a dated same-property SSR conventional result
   when the winning API result contains inventory but no explicit dates.
4. Add the strict same-origin marketing inventory route used by 946 MLK to
   OneSite Workflow recovery, retaining site/name/address boundary checks.
5. Keep Squarespace's raw `Available Now` / `Available M/D` token through the
   adapter and normalize only in the formatter.
6. Preserve the source calendar-date prefix on timezone-bearing timestamps;
   do not convert move-in dates through UTC.

## One-day-shift check

No one-day conversion defect appeared in this live evidence. All 99 dates
that reached Jugnu equal the source calendar date exactly, including RealPage
timestamps such as `2026-08-15 00:00 -0500` -> `2026-08-15`. The current local
date parser deliberately preserves the source date prefix instead of applying
a timezone conversion.

## Git status

The availability changes are **uncommitted**. Branch
`codex/availability-date-preservation` and `origin/main` both point to
`02369d2827dd6bfe49e7abb8d32e028742ef8d6c`; the parser, formatter, adapters,
and tests exist only as dirty working-tree changes at audit time.

## Authoritative evidence

- `with_hyperbrowser/summary.json` — denominator, guardrails, per-tier totals.
- `with_hyperbrowser/july31_separate_denominator_property_ledger.csv` — exact
  78-property historical denominator.
- `with_hyperbrowser/current_live_property_audit.csv` — identity and property
  boundary evidence for all 18 probes.
- `with_hyperbrowser/current_live_unit_evidence.csv` — raw source value,
  adapter value, canonical formatter value, provenance, and classification.
- `with_hyperbrowser/jugnu_formatter_trace.csv` — independent production
  formatter replay for every parsed adapter row.
- `with_hyperbrowser/jugnu_formatter_trace_summary.json` — 99/101 result and
  zero trace errors.

Source hashes:

- July SurgeX CSV: `d0b270037a820717bd0f2ae5737e31f8babee07af3da7d56d01433ec0accd267`
- Property config: `086d922b6b41ac5c6b881b3509486feb1846b61812c14e89a3fcf794b4465628`
- Live unit evidence: `2d5125eaa795078ef70a1aad9f23b22b440e3863ae3a1064b60c410830c391f7`
- Live property audit: `6dff5411161738f3f92dfc1fbc4c25bcae72f335a65f9f4b7ad927111f01c7c3`
