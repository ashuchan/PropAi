# Staging demo backfills

The staging demo tenant gets its tours from production every day (`ontour-test-sync-staging-merge`). Production doesn't run some staging-only analysis, so those tours arrive without it. These backfills fill the gaps on **staging only**.

```bash
investigations/2026-11-16-demo-readiness/demo_backfills/run.sh evidence sample                       # plan only (no model calls, no writes)
investigations/2026-11-16-demo-readiness/demo_backfills/run.sh evidence sample --apply --undo <new>   # 3-tour sample
investigations/2026-11-16-demo-readiness/demo_backfills/run.sh evidence all --apply --undo <new>      # the rest
```

## Coaching-note evidence (`coaching_evidence_backfill.py`)
**Fills:** `tour_analytics.what_went_well_evidence` and `areas_for_improvement_evidence` (aiservice v21). These are the jump-to-transcript chips on "What went well" and "Growth areas" in a tour review.

**How:**
1. One Gemini Flash call per tour (`gemini-3.7-flash`, temperature 0). It receives the stored transcript turns and the stored bullets, and returns one verbatim quote per bullet, or null.
2. aiservice's own `agents.coaching_note_evidence` (`build_evidence_map` + `resolve_all`) places the quotes, exactly as a pipeline run would. A quote that isn't an exact substring of exactly one turn becomes `{turn_index: null}` and shows no chip.

**Safety:**
- Refuses any database that isn't `*_staging`.
- Writes only rows where both evidence columns are NULL, so it's idempotent.
- Writes the undo file (tour ids) before any write.
- Read-only unless `--apply`; stops at `--cap-usd` (default $5).

**First run (2026-11-16):**
- 54 tours: 251 notes, 215 placed, 36 left null.
- About $0.45 estimated: roughly 494k input tokens and 121k output tokens at $0.30 / $2.50 per million.
- Sample read by eye on Parker's tour review: each chip lands on the turn that supports its note.
- The 3 tours left NULL are non-demo "couldn't process" tours with no transcript or notes.

**Why it sticks:** production has no `*_evidence` columns, and the staging merge writes only the columns it receives. So once written on staging, the evidence survives every merge. (Tour measures are different: they live inside `analysis_details`, which the merge rewrites. ontour-ops #105 `CARRY_NESTED` keeps them.)

## Tour measures
Run with aiservice's own `scripts/tour_measures_rerun.py` (origin/staging), with `TOUR_MEASURES_MODE=on`, `--missing --tour-ids …`. Use a fresh gcloud token, because stale ADC fails with `RefreshError`. First run (2026-11-16): 100 demo tours, all `ok=true`, about $2.40 estimated.

## Requirements
- `AISERVICE_REPO`: an aiservice checkout at origin/staging (default `~/ontour-private-evidence/2026-11-16-demo-readiness/aisvc-staging`).
- `AISERVICE_PY`: a Python with google-genai and psycopg2.
- `DEMO_SYNC_REPO`: ontour-ops, for the staging DB config.
- gcloud as `claude-ontour`.
- Candidate lists and undo files stay in the private folder (`DEMO_PRIVATE`), never in the repo.

## Going forward (proposal, not built)
New demo tours arrive daily (about 5–25 a day). The cheapest durable option is a **post-merge step in the staging merge job**, after the existing rescrub and A9 steps:
1. Select demo tours from the last 2 days that have no `tour_measures` or NULL evidence.
2. Run both passes, with a daily cap (e.g. 40 tours and $2 a day). That costs about $0.02 (measures) + $0.008 (evidence) per tour, so roughly $0.20–0.75 a day.

**What it needs:**
- The test-sync job's service account needs Vertex AI user access.
- The job needs aiservice's `agents.tour_measures` and `agents.coaching_note_evidence`: vendored, as a pip-installable package, or by calling a staging aiservice endpoint.

**Fail-open:** a failure leaves the tour unmeasured; the merge itself never fails.

## Units tab seed (`units_seed.py`, 2026-11-16) and Maple Grove gold (`maple_grove_gold.py`, 2026-11-17)

Staging only, demo company 3360791e. Undo files are in `~/ontour-private-evidence/2026-11-16-demo-readiness/`:
- `undo-units-seed.json`: the first seed of all 6 communities, plus the Prestige Park applicant names and the June report.
- `undo-units-seed-maple-grove-replaced.json` and `undo-units-seed-maple-grove.json`: Maple Grove's seed rebuilt from the rescaled gold.
- `undo-units-seed-plan-order.json`: seeded plans renumbered A1 < A2 < … by rent.
- `undo-maple-grove-gold.json`: every original Maple Grove row of `gold.availability_unit_current`, `unit_status_history`, `availability_daily` and `availability_by_plan_daily`.

What keeps or breaks them:
- **The staging merge (06:30 CT)** doesn't touch the availability tables or gold, so both stay.
- **`waymaker-demo-refresh-staging` (10:00 UTC)** only shifts the seeded gold rows' dates, so the Maple Grove values stay.
- **Re-running `ontour-etl-pipeline tools/demo_waymaker_availability.py`**, or a staging medallion run that rebuilds WayMaker's gold, replaces Maple Grove's gold with the source community's rents and occupancy. Run `maple_grove_gold.py --commit --undo <new file>` again afterwards. It skips if the median rent is already under $3,000.
- **After any gold rescale**, rebuild that community's seed: `units_seed.py --replace --community <id> --commit --undo <new> --undo-replaced <new>`. It compares the seed it would write now (plan code, name, unit, rent) with the current one: identical is skipped, otherwise the old seed's rows go to `--undo-replaced` (written before any delete), are deleted, and the community is re-seeded. Buildings are reused. Plan names stay generic.
- **The seed's dates** are relative to its report date. `demo-fresh` moves them to today once the job's login (`test_sync_staging_writer`) has SELECT and UPDATE on `availability_reports`, `unit_availability` and `floor_plan_availability_summary`. Until then it skips. The dates stay plausible for weeks.

## Rent rescale for any demo community (`demo_gold_rescale.py`, 2026-11-17): Prestige Park

Generalises `maple_grove_gold.py`'s rent step: rents only, occupancy and every count unchanged.

```bash
python demo_gold_rescale.py --community <id> [--band 1750 1950]                     # dry run
python demo_gold_rescale.py --community <id> --commit --undo <new file>             # write
python units_seed.py --replace --community <id> --commit --undo <new> --undo-replaced <new>   # then the Units tab
```

- **How:** one factor = band midpoint / the latest day's `gold.availability_daily.avg_market_rent`, applied to every rent column of `availability_unit_current`, `unit_status_history`, `availability_daily` and `availability_by_plan_daily` (demo seed rows only). Plan order, the spread between plans and each unit's place within its plan are kept. The Units seed adds its own +/-3% per unit.
- **Safety:** staging DB only; company 3360791e only; every touched row must carry the demo seed's job_id; dry run unless `--commit`; the undo file (every original row, 0600, never overwritten) is written before the first UPDATE; the after-value is checked against the band, else rolled back.
- **Idempotent:** skips when the latest-day average is already in band, so a second run changes 0 rows.
- **`demo_seed`:** no rent columns (`unit_attributes` etc. are keyed by gold unit_key, which a rescale does not change), so nothing to redo there.

**Prestige Park run (2026-11-17 04:45 CT):** factor 0.5972, latest-day average $3,097.85 -> $1,850.04; rows changed: units 601, unit history 1,363, daily 53, by-plan 364 (non-null rents). Occupancy 95.33% unchanged. Units seed replaced (52 units, 8 plans, rents $1,247-2,392). Undo files are in `~/ontour-private-evidence/2026-11-17-data-sufficiency/undo/`:
- `undo-prestige-park-gold-rents.json`
- `undo-units-seed-prestige-park-replaced.json` and `undo-units-seed-prestige-park.json`

The re-seed and medallion caveats above apply: re-run both commands after either.

## Unit tagging demo data (`unit_tagging_seed.py`, 2026-11-17)

Four tables in `demo_seed` (staging-only, synthetic), keyed by gold's `unit_key` (U####, the same key the tour map's unit taps use):
- `unit_attributes`
- `tour_unit_shown`
- `tour_lease_unit`
- `tour_unit_reactions`

Notes:
- **Quotes** are only each tour's own verbatim `feature_sentiments` quotes. The story bias comes only from which homes a tour is shown.
- **Re-runs** write only differences, and a second run changes 0 rows.
- **Undo:** `~/ontour-private-evidence/2026-11-16-demo-readiness/undo-unit-tagging-seed.json`.
- **The 06:30 merge** never touches `demo_seed`, and tour ids are stable.
- **Re-run after:** the gold seed tool or a staging medallion run, since either re-keys gold units.
- **Post-demo:**
  - a nightly run in demo-fresh;
  - re-keying the app's Units tab to gold U#### keys, with plan names kept generic.
