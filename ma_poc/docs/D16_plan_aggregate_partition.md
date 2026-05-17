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

5. **Promote `unit_number` to a first-class identity key in `classify`.** In `ma_poc/extraction/classify.py::_has_natural_unit_identity` (lines 59-84), a row currently fails the natural-identity check whenever `unit_id` is missing or starts with `inferred_`. That is wrong when `unit_number` carries a real apartment number (e.g. `"618"`) — the row IS unit-level, the `unit_id` is just an upstream gap. Change the rule to: a row has natural identity if `unit_id` is natural OR `unit_number` is present, normalisable, and does not equal any `floor_plan_id` observed elsewhere in the same property's row set. Pass the property's known plan-code set into `classify()` (or a thin wrapper) — that's the only data dependency this rule needs.

6. **Add a post-partition cross-page unit-number dedup pass.** After D16's partition lands and B4 runs, deduplicate `result.units` by `(property_id, normalize_unit_number(unit_number))`. Multi-page floor-plan-page crawls (the capability added in commit `4ac6611`) routinely emit the same physical apartment from two pages with different `floor_plan_name` values — today this slips through `source_merger`'s Rank-1 key because each page becomes its own `ExtractedSource` and the inferred-id hash diverges across plans. Implementation:
   - New helper `ma_poc/extraction/unit_number.py::normalize_unit_number(raw: str) -> str | None`: strips `Apt`/`Unit`/`#`/`Suite`/`Ste` prefixes (case-insensitive), trims, lowercases, drops trailing `.0` on numeric strings, preserves alphanumeric suffixes (`101A` ≠ `101B`).
   - When two rows share a normalized unit_number, merge complementary fields at the field level (sqft from row A, rent from row B). Use *field-level* confidence to break conflicts — never row-level, that downgrades correctly-extracted fields from low-confidence sources.
   - Include `building` (when present on either side) in the dedup key to avoid false-collapses in townhome complexes that re-use unit numbers per building.
   - Emit `cross_page_unit_dedup_collapses` into the run report — this is the signal that proves the leak was real and the fix is working.

7. **Pre-empt the inferred-id hash collision in `infer()`.** In `ma_poc/extraction/infer.py`, when `unit_number` is present and passes the plan-code check from item 5, skip the `inferred_<hash>` assignment entirely. This stops two real apartments on different plans from colliding into the same inferred id, and stops the same apartment on two plan pages from getting two *different* inferred ids that the merger then can't reconcile.

8. **Plan-code-as-unit-number guard (inverse B4).** Drop or re-route any row in `result.units` whose `unit_number` matches a `floor_plan_id` from `result.plan_summaries` in the same property. B4 catches plan rows that duplicate units; this rule catches the inverse — a plan row misclassified as a unit because a per-unit signal field (availability_date, floor) was noisily populated upstream.

> **Ordering matters.** Run order inside the adapter must be: (a) `post_process` partition → (b) B4 plan-vs-unit dedup → (c) cross-page unit-number dedup (item 6) → (d) inverse-B4 plan-code guard (item 8). Reversing (b) and (c) lets plan rows pollute the unit-number buckets.

### Verification expectations

After the fix lands, re-run the 2026-05-16 canary manifest at `ma_poc/data/canary/local_runs/2026-05-16_d13_d14/canary_input.csv` and verify:

- **PID 65069 Olympic by Windsor**: `result.units` length goes from 29 → 15 (the 14 plan-aggregate rows move to `result.plan_summaries`).
- **PID 2982 Cortland on Pike**: should stay near 79-81 (all unit-level — D11 fix makes the canonical inventory page yield 79 real `data-unit-id` cards; minimal plan-aggregate leakage expected).
- **PID 67327 Windsong Estates**: `result.units` length should drop from 17-34 (depending on which canary) toward manual=18 if plan-summaries were inflating it.

Canary success criterion: at least 3 of the OVERCOUNT PIDs (`67327`, `65069`, `161`) move CLOSER to manual without regressing the UNDERCOUNT wins (Cortland, Brook, Izzy).

Additionally, include one property known to use the multi-page sub-floor-plan crawl path (added in commit `4ac6611`) in the canary input. Assert:
- `cross_page_unit_dedup_collapses` telemetry is non-zero on that property.
- `result.units` count moves toward manual ground truth.
- No regression on Olympic by Windsor's 15 natural-id units (the cross-page dedup must not over-collapse).

### Tests required

Add to `ma_poc/tests/pms/adapters/`:

1. `test_d16_plan_summary_partition.py` — assert that after `post_process` partition, `result.units` contains ONLY rows where `classify()` returns `"unit"`, and `result.plan_summaries` contains ONLY rows where `classify()` returns `"plan"`. No row appears in both.

2. Augment `test_2026_05_16_d_fixes.py::TestB4_PlanRowDedupAgainstUnits` to verify the dedup still drops plan-summaries that duplicate unit-level records by `floor_plan_id` — that B4 behaviour must survive D16.

3. End-to-end against a real per-property HTML fixture (e.g., the Olympic by Windsor capture if available, or a synthetic one with 5 unit-level + 5 plan-aggregate JSON-LD records) — assert the partition is correct.

4. `test_d16_cross_page_unit_number_dedup.py` — feed two `ExtractedSource` inputs each emitting `unit_number="618"` under different `floor_plan_name` values (simulating sub-floor-plan page crawl); assert exactly 1 unit in `result.units`, and that fields from both pages are merged (sqft from one, rent from the other).

5. `test_d16_unit_number_normalization.py` — assert `normalize_unit_number` collapses `"Apt 101"`, `"#101"`, `"101 "`, `"unit 101"`, `"101.0"` to the same key; assert `"101A"` and `"101B"` remain distinct; assert `None`/empty/whitespace-only input returns `None`.

6. `test_d16_classify_promotes_unit_number_over_inferred_uid.py` — row with `unit_id="inferred_abc123"` and `unit_number="618"` (and no clash with any property `floor_plan_id`) → `classify()` returns `"unit"`. Same row with `unit_number="2B4"` where `"2B4"` exists as a `floor_plan_id` in the property's plan set → `classify()` returns `"plan"`.

7. `test_d16_plan_code_as_unit_number_guard.py` — feed a "unit" row with `unit_number="2B4"` where `result.plan_summaries` contains a row with `floor_plan_id="2B4"`; assert the row is removed from `result.units` (or re-routed to `plan_summaries`) by the inverse-B4 guard.

8. `test_d16_infer_skips_unit_number_rows.py` — row with `unit_number="618"` and no `unit_id` → `infer()` does NOT assign an `inferred_<hash>` value (leaves `_inferred_id` unset or False); row with no `unit_number` AND no `unit_id` → `infer()` still assigns the inferred id (no regression on existing fallback path).

9. `test_d16_ordering_invariant.py` — assert the adapter runs partition → B4 → cross-page unit-number dedup → inverse-B4 guard, in that order. A unit-shaped plan row that would be dropped by B4 must not enter the cross-page dedup buckets. Verify via a fixture that fails when steps are reordered.

10. `test_d16_townhome_building_disambiguation.py` — two rows with `unit_number="1"` but different `building` values (`"A"` vs `"B"`) must NOT be collapsed by the cross-page dedup pass.

### Files likely to touch

- `ma_poc/pms/adapters/generic.py:876-906` (primary fix site + ordering for items 6 and 8)
- `ma_poc/pms/adapters/entrata.py`, `rentcafe.py`, `appfolio.py`, `amli.py`, `avalonbay.py`, `funnel.py`, `sightmap.py`, `wix.py`, `squarespace.py`, `onesite.py` — all call `post_process(...).admitted` and need the same change
- `ma_poc/extraction/classify.py` — extend `_has_natural_unit_identity` for item 5 (unit_number promotion); thread the property's plan-code set through `classify()` or a thin wrapper
- `ma_poc/extraction/infer.py` — item 7: skip inferred-id assignment when `unit_number` is present and not a plan code
- `ma_poc/extraction/unit_number.py` — **new module** for `normalize_unit_number(raw)` (item 6)
- `ma_poc/extraction/post_process.py` — wire items 5 and 6 into the partition pipeline; expose the cross-page dedup pass as a discrete step that adapters call after B4
- `ma_poc/reporting/run_report.py` — needs new `plan_summaries_count` field (Option A) and new `cross_page_unit_dedup_collapses` counter
- `ma_poc/observability/slo_watcher.py` — verify it reads the right field; consider a new SLO if cross-page collapses are persistently zero (signals the new path is dead)
- `ma_poc/scripts/diagnostics/local_canary.py` — surface `plan_summaries_count` AND `cross_page_unit_dedup_collapses` in the canary report
- `ma_poc/tests/pms/adapters/test_d16_plan_summary_partition.py` — new file
- `ma_poc/tests/extraction/test_d16_cross_page_unit_number_dedup.py` — new file
- `ma_poc/tests/extraction/test_d16_unit_number_normalization.py` — new file
- `ma_poc/tests/extraction/test_d16_classify_promotes_unit_number_over_inferred_uid.py` — new file
- `ma_poc/tests/extraction/test_d16_plan_code_as_unit_number_guard.py` — new file
- `ma_poc/tests/extraction/test_d16_infer_skips_unit_number_rows.py` — new file
- `ma_poc/tests/pms/adapters/test_d16_ordering_invariant.py` — new file
- `ma_poc/tests/extraction/test_d16_townhome_building_disambiguation.py` — new file
- Frontend / DB layer (if applicable — search `grep -r ".admitted\|result.units\|plan_summaries" ma_poc/frontend/ ma_poc/data_provider/`)

### Don'ts

- Don't remove plan_summaries from the output; downstream consumers may want them.
- Don't relax the plan-code disambiguation in item 5/8 — without the `floor_plan_id` cross-check, `unit_number="2B4"`-shaped rows will start admitting as units.
- Don't merge fields at the row level in the cross-page dedup pass — that downgrades correct values. Always merge at the field level using field-level confidence.
- Don't reorder the adapter pipeline (partition → B4 → cross-page dedup → inverse-B4); the ordering invariant is load-bearing.
- Don't extend the cross-page dedup to fuzzy keys (beds/baths/sqft) — that's `source_merger`'s Rank-3 job and it's already callback-gated. Cross-page dedup is exact-match on normalised `unit_number` only.

### Out of scope

- Building the hy.ly Marketing Cloud adapter (D15 / Sound at Peninsula). Separate workstream.
- Investigating the R1 ambiguity over-collapse on Canyon Ridge (D12 follow-up). Separate workstream.
- Touching the URL-shape scoring (D2 / D11) or DOM compact-row extractors (D13). Those are done.

### Definition of done

- Tests pass: `pytest ma_poc/tests/pms ma_poc/tests/extraction ma_poc/tests/services/test_source_planner.py ma_poc/tests/integration -q` exits 0 except the pre-existing `test_h5_visited_urls_dedupe` failure.
- All ten new D16 test files pass (partition, cross-page dedup, normalisation, classify promotion, plan-code guard, infer skip, ordering invariant, townhome disambiguation, and the augmented B4 test).
- Canary re-run on the 10-PID manifest plus one sub-floor-plan-page property shows: Olympic / Windsong / Hickory Mill moving CLOSER to manual; `cross_page_unit_dedup_collapses > 0` on the sub-floor-plan property; no UNDERCOUNT regressions on Cortland / Brook / Izzy.
- `result.units` and `result.plan_summaries` are strictly disjoint in every adapter output (no row in both partitions).
- One-paragraph summary written to `C:/tmp/rp_sx_analysis_2026_05_14/D16_CANARY.md` documenting the per-PID before/after, plus the `cross_page_unit_dedup_collapses` totals across the canary run.

---

**Hand-off context** — you'll find this file at `C:/tmp/rp_sx_analysis_2026_05_14/D16_PROMPT_FOR_NEW_SESSION.md`. Read it through, then start a new branch (`d16-plan-summary-partition`), implement, test, canary, and report back.
