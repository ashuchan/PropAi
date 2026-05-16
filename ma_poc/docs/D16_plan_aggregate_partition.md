# D16 — Plan-aggregate partition prompt for new session

Paste this into a fresh Claude Code session. It is self-contained.

---

## Task: Stop conflating plan-level summary rows with unit-level inventory in `result.units`

### Context (5-minute orientation)

Working directory: `c:\Users\ashus\OneDrive\Documents\Code\PropAi\ma_poc\`.

Read this first: `C:/tmp/rp_sx_analysis_2026_05_14/DETAIL_PAGE_GAPS.md` — the design history. Then `C:/tmp/rp_sx_analysis_2026_05_14/D678_CANARY.md` — the most recent canary results that flagged this gap.

This session is a follow-up to a series of fixes (B1-B5, D1-D15) shipped on 2026-05-16. The remaining systemic gap surfaced after the upstream fixes landed: a unit's run output `result.units` contains both unit-level inventory rows AND plan-aggregate summary rows that describe a floor plan typology rather than an available apartment. Manual-vs-our count comparisons inflate as a result.

### The problem in one example

**PID 65069 Olympic by Windsor** in the 2026-05-16 D-fix canary:
- Manual ground truth: **15** available units.
- Our latest output: **29 records**.
- Of those 29: **15 carry real natural unit_ids** (`618`, `414`, `518`, `526`, `330`, `230`, `415`, `652`, `252`, `354`, `244`, `444`, `419`, `634`, `348`) — these are the 15 real units, matching manual.
- The remaining **14 records have `inferred_*` unit_ids** and are plan-aggregate rows for plans the property advertises but currently has no available units (`2B4`, `1A7`, `1A6`, `1A2`, etc.). They came from JSON-LD `FloorPlan` typology or similar plan-level emissions.

The 14 plan-aggregate rows should live in `result.plan_summaries` (which already exists in `PostProcessResult`), NOT in `result.units`. Today they leak into `result.units` because the back-compat shim at `ma_poc/pms/adapters/generic.py:876-906` does `result.units = list(_pp.units) + list(kept_plans)` for the back-compat path.

This conflation makes "count of units" reports inflate, makes the dedup harder (plan rows pretend to be units and trigger ambiguity ranks), and obscures the true availability picture for downstream consumers.

### The architectural fix

The codebase already partitions correctly. Look at:

- `ma_poc/extraction/post_process.py` — `PostProcessResult` has separate `units` and `plan_summaries` fields. The `classify()` helper at `ma_poc/extraction/classify.py` already labels each row.
- `ma_poc/pms/adapters/generic.py:876-906` — the current B4 dedup logic concatenates `_pp.units + kept_plans` into `result.units`. The `kept_plans` should be assigned to `result.plan_summaries` only, never appended to `result.units`.
- `ma_poc/pms/adapters/base.py` — `AdapterResult` already has a `plan_summaries` field per the existing convention.

The fix is to:

1. **At the adapter layer (`generic.py:876`):** assign `result.units = list(_pp.units)` (unit-level only) and `result.plan_summaries = list(_pp.plan_summaries)` (after the existing fp_id-dedup). Drop the `+ kept_plans` concat.

2. **Audit every other adapter** that uses `post_process(...)` — the call sites are visible via `grep "post_process(" ma_poc/pms/adapters/*.py`. Each adapter is currently doing `result.units = _pp.admitted` (which sums both partitions). Change to assign units-only and plan_summaries-separately.

3. **Downstream readers** — the run-report and SLO watcher read `result.units` length as "units extracted". Decide one of:
   - **Option A (purest)**: keep `units` count strict (unit-level only). Update reporting/run_report.py to also emit a `plan_summaries_count` field. Downstream metrics distinguish the two.
   - **Option B (back-compat)**: leave reporting as-is, just stop conflating in the data model. Frontend / DB layer reads `.admitted` when it wants the union and `.units` when it wants strict unit-level.

   The 2026-04-19 BRD says `units[]` is the property's available-unit list. Option A is more correct.

4. **Update the canary diff tool** so it knows which property has plan-only data vs unit-level data. `ma_poc/scripts/diagnostics/local_canary.py` currently reports a single `units` count.

### Verification expectations

After the fix lands, re-run the 2026-05-16 canary manifest at `ma_poc/data/canary/local_runs/2026-05-16_d13_d14/canary_input.csv` and verify:

- **PID 65069 Olympic by Windsor**: `result.units` length goes from 29 → 15 (the 14 plan-aggregate rows move to `result.plan_summaries`).
- **PID 2982 Cortland on Pike**: should stay near 79-81 (all unit-level — D11 fix makes the canonical inventory page yield 79 real `data-unit-id` cards; minimal plan-aggregate leakage expected).
- **PID 67327 Windsong Estates**: `result.units` length should drop from 17-34 (depending on which canary) toward manual=18 if plan-summaries were inflating it.

Canary success criterion: at least 3 of the OVERCOUNT PIDs (`67327`, `65069`, `161`) move CLOSER to manual without regressing the UNDERCOUNT wins (Cortland, Brook, Izzy).

### Tests required

Add to `ma_poc/tests/pms/adapters/`:

1. `test_d16_plan_summary_partition.py` — assert that after `post_process` partition, `result.units` contains ONLY rows where `classify()` returns `"unit"`, and `result.plan_summaries` contains ONLY rows where `classify()` returns `"plan"`. No row appears in both.

2. Augment `test_2026_05_16_d_fixes.py::TestB4_PlanRowDedupAgainstUnits` to verify the dedup still drops plan-summaries that duplicate unit-level records by `floor_plan_id` — that B4 behaviour must survive D16.

3. End-to-end against a real per-property HTML fixture (e.g., the Olympic by Windsor capture if available, or a synthetic one with 5 unit-level + 5 plan-aggregate JSON-LD records) — assert the partition is correct.

### Files likely to touch

- `ma_poc/pms/adapters/generic.py:876-906` (primary fix site)
- `ma_poc/pms/adapters/entrata.py`, `rentcafe.py`, `appfolio.py`, `amli.py`, `avalonbay.py`, `funnel.py`, `sightmap.py`, `wix.py`, `squarespace.py`, `onesite.py` — all call `post_process(...).admitted` and need the same change
- `ma_poc/reporting/run_report.py` — needs new `plan_summaries_count` field (Option A) or just docstring update (Option B)
- `ma_poc/observability/slo_watcher.py` — verify it reads the right field
- `ma_poc/scripts/diagnostics/local_canary.py` — surface plan_summaries_count in the canary report
- `ma_poc/tests/pms/adapters/test_d16_plan_summary_partition.py` — new file
- Frontend / DB layer (if applicable — search `grep -r ".admitted\|result.units\|plan_summaries" ma_poc/frontend/ ma_poc/data_provider/`)

### Don'ts

- Don't change the `classify()` rules — they're already correct (`unit` when natural identity present OR per-unit signals; `plan` otherwise).
- Don't remove plan_summaries from the output; downstream consumers may want them.
- Don't try to fix the inferred-id-hash-collision issue here — that's orthogonal (D6 / D11 follow-ups).

### Out of scope

- Building the hy.ly Marketing Cloud adapter (D15 / Sound at Peninsula). Separate workstream.
- Investigating the R1 ambiguity over-collapse on Canyon Ridge (D12 follow-up). Separate workstream.
- Touching the URL-shape scoring (D2 / D11) or DOM compact-row extractors (D13). Those are done.

### Definition of done

- Tests pass: `pytest ma_poc/tests/pms ma_poc/tests/extraction ma_poc/tests/services/test_source_planner.py ma_poc/tests/integration -q` exits 0 except the pre-existing `test_h5_visited_urls_dedupe` failure.
- New `test_d16_plan_summary_partition.py` tests pass.
- Canary re-run on the 10-PID manifest shows Olympic / Windsong / Hickory Mill moving CLOSER to manual with no UNDERCOUNT regressions.
- One-paragraph summary written to `C:/tmp/rp_sx_analysis_2026_05_14/D16_CANARY.md` documenting the per-PID before/after.

---

**Hand-off context** — you'll find this file at `C:/tmp/rp_sx_analysis_2026_05_14/D16_PROMPT_FOR_NEW_SESSION.md`. Read it through, then start a new branch (`d16-plan-summary-partition`), implement, test, canary, and report back.
