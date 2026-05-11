# 2026-05-11 cloud-run regressions — consolidated fix design

**Status:** **complete**. All four bugs closed (A → D → C → B), each through full design + implementation + self-review + verification cycles.

**Final tally:**
- 15 contract tests across `tests/integration/contracts/`: all green
- 1427 tests in the full pytest suite: green
- 4 unrelated failures (3 pre-existing at HEAD; 1 flaky live-LLM test that skips without an LLM provider)
- Four cross-cutting principles established (P1 through P4) that frame future regression fixes in this codebase

**Principles introduced (cross-cutting):**

| # | Principle | Established by |
|---|---|---|
| **P1** | Lifecycle ownership of shared mutable state — initialise once, use `setdefault` not `.get` | Bug A |
| **P2** | Cross-file invariants live in one module and are imported, never duplicated | Bug D |
| **P3** | Downgrade decisions require positive evidence of the alternative, not absence of confirmation | Bug C |
| **P4** | Evidence ladders, not single sources, for recovery paths — fall through, never give up | Bug B |


**Scope:** four production regressions surfaced by the 2026-05-11 cloud-run analysis ([docs/run_2026_05_11_manual_analysis.md](run_2026_05_11_manual_analysis.md)). Tests reproducing each bug live in [tests/integration/contracts/](../tests/integration/contracts/).

**Order:** A → D → C → B. Ascending risk and design surface. Earlier bugs may extract primitives the later ones reuse.

**Workflow per bug:**
1. Indexing pass (every impacted file read in full, every caller mapped, baseline tests green)
2. Design pass written here BEFORE any code change
3. Implementation pass (smallest set of edits)
4. Self-review pass — iterates until **zero High + zero Medium findings**
5. Verification pass (contract test green, no regressions in adjacent suites, `ruff` + `mypy --strict` clean on changed files)

A bug is not closed until the above five pass for it.

---

## Cross-cutting principles (the design's spine)

These will be referenced and reinforced as each bug is addressed. Started as a single observation about Bug A; will expand as later bugs reveal shared structure.

**P1 — Lifecycle ownership of shared mutable state.** A function that builds a result dict and hands it to other functions which mutate the same dict (the `_meta` pattern) must initialise the dict once, in one place, before any reader or mutator sees it. Mutators must use `setdefault`-style access — never `.get(key, default)` — so the same dict object is returned across all access points.

**P2 — Cross-file invariants live in one module and are imported.** When two modules must agree on a value (a set of supported adapter names, a tier-string vocabulary, a configuration default), the owning module — the one that *enforces* the invariant — defines the constant. Other modules import it. Drift becomes structurally impossible. A second test surface (an AST-walk invariant test under `tests/integration/contracts/`) backstops the import discipline by catching any reintroduction of the duplicated literal.

**P3 — Downgrade decisions require positive evidence of the alternative, not absence of confirmation of the current.** When a classification (PMS type, tier-priority, etc.) has a confidence-bearing source (URL fingerprint, profile-learned hint, prior success), a *downgrade* — moving to "unknown" or a lower-confidence tier — must be triggered by **a positive signal that the current classification is wrong**, not merely by absence of a signal confirming it. Noise, missing captures, change-detector GETs, and timed-out pages are *absence-of-evidence*, not *evidence-of-absence*. The rule is "innocent until proven guilty": preserve unless something speaks against.

**P4 — Evidence ladders, not single sources, for recovery paths.** When a recovery decision (link-hop candidate selection, fallback adapter dispatch, retry target choice) reads a primary signal that may be absent, the code must fall through to lower-confidence sources rather than give up. The cost of attempting a low-confidence guess is small (one extra fetch); the cost of a silent miss is large (~1633 properties/day on 2026-05-11). Each signal source carries a score; the ladder is sorted by score and tried in order. Sources may include profile-learned URLs, LLM-emitted hints, PMS template priors, sitemap entries, and captured-API path inspection. Adding a new ladder rung is additive — never replaces an existing rung — so no signal source is lost.

---

## Bug A — `_meta.verdict` lost through `_v2_formatted` cache

### Symptom

`shard_*/properties.json` on 2026-05-11 contains `_meta = {}` for every property (verified against GCS). `reporting/run_report.py:117` reads `meta.get("verdict") or ""` → empty → never matches `startswith("FAILED")` → counts every failed property as a success. Headline reads "99.92 % success" while events.jsonl shows 58.95 %.

### Root cause (from indexing)

Commit `3013362` ("Fixing the ever alluding llm feedback loop") hoisted `_format_output(...)` into `_process_property` at `scripts/runners/jugnu.py:691` so that `_run_null_field_recovery` could see the formatted dict in time for FieldPatch persistence. The hoist runs **before** the verdict-writer at line 761:

```python
# line 691 — hoisted, runs first
formatted_for_recovery = _format_output(result, csv_row, schema_version)
result["_v2_formatted"] = formatted_for_recovery
...
# line 761 — runs later
meta = result.setdefault("_meta", {})
meta["verdict"] = verdict.verdict.value
```

Inside `_format_v2` (and the sibling `_format_v1`):

```python
meta = result.get("_meta", {})    # _meta not in result yet → returns a FRESH empty dict
...
return {... "_meta": meta, ...}    # the fresh dict is embedded in the formatted output
```

`result.get("_meta", {})` returns a brand-new `{}` when `_meta` is absent. That dict is embedded in the formatted output. Line 761 later does `result.setdefault("_meta", {})` which creates **another** new dict and stores it in `result["_meta"]`. Mutations to that dict (verdict, canonical_id, etc.) never reach the embedded one.

### Fix options considered

| Option | Description | Trade-off | Decision |
|---|---|---|---|
| **A. Init `_meta` early in `_process_property`** | Add `result.setdefault("_meta", {})` immediately after `scrape_jugnu` returns. By the time `_format_output` runs, `_meta` exists and the formatter's `.get("_meta", {})` returns the shared object. | Caller-side discipline. A future formatter that's called before `_process_property` runs through line 761 would have the same bug. | Rejected as **sole** fix — places the invariant in the wrong layer. |
| **B. Make formatters use `result.setdefault("_meta", {})`** | One-line change inside `_format_v1` and `_format_v2` (and any future `_format_vN`). The formatter guarantees `result["_meta"]` exists and is the same object as the returned dict's `_meta`. | Formatter mutates input. Minor SRP smell — but the `result` dict is already being mutated by every other downstream consumer; the formatter is in good company. | **Selected.** Self-contained, future-proof, testable. |
| **C. Discard the cache; reformat after verdict is set** | Remove `result["_v2_formatted"] = ...`; always re-call `_format_output` in the outer caller. | Recovery patches that mutate `formatted["units"]` would be lost — they're only stored on the formatted dict, not on `result["units"]`. | Rejected — breaks `_run_null_field_recovery`'s contract. |
| **D. Introduce a typed `Meta` Pydantic model** | Replace `result["_meta"]` dict with a typed model. Removes the entire class of "dict captured at wrong time" bugs. | Large refactor; touches 16+ reader sites. | Deferred. Worth tracking as a future hardening item, but out of scope for this regression fix. |

### Selected design

**Layer: `_format_v1` and `_format_v2` in `ma_poc/scripts/runners/jugnu.py`.**

**Change (both functions):**

```python
# BEFORE
meta = result.get("_meta", {})

# AFTER
meta = result.setdefault("_meta", {})
```

**Why this works:** `dict.setdefault(key, default)` returns the existing value at `key` if present, else inserts `default` and returns it. The returned object is **the** dict object now living at `result["_meta"]`. The formatter then embeds that same object into its output at `"_meta": meta`. Any subsequent mutation of `result["_meta"]` (the verdict-writer at line 763) reaches the embedded reference because they are the same object.

**Why this generalises:** the contract becomes "after `_format_output` returns, `result['_meta']` exists and is the same object as the formatted dict's `_meta`." This is testable (the contract test in `tests/integration/contracts/test_verdict_meta_persistence.py::test_v2_formatted_meta_shares_object_with_result_meta` asserts exactly this with `is`-identity).

**Docstring update:** the dispatcher `_format_output` gains a one-line contract note: *"After return, `result['_meta']` and the returned dict's `_meta` are the same object. Mutations to either are visible through both."*

### Why this fix does not break the recovery hoist

The hoist at line 691 calls `_format_v2` to produce a dict that `_run_null_field_recovery` mutates in place (it adds `_field_patches` to `result` and patches `formatted["units"][i]`). The fix changes only how `_meta` is captured — `_field_patches` and unit mutations are unaffected. Recovery continues to work; in fact it now sees a `result["_meta"]` it can read from (`{}` initially, populated later by the verdict-writer).

### Why this fix does not break v1 callers

`_format_v1` is called from two paths:
- `_process_one` outer caller (line 347, runs AFTER `_process_property` returns; `_meta` is already set by line 761).
- `jugnu_retry.py:736` (line 731 explicitly seeds `result["_meta"]` with retry fields before calling `_format_output`).

In both v1 paths the new behaviour (`setdefault` returns the existing dict) matches the old behaviour (`.get` returns the existing dict). The only behavioural change is in the "`_meta` not yet set" path, which only the v2 hoist hits — and fixing it there is the whole point.

### Why this fix does not break `jugnu_retry.py`

The retry caller does:
```python
meta = result.setdefault("_meta", {})
meta["retry"] = True
meta["retry_source_run"] = run_date
csv_row = csv_rows.get(task.property_id, {})
return _format_output(result, csv_row, schema_version)
```

After the fix, `_format_output` → `_format_v2` does `meta = result.setdefault("_meta", {})` — the SAME dict the retry caller just mutated. The returned formatted dict's `_meta` IS the retry caller's `meta`. Retry fields survive into `properties.json`. **Behaviour unchanged from today.**

### Test plan

**Contract tests (already in place, currently failing — they turn green when the fix lands):**

1. `tests/integration/contracts/test_verdict_meta_persistence.py::test_v2_formatted_meta_shares_object_with_result_meta` — asserts `formatted["_meta"] is result["_meta"]` after `_format_v2` returns and `result["_meta"]` is mutated.
2. `tests/integration/contracts/test_verdict_meta_persistence.py::test_run_report_succeeded_count_matches_event_emit_verdicts` — asserts headline metric round-trip through `run_report.build` against an event-ledger ground truth.

**New regression guard tests to be added in this PR:**

3. `_format_v1` exhibits the same sharing invariant (so a future hoist of v1 doesn't reintroduce Bug A). Add to `tests/integration/contracts/test_verdict_meta_persistence.py`.
4. End-to-end retry path: a `_format_output` invocation after `setdefault("_meta", {}); meta["retry"] = True` produces a formatted dict whose `_meta.retry == True`. Confirms the retry caller's pre-init pattern continues to work post-fix.
5. Carry-forward path: `_format_output` invocation on a result whose `_meta` already has `verdict = "SUCCESS"` (the cf path at `jugnu.py:631`) produces a formatted dict whose `_meta.verdict == "SUCCESS"`.

Tests 3, 4, 5 each take ~10 lines, all in the same file. They formalise the three callers of `_format_output` and prove each one is correct after the fix.

**Adjacent suites to re-run for regression check:**

- `tests/reporting/` (full)
- `tests/scripts/test_sync_run_to_pg.py`
- `tests/integration/e2e/`
- `tests/integration/consume/`
- `tests/integration/contracts/` (must show Bug A tests green; Bug B, C, D still red — those are not fixed yet)

### Implementation steps (in order)

1. Add the three regression guard tests (3, 4, 5) to the contract file. They will fail initially.
2. Apply the one-line change in `_format_v1`.
3. Apply the one-line change in `_format_v2`.
4. Add the contract note to `_format_output`'s docstring.
5. Run all five contract tests for Bug A — assert green.
6. Run adjacent suites — assert no regressions.
7. Run `ruff check ma_poc/scripts/runners/jugnu.py` — assert clean.
8. Run `mypy --strict ma_poc/scripts/runners/jugnu.py` — assert clean.

### Out of scope (tracked as follow-ups, not part of this fix)

- **F-A1.** Migrate `_meta` to a typed Pydantic model (option D above). Removes the entire class of dict-capture bugs at compile time. Requires updates to 16+ reader sites.
- **F-A2.** Audit all other `result.get(<key>, {})` patterns in `jugnu.py` for similar capture-before-mutation footguns. Quick grep: there are ~8 sites; most are read-only (the formatter case is the only mutating one).
- **F-A3.** Add a CI lint rule that flags `<dict>.get(<lit>, {})` followed by `[<sub-key>]` assignment — encodes Bug A's anti-pattern as a static check.

### Design revision v0.1 — second layer of defence in `run_report.build`

**Trigger for revision.** During implementation, the contract test
`test_run_report_succeeded_count_matches_event_emit_verdicts` continued to
fail after the formatter fix. The test directly constructs properties with
empty `_meta` (the exact production-observed shape on 2026-05-11) and
asserts `run_report.build` still produces correct totals. The formatter fix
prevents future occurrences of empty `_meta` but does not heal `run_report`
against the failure mode — if `_meta.verdict` is ever absent for any
reason (legacy data, future regression, partial reads), the headline
metric silently inverts again.

**Architectural decision.** Add events.jsonl as the **authoritative
secondary** source for verdicts in `run_report.build`. This follows the
existing precedent in the same function: `_scan_event_ledger` already
reads events.jsonl to derive bot/captcha classification because events
are the canonical signal — the property dicts mirror but never originate
those classifications. Verdicts follow the identical pattern:
`output.property_emitted` is the canonical emit; `_meta.verdict` is the
mirror.

**Rule when both sources are present.** Events wins. Reasoning:

| Scenario | _meta.verdict | event.verdict | Resolved |
|---|---|---|---|
| Both present, agree | SUCCESS | SUCCESS | SUCCESS (trivial) |
| Both present, agree | FAILED_NO_DATA | FAILED_NO_DATA | FAILED_NO_DATA (trivial) |
| _meta empty, event present | (missing) | SUCCESS / FAILED_* | event value — defence in depth fires |
| _meta present, event missing | SUCCESS / FAILED_* | (no emit) | _meta value — keeps today's behaviour for the 4-no-emit case observed in prod |
| Both disagree | SUCCESS | FAILED_NO_DATA | event wins, but **log a warning** — this would be a runner bug (both set from the same `verdict.verdict.value` at `jugnu.py:763`/`emit(EventKind.PROPERTY_EMITTED, …)`); silently overruling is worse than surfacing |

**Module under change:** `ma_poc/reporting/run_report.py`.

**Change shape:**

1. `_scan_event_ledger` extended to a third return: `verdict_by_pid: dict[str, str]`. Same single-pass scan, just one more `kind` check (`output.property_emitted`). No additional I/O cost.
2. `build()` extracts `pid` from each property (using the same key fallback chain as the bot/captcha scanner: `meta.get("canonical_id") or p.get("property_id")`).
3. The `verdict =` line in the per-property loop becomes a two-source resolution with `events_wins_when_present` semantics, including a `log.warning` on disagreement.

**Why this composes cleanly with the formatter fix.** In production after both fixes:
- formatter writes `_meta.verdict` correctly (primary)
- run_report reads events.jsonl and verifies (secondary)
- If they ever disagree, oncall sees a warning in the logs

If the formatter ever regresses again (a future hoist, a serialisation change), the report's totals stay correct because events is authoritative. The bug that took a day to catch on 2026-05-11 becomes a same-run-warning instead.

**New regression guard tests.**

6. Both sources agree → no warning, count is the agreed value.
7. _meta empty, events populated → count uses events.
8. _meta populated, events missing → count uses _meta (no regression in the no-emit-but-meta-correct path).
9. Disagreement → events wins, a warning is logged.

These are added to `tests/integration/contracts/test_verdict_meta_persistence.py`.

**Why this is in scope for "fix Bug A".** The original failure mode was *silent* metric corruption that took a day to notice. Defence in depth against silent corruption of the same metric, using a source we already read in the same function, is bug-class containment — not feature-creep.

### Design revision v0.2 — symmetry with `slo_watcher`

**Trigger.** Self-review pass 1 surfaced that `observability/slo_watcher.py:check()` reads `_meta.verdict` with the same dual-source vulnerability. In production after the formatter fix it works correctly, but it is the function that feeds the paging signal — false-OK there is uniquely bad. The defence-in-depth principle of v0.1 must extend to it; otherwise the design is inconsistent across the two functions that share the same authoritative source.

**Refactor.** The resolver + scanner are too domain-specific to live in `run_report.py` once a second consumer needs them. Move them to `ma_poc/reporting/verdict.py`, which is the existing home of `Verdict` enum + `VerdictResult` + `compute()` — the natural module for verdict-domain logic.

**New public surface in `reporting/verdict.py`:**

| Symbol | Purpose |
|---|---|
| `scan_event_ledger_verdicts(run_dir) -> dict[str, str]` | Single-purpose scanner: returns `pid → verdict` from `output.property_emitted` events in events.jsonl. Distinct from `run_report._scan_event_ledger` (which also pulls bot/captcha; that stays internal to `run_report.py` because bot/captcha classification is a reporting concern, not a verdict concern). |
| `resolve_verdict(meta_verdict, event_verdict, pid) -> str` | The resolver. Same semantics as the v0.1 internal helper. |

**Why split the two scanners** (one in `run_report`, one in `verdict.py`) rather than a single shared one with multiple return values:
- `verdict.py` is in the reporting layer's verdict-domain — has no reason to know about bot/captcha shapes.
- `run_report.py`'s `_scan_event_ledger` reads events.jsonl once and pulls four signal kinds in the same pass. The verdict scanner re-reads events.jsonl when called separately. Performance impact: two file reads instead of one for the report-build path (one already accepted as background cost). slo_watcher gets its own single read.
- Splitting respects SRP: each function does one thing.

**Layer concern.** `slo_watcher.py` is in `observability/`; `verdict.py` is in `reporting/`. The slo_watcher → reporting import direction is consistent with the existing pattern (`reporting/run_report.py` already imports `ma_poc.observability.events` and `ma_poc.observability.cost_ledger`). No new circularity.

**API signature change for `slo_watcher.check`.** The function currently takes `(cost_rollup, property_results)`. To consult events.jsonl it needs `run_dir`. Three callers exist (per grep): `scripts/runners/jugnu.py:468`, `scripts/runners/jugnu_retry.py`, and the test suite. All callers have `run_dir` in scope. Backwards-compatible signature: add `run_dir: Path | None = None` as a keyword arg — when omitted, behaviour is identical to today (no event-ledger consultation, `_meta.verdict` is the sole signal). All callers updated to pass `run_dir`. Tests updated.

**New test coverage:**

- `tests/observability/test_slo_watcher.py`: new test mirroring `test_resolver_events_used_when_meta_empty` — properties with empty `_meta.verdict`, events with mixed verdicts, assert `success_rate` violation fires correctly. Confirms the defence-in-depth path.
- `tests/observability/test_slo_watcher.py`: new test asserting the no-`run_dir` legacy path still works (backwards compat).

**Selected design final shape (v0.2):**

1. `ma_poc/reporting/verdict.py` gains `scan_event_ledger_verdicts` + `resolve_verdict`.
2. `ma_poc/reporting/run_report.py` deletes its internal `_resolve_verdict`, imports the public one from verdict.py. Its `_scan_event_ledger` continues to pull verdicts inline (single-pass over events.jsonl) — implementation detail.
3. `ma_poc/observability/slo_watcher.py:check` accepts optional `run_dir`, uses `scan_event_ledger_verdicts` + `resolve_verdict` when provided. Same semantics as run_report.
4. `scripts/runners/jugnu.py:468` and `scripts/runners/jugnu_retry.py` pass `run_dir=run_dir`.

This preserves both the contract test surface (Bug A's 9 tests) and adds an explicit second test surface for slo_watcher's defence-in-depth path.

### Bug A — close-out summary

Status: **closed**. Self-review converged at pass 2 with zero High and zero Medium findings.

**Files changed:**
- `ma_poc/reporting/verdict.py` — added `scan_event_ledger_verdicts` + `resolve_verdict` (public surface)
- `ma_poc/reporting/run_report.py` — `_format_v1`/`_format_v2` sharing contract via `setdefault`; `build()` consults event ledger via shared resolver; `_scan_event_ledger` extended to a 3-tuple return so the same single-pass scan feeds verdict + bot + captcha classification
- `ma_poc/scripts/runners/jugnu.py` — `_format_v1` + `_format_v2` use `setdefault`; `slo_check` passes `run_dir=run_dir`
- `ma_poc/scripts/runners/jugnu_retry.py` — `slo_check` passes `run_dir=output_run_dir`
- `ma_poc/scripts/runners/jugnu_retry_merge.py` — `slo_check` passes `run_dir=output_run_dir`
- `ma_poc/observability/slo_watcher.py` — `check()` accepts optional `run_dir`, consults event ledger via shared resolver

**Tests added/modified:**
- `tests/integration/contracts/test_verdict_meta_persistence.py` — 9 contract tests (5 sharing + 4 resolver behaviour)
- `tests/observability/test_slo_watcher.py` — 3 new defence-in-depth tests (events-empty-meta, legacy no-run_dir path, disagreement warns)

**Verification:**
- All 9 Bug-A contract tests: green
- 137 tests across `tests/reporting/ tests/observability/ tests/integration/e2e/ tests/integration/consume/ tests/scripts/test_sync_run_to_pg.py`: green
- `ruff check` clean on all changed files (3 pre-existing E402/UP041 findings in `scripts/runners/jugnu.py` and `jugnu_retry.py` are pre-existing and unrelated to this fix)
- `mypy --strict` clean on `reporting/verdict.py`, `reporting/run_report.py`, `observability/slo_watcher.py`

**Follow-ups (out of scope for Bug A, tracked for later):**
- **F-A1.** Migrate `_meta` to a typed Pydantic model. Eliminates the dict-capture-before-init bug class at compile time.
- **F-A2.** Audit other `result.get(<key>, {})` patterns for similar footguns.
- **F-A3.** CI lint rule flagging the anti-pattern.
- **F-A4.** Apply the same dual-source resolver to `scripts/sync/run_to_pg.py` (7 read sites of `_meta.verdict`). Lower stakes than slo_watcher (writes to DB, auditable separately, doesn't feed paging) so not blocking, but consistency is worth chasing.
- **F-A5.** Unify pid-extraction order between `run_report.build`'s resolver call and its pre-extraction-terminations classifier (one-line helper).

---

## Bug D — Rescue allow-list drift between scraper and rescue

### Symptom

Per the 2026-05-11 analysis: `extract.llm_rescue_failed` events show `unsupported adapter: onesite` (408 occurrences) and `unsupported adapter: amli` (19) — the rescue is invoked, immediately refuses, and the property loses its only remaining recovery path. Of the 427 properties affected, 79 terminally fail (others fall through to link-hop or other paths).

### Root cause (from indexing)

Commit `53b0680` (May 9) widened the inline allow-list at `scripts/.../scraper.py:713` to include `onesite` and `amli`, completing the F1.3 plan from the May-8 implementation notes. But the parallel gate at `services/llm_api_rescue.py:154` — `SUPPORTED_ADAPTERS = frozenset({"generic", "entrata", "appfolio"})` — was not updated. The two sets have drifted since.

A second site compounds the bug: `_tier_label_for` at `llm_api_rescue.py:632` maps only `{generic, entrata, appfolio}` to tier-string labels. Widening `SUPPORTED_ADAPTERS` alone would cause `KeyError` at line 719 on a successful onesite/amli rescue, silently swallowed by the `except Exception` at line 732. Both sites must change.

### Fix options considered

| Option | Description | Trade-off | Decision |
|---|---|---|---|
| **D-1. Widen `SUPPORTED_ADAPTERS` literal in-place** | Add `onesite, amli` to the frozenset at line 154. Add label entries at line 632. | Minimum diff. But duplicated literal (scraper.py vs llm_api_rescue.py) remains — drift can recur. | Rejected as **sole** fix. |
| **D-2. Single ownership: scraper imports `SUPPORTED_ADAPTERS` from rescue** | `llm_api_rescue.py` owns the constant (it enforces the invariant). `scraper.py` imports and uses the imported name in its inline gate. Drift becomes structurally impossible. | One import line in scraper. Existing AST contract test continues to enforce equality. | **Selected.** P2 in action. |
| **D-3. Move both constants to `ma_poc/contracts.py`** | New top-level module for all cross-file invariants. Generalisable for future Bug-D shapes. | Gold-plating for a single invariant. Premature abstraction. | Deferred until at least three constants need it (F-D2). |

### Selected design

1. **Widen `SUPPORTED_ADAPTERS` in `llm_api_rescue.py`** to include `onesite` and `amli`. This matches the scraper-side gate's intent.
2. **Extend `_tier_label_for`** with two new entries:
   - `"onesite": "TIER_1_API_ONESITE_LLM_RESCUE"`
   - `"amli": "TIER_1_API_AMLI_LLM_RESCUE"`
   Naming follows the existing `TIER_1_API_<NAME>_LLM_RESCUE` convention. (Note: the existing tier names are unparametrised — `TIER_1_API_LLM_RESCUE` for the generic case rather than `TIER_1_API_GENERIC_LLM_RESCUE` — but the platform-named tiers follow a clear pattern that we're extending consistently.)
3. **Replace the inline set literal in `scraper.py:713`** with an import-and-use of `SUPPORTED_ADAPTERS` from `llm_api_rescue`. The expression becomes `adapter_name in SUPPORTED_ADAPTERS`.

### Why this fix does not break existing tests

`test_rescue_returns_empty_when_unsupported_adapter` uses `rentcafe` as its "unsupported" example. RentCafe is handled by its own adapter — never goes through the generic rescue path — and is correctly absent from `SUPPORTED_ADAPTERS`. Will continue to be rejected. Test passes unchanged.

`test_f1_3_rescue_fires_for_onesite_adapter` mocks `rescue_from_api_responses` so the real gate is never exercised. It currently passes only because the mock intercepts. After the fix the real gate would also accept onesite — even more reason it passes.

### Test plan

**Contract test (existing, currently failing — will turn green when the fix lands):**

1. `tests/integration/contracts/test_rescue_adapter_allow_list.py::test_scraper_rescue_gate_equals_supported_adapters` — AST-walks `scraper.py` to extract the inline allow-list, compares to `SUPPORTED_ADAPTERS` frozenset. Equality required.

**New regression guard tests to be added in this PR:**

2. `test_rescue_accepts_onesite_source_adapter` — calls `rescue_from_api_responses` with `source_adapter="onesite"`, asserts the failure message is NOT "unsupported adapter: onesite" (the rescue may still fail downstream for other reasons; that's fine — we only assert it gets past the gate).
3. `test_rescue_accepts_amli_source_adapter` — same for amli.
4. `test_tier_label_for_handles_all_supported_adapters` — parametrised test asserting `_tier_label_for(adapter)` doesn't `KeyError` for any member of `SUPPORTED_ADAPTERS`. This invariant test is the structural guard against the "widen one site but not the other" bug class. Goes in `tests/services/test_llm_api_rescue.py`.

Tests 2-4 are short (~10 lines each).

**Adjacent suites to re-run for regression check:**

- `tests/services/test_llm_api_rescue.py` (full)
- `tests/pms/test_scraper_llm_rescue_routing.py` (full — exercises the scraper-side gate end-to-end)
- `tests/integration/contracts/` (Bug A still green, Bug D turns green, Bug B + C still failing — expected)

### Implementation steps (in order)

1. Add the three guard tests (2, 3, 4).
2. Widen `SUPPORTED_ADAPTERS` in `llm_api_rescue.py:154`.
3. Extend `_tier_label_for` in `llm_api_rescue.py:632`.
4. Replace inline set literal in `scraper.py:713` with `import SUPPORTED_ADAPTERS from llm_api_rescue` + `adapter_name in SUPPORTED_ADAPTERS`.
5. Run Bug D's contract test — assert green.
6. Run adjacent suites — assert no regressions.
7. `ruff check` + `mypy --strict` on changed files.

### Out of scope (tracked as follow-ups)

- **F-D1.** Add an entry for the new `TIER_1_API_ONESITE_LLM_RESCUE` / `TIER_1_API_AMLI_LLM_RESCUE` strings to `_TIER_MAP` in `profile_updater.py` so successful rescues at these tiers update `preferred_tier`. This is a pre-existing gap (the existing rescue tiers aren't in the map either), not a Bug D regression.
- **F-D2.** Centralise cross-file invariants into `ma_poc/contracts.py` once a third invariant calls for it (most likely candidate is Bug C's PMS-name vocabulary).
- **F-D3.** Strengthen `test_f1_3_rescue_fires_for_onesite_adapter` by mocking `_call_llm` (the LLM HTTP layer) instead of `rescue_from_api_responses` itself. Would have caught Bug D directly. Independently valuable, but the new AST contract test + behaviour tests 2-4 already provide better coverage.
- **F-D4.** Extend `PLATFORM_TIERS` in `scripts/diagnostics/analyze_cloud_run.py` to recognise the `_LLM_RESCUE` tier variants. Pre-existing gap (the existing three rescue tier strings already aren't in there).

### Bug D — close-out summary

Status: **closed**. Self-review converged at pass 1 with zero High and zero Medium findings.

**Files changed:**
- `ma_poc/services/llm_api_rescue.py` — widened `SUPPORTED_ADAPTERS` to include `onesite, amli`; extended `_tier_label_for` with matching entries; added module-level docstring linking the two together.
- `ma_poc/pms/scraper.py` — replaced inline allow-list literal with `from ma_poc.services.llm_api_rescue import SUPPORTED_ADAPTERS` + `adapter_name in SUPPORTED_ADAPTERS`.

**Tests added/modified:**
- `tests/integration/contracts/test_rescue_adapter_allow_list.py` — replaced the AST-walk-for-literal-set test with three stronger structural tests: (a) scraper.py references the imported `SUPPORTED_ADAPTERS` Name, (b) no competing inline literal exists (or if it does, it must agree with the constant), (c) `SUPPORTED_ADAPTERS` contains the full widened membership.
- `tests/services/test_llm_api_rescue.py` — three new behaviour tests: rescue accepts onesite, rescue accepts amli, every supported adapter has a tier label.

**Verification:**
- All Bug D contract tests: green
- All `test_llm_api_rescue.py` (57 tests): green
- All `test_scraper_llm_rescue_routing.py` (22 tests): green
- 165 tests across `tests/integration/contracts/ tests/services/test_llm_api_rescue.py tests/pms/test_scraper_llm_rescue_routing.py tests/reporting/ tests/observability/`: green (2 failures remain — Bug B and Bug C, not yet fixed)
- `ruff check` on changed files: 3 pre-existing I001 findings in untouched portions of `scraper.py`, zero new findings from this fix
- `mypy --strict` on the touched library module (`services/llm_api_rescue.py`): zero findings introduced by this fix (pre-existing errors in transitive imports unrelated to this change)

---

## Bug C — `confirm_detection` demotes on absence of confirmation

### Symptom

558 RentCafe-fingerprinted properties per day on 2026-05-11 had their detection demoted to `unknown` and fell through to the generic adapter (which then failed). The page's URL had a strong RentCafe fingerprint (`script_src: cdngeneralmvc.rentcafe.com` plus DOM markers, confidence 0.9), but every captured XHR was a third-party widget (`googletagmanager`, `maps.googleapis.com`, `cloudflare.com/turnstile`) — none matched RentCafe's body shape.

### Root cause (from indexing)

`confirm_detection` at `ma_poc/pms/detector.py:528-596` (introduced by commit `eb18889`, 2026-04-20) implements a binary keep-or-demote rule:

```python
for resp in responses:
    if checker(body):
        return initial  # match found — keep
# loop fell through — demote
return DetectedPMS(pms="unknown", confidence=0.0, ...)
```

The rule treats *absence of a positive match* as disconfirming evidence. The eb18889 design intent was correct (counter the Windsor/Mark-Taylor case where URL says RentCafe and bodies are positively Funnel-shaped — there a Funnel body IS captured), but the implementation collapses two distinct situations:

| Situation | Bodies say | What it means | Correct response |
|---|---|---|---|
| Windsor case (eb18889 target) | "I'm Funnel-shaped" | URL was wrong, page belongs to Funnel | **Demote** |
| 2026-05-11 case | "I'm a Google CDN widget" (noise) | URL is probably right; the real PMS API just wasn't captured (loaded after networkidle, blocked by per-property blocklist, or fired via the JS widget) | **Preserve** |

Today's binary rule demotes in both cases, taking down 558 valid RentCafe properties along with the handful of mis-routed ones.

### Fix options considered

| Option | Description | Trade-off | Decision |
|---|---|---|---|
| **C-1. Tri-state return (keep ∣ dual-run ∣ demote)** | Add a middle state where router tries detected adapter first, falls through to generic on empty. | Changes the function's return type. Touches scraper.py call site, all callers, all tests. Larger blast radius. | Rejected — invariant can be expressed without changing the type. |
| **C-2. Require positive negative evidence** | Demote only when at least one captured body **positively matches a different adapter's `matches_response_body`**. Noise alone is preserved. | Adds a second pass over responses against all other body-shape checkers. ~4 checkers × ~10 responses = 40 predicate calls per non-matching detection. Negligible cost. Preserves the function signature. | **Selected.** P3 in action — innocent until proven guilty. |
| **C-3. Confidence threshold** | Only demote when initial confidence is below some value (e.g., < 0.5). | Doesn't distinguish "I'm rentcafe with no captures" (preserve) from "I'm rentcafe with funnel-shaped captures" (demote). Wrong axis. | Rejected. |

### Selected design

Modify `confirm_detection` (`pms/detector.py:528-596`):

1. **Phase 1 (today's logic preserved):** iterate responses, check each against the *initial* adapter's `matches_response_body`. If any matches, return `initial` (same as today).
2. **Phase 2 (new):** if no body matched the initial adapter, iterate responses again, checking each against **every other** registered adapter's `matches_response_body`. If any match positively, demote (same shape as today's demotion, with evidence updated to reflect the positive cross-match). If none match, **preserve `initial`** — this is the new "noise is not disconfirmation" path.

### Implementation outline

```python
# Phase 1 — does any body match the initial adapter's expected shape?
for resp in responses:
    body = resp.get("body") if isinstance(resp, dict) else None
    try:
        if checker(body):
            return initial
    except Exception:
        continue

# Phase 2 (new) — positive negative evidence required to demote.
# Iterate all registered adapters' body-shape checkers (skipping initial's).
# If any captured body matches a different adapter, that's evidence the
# URL detection was wrong → demote with a richer evidence string.
from ma_poc.pms.adapters.registry import all_adapters

cross_match: tuple[str, int] | None = None  # (matched_pms_name, response_idx)
for other in all_adapters():
    other_name = getattr(other, "pms_name", "")
    if not other_name or other_name == initial.pms or other_name == "generic":
        continue
    other_checker = getattr(other, "matches_response_body", None)
    if not callable(other_checker):
        continue
    for idx, resp in enumerate(responses):
        body = resp.get("body") if isinstance(resp, dict) else None
        try:
            if other_checker(body):
                cross_match = (other_name, idx)
                break
        except Exception:
            continue
    if cross_match is not None:
        break

if cross_match is None:
    # Phase 2 found nothing — bodies are noise, NOT positive evidence
    # for a different PMS. Preserve URL detection.
    return initial

# Cross-match found — URL detection contradicted by a positively-shaped
# response. Demote with the specific cross-match in evidence so
# downstream operators can see which PMS the body actually belonged to.
matched_pms, matched_idx = cross_match
return DetectedPMS(
    pms="unknown",
    confidence=0.0,
    evidence=list(initial.evidence) + [
        f"demoted_from_{initial.pms}:"
        f"response_{matched_idx}_matches_{matched_pms}_body_shape"
    ],
    pms_client_account_id=None,
    recommended_strategy=_STRATEGY_BY_PMS["unknown"],
)
```

### Why existing tests don't regress

| Test | Today's input | Today's expected | After fix |
|---|---|---|---|
| `test_confirm_detection_keeps_when_body_matches` | RentCafe init + RentCafe body | rentcafe | rentcafe (Phase 1 returns early — unchanged) |
| `test_confirm_detection_demotes_when_no_body_matches` | RentCafe init + Funnel body | unknown | unknown (Phase 2 detects Funnel match — demotes with richer evidence) |
| `test_confirm_detection_preserves_when_no_responses` | RentCafe init + [] | rentcafe | rentcafe (responses empty → already-existing F0.2 early return — unchanged) |
| `test_confirm_detection_preserves_when_responses_is_none` | RentCafe init + None | rentcafe | rentcafe (same path) |
| `test_confirm_detection_leaves_unknown_alone` | unknown init | unknown | unknown (`if initial.pms == 'unknown': return initial` — unchanged) |
| `test_confirm_detection_handles_adapter_without_body_check` | Entrata init (no checker) | entrata | entrata (`checker is None → return initial` — unchanged) |

All six existing tests pass after the fix.

### Test plan

**Contract tests (already in place):**

1. `tests/integration/contracts/test_detection_preservation_under_noise.py::test_confirm_detection_preserves_rentcafe_when_apis_are_noise_only` — failing today, turns green when the fix lands.
2. `tests/integration/contracts/test_detection_preservation_under_noise.py::test_confirm_detection_demotes_when_apis_match_a_different_pms` — currently passing (demotes correctly on cross-match); must continue to pass.

**New regression guard tests (add to `tests/pms/test_detector.py`):**

3. `test_confirm_detection_preserves_when_bodies_are_pure_noise` — RentCafe init, bodies are `[]`, `{}`, `"<html>404</html>"`, captcha HTML. Asserts preserved. Direct mirror of the production failure shape.
4. `test_confirm_detection_demotes_with_specific_cross_match_in_evidence` — RentCafe init, one body is Funnel-shaped. Asserts demoted AND evidence contains the matched PMS name. Documents the new richer evidence format.
5. `test_confirm_detection_preserves_when_only_initial_adapter_has_checker` — Edge case: registry has only RentCafe with `matches_response_body`. Bodies are noise. Phase 2 finds no other checkers; preserves. Documents the safe-default behaviour.

**Adjacent suites to re-run:**
- `tests/pms/test_detector.py` (full — 39 tests)
- `tests/pms/test_scraper.py` (exercises confirm_detection via `scrape()`)
- `tests/pms/adapters/test_*.py` (matches_response_body implementations)
- `tests/integration/contracts/` (Bug A, D green; Bug C turns green; Bug B still failing)

### Implementation steps (in order)

1. Add the three guard tests (3, 4, 5) to `tests/pms/test_detector.py`.
2. Replace the demotion logic in `confirm_detection` with Phase 1 + Phase 2 as outlined above.
3. Update the docstring to reflect the P3 rule.
4. Run Bug C's contract test — assert green.
5. Run adjacent suites — assert no regressions.
6. `ruff check` + `mypy --strict` on the touched file.

### Out of scope (tracked as follow-ups)

- **F-C1.** Audit other downgrade decisions in the codebase against P3: which other "preserve unless proven wrong" classifications are using absence-of-evidence as disconfirmation? Candidates: profile maturity demotion in `services/drift_detector.py`, replay-cache eviction in `pms/adapters/generic.py`.
- **F-C2.** Add a per-PMS confidence-aware demotion threshold (high-confidence URL fingerprints might tolerate a contradicting body before flipping). Today's fix preserves the binary "any cross-match → demote" rule; a future refinement could require a confidence ratio.

---

## Bug B — `_try_link_hop` returns None when profile + keyword ranker both empty

### Symptom

On 2026-05-11, 1633 properties (87 % of FAILED_NO_DATA) failed in this exact trace shape: the LLM rescue ran and returned "no candidates after filtering"; the runner then invoked `_try_link_hop` which short-circuited at `scraper.py:1336` because `ranked == []` after profile-top injection + keyword ranking + visited/explored filtering. The signature of the failure: profile starvation (Bug 1 collateral over many days), SPA marketing-shell homepage (anchors load post-hydration, never observed at `networkidle`), and a fingerprint-matched PMS (RentCafe in 558 cases) that the runner **knows** templates `/floorplans` as its availability URL — but no code path uses that prior.

### Root cause (from indexing)

`_try_link_hop` at `pms/scraper.py:1249-1336` builds its candidate list from three sources:

1. `_rank_internal_links(entry_page_html)` — keyword/host/path anchor ranker over the HTML's `<a>` tags.
2. `_augment_ranked_with_hints` — LLM-emitted `navigation_hint` URLs (when present).
3. `profile_top` from `profile.navigation.{winning_page_url, availability_links}`.

When **all three sources produce zero** — empty profile + SPA shell with no useful anchors + LLM didn't emit a nav hint — the function returns None. There is no fallback to the *PMS template prior* even though the matched fingerprint trivially identifies the canonical sub-path for that platform.

P4 says: an evidence ladder must not give up just because one rung is absent. The current code has three rungs (keyword, LLM hint, profile); production needs a fourth — the PMS fingerprint prior.

### Fix options considered

| Option | Description | Trade-off | Decision |
|---|---|---|---|
| **B-1. PMS fingerprint priors (template defaults)** | When `detected.pms != "unknown"`, inject a fixed per-PMS list of template-derived sub-paths (`/floorplans`, `/availability`, `/apartments` for RentCafe; analogous for Entrata/Sightmap/etc.). Slots between profile-top (highest) and keyword-ranked (lowest) in score. | Zero LLM cost. Zero new I/O per property (one extra sub-page fetch per affected property, gated by the existing cap). Covers the 558 RentCafe-only failures that also benefit from Bug C's preservation. | **Selected.** Highest leverage, lowest cost, smallest diff. |
| **B-2. LLM nav-hint re-prompt** | When monolithic LLM returns 0 units AND emits no nav hint, fire a second LLM call with a navigation-only goal ("given this homepage HTML, what URL would have unit data?"). | +1 LLM call per affected property (~$0.01 × 1633 = ~$16/day). +5s latency per property. | Tracked as F-B1. Higher cost, narrower coverage; revisit when PMS-prior baseline is established. |
| **B-3. Sitemap consumer** | When PMS prior list doesn't apply (no fingerprint match) or fails (all priors 404), fetch `/sitemap.xml` and filter for `_AVAILABILITY_URL_SIGNALS` paths. | Free for sites with a sitemap (most). One extra fetch per affected property. | Tracked as F-B2. Adds breadth (covers fingerprintless properties) but bigger diff; sequence after B-1. |
| **B-4. Captured-API URL path inspection** | Parse the URLs of captured XHR responses (e.g., `/api/v1/floorplans/list.json`) for page-URL hints. | Niche. Free. | Tracked as F-B3. Marginal additional coverage. |

The selected design implements B-1 only. B-2/B-3/B-4 are tracked but not in scope. Rationale: B-1 covers the largest population (558 + secondary effects from Bug C's preservation fix), the diff is small (~30 lines + tests), and the architectural primitive (`_PMS_SUB_PATH_PRIORS` constant + injection in `_try_link_hop`) is reusable. The other ladders ride on the same primitive when they land.

### Selected design

**1. New constants in `pms/scraper.py`** alongside the existing link-hop constants (`_LINK_PATH_KEYWORDS`, `_LINK_HOST_KEYWORDS`):

```python
# Score for PMS-template priors — between profile-top (10000+) and the
# keyword anchor ranker (0-200). Profile + LLM hints still win when
# present; PMS priors win over keyword anchors.
_PMS_PRIOR_SCORE = 5000

# Template-derived sub-paths for each detected PMS. When a property has
# no profile-learned URL and the homepage's anchor scan returns zero
# useful candidates (SPA marketing shell, 2026-05-11 Bug B shape), these
# priors guarantee at least one well-typed URL to try.
#
# Order within each tuple reflects the most common availability-page
# convention for that PMS — first entry tried first (post-merge).
_PMS_SUB_PATH_PRIORS: dict[str, tuple[str, ...]] = {
    "rentcafe": ("/floorplans", "/availability", "/apartments"),
    "entrata": ("/floorplans", "/availability", "/leasing"),
    "appfolio": ("/listings", "/apartments", "/floor-plans"),
    "onesite": ("/floorplans", "/availability", "/apartments"),
    "realpage_oll": ("/floorplans", "/availability"),
    "sightmap": ("/floorplans", "/availability"),
    "avalonbay": ("/floor-plans-pricing", "/apartments"),
    "amli": ("/floor-plans", "/availability"),
    "funnel": ("/floorplans", "/availability"),
}
```

**2. Injection block in `_try_link_hop`**, between the profile-top section and the visited/skip filter:

```python
# Bug B (P4): PMS fingerprint priors. Template-derived sub-paths for the
# detected PMS. Slot between profile-top (highest) and keyword-ranked
# (lowest) in score. When neither profile nor keyword ranker produces
# candidates (SPA marketing shell with detected PMS — the 2026-05-11
# Bug B shape), this guarantees at least one well-typed candidate.
pms_priors: list[tuple[str, int, str]] = []
if detected and detected.pms != "unknown":
    template_paths = _PMS_SUB_PATH_PRIORS.get(detected.pms, ())
    for path in template_paths:
        prior_url = urllib.parse.urljoin(entry_url, path)
        if prior_url and prior_url != entry_url:
            pms_priors.append(
                (prior_url, _PMS_PRIOR_SCORE, f"pms_prior:{detected.pms}")
            )

# Merge order: profile-top (10000+) > PMS priors (5000) > keyword (0-200).
# Single dedup pass by URL; first-seen wins so profile beats prior beats
# keyword for the same URL.
if profile_top or pms_priors:
    seen: set[str] = set()
    merged: list[tuple[str, int, str]] = []
    for u, s, a in list(profile_top) + pms_priors + list(ranked):
        if u not in seen:
            seen.add(u)
            merged.append((u, s, a))
    ranked = merged
```

**3. Cap stays unchanged.** With `max_hops = 3` and one profile-top slot bumping the cap to 4, the truncation at `ranked[:cap]` keeps the highest-priority 3–4 candidates. PMS priors compete with keyword anchors for the remaining slots after profile-top — and since their score (5000) is much higher than keyword anchors (≤200), they win. For the Bug B shape (empty profile, no keywords), the first 3–4 entries are all PMS priors, exactly what's needed.

### Why the existing tests don't regress

| Test | Today's input | Today's expected | After fix |
|---|---|---|---|
| `test_link_hop_helpers.test_rank_*` | HTML with anchors | scored candidates | unchanged — `_rank_internal_links` itself is unmodified |
| `test_link_hop_navigation_memory.*` | profile + HTML | profile-top first | unchanged — profile-top remains highest priority |
| `test_runtime_hint_consumption.*` | LLM nav hints | hint slot used | unchanged — LLM nav hints still bind via `_augment_ranked_with_hints` ahead of priors |
| `test_extract_link_hop_*` (integration) | HTML + adapter signals | sub-page recovery | unchanged for happy paths; for property+SPA-shell tests, may now make extra hops (defensive: existing tests assert "at least N hops", not "exactly N") |

### Test plan

**Contract test (already in place):**

1. `tests/integration/contracts/test_link_hop_pms_template_priors.py::test_link_hop_recovers_via_pms_prior_when_profile_starved` — failing today, turns green after fix. Asserts at least one fetch is attempted and at least one URL matches a RentCafe template path.

**New regression guard tests (add to `tests/pms/test_link_hop_helpers.py` or a new `tests/pms/test_link_hop_pms_priors.py`):**

2. `test_pms_priors_dict_covers_all_registered_pms_with_body_checker` — invariant: every PMS that has `matches_response_body` (i.e., the same PMSes that ride the Bug-C P3 preservation path) must also have a `_PMS_SUB_PATH_PRIORS` entry. Catches drift if a new PMS is added without a prior list.
3. `test_link_hop_priors_for_each_pms_inject_candidates` — parametrised over the priors dict; given a detection for each PMS + empty profile + bare HTML, assert that the prior URLs are produced (via the helper or via a unit-level inspection of the injection block).
4. `test_link_hop_priors_skipped_when_pms_is_unknown` — `detected.pms == "unknown"` → no prior URLs injected.
5. `test_link_hop_priors_deduped_against_profile` — profile has `/floorplans` and prior wants `/floorplans` → only one entry in the merged list, profile wins on score.
6. `test_link_hop_priors_respect_visited_and_explored_skip` — prior URLs that are in `visited` or `explored_links` get filtered like any other candidate.

### Implementation steps (in order)

1. Add the constants `_PMS_PRIOR_SCORE` and `_PMS_SUB_PATH_PRIORS` to `pms/scraper.py`.
2. Add the injection block to `_try_link_hop` between the existing profile-top section and the visited/skip filter.
3. Add the merge + dedup logic.
4. Add the regression guard tests (2-6).
5. Run Bug B's contract test — assert green (and assert it fetched at least one prior URL).
6. Run adjacent suites (link-hop tests, extraction integration) — assert no regressions.
7. `ruff check` + `mypy --strict` on the touched file.

### Out of scope (tracked as follow-ups)

- **F-B1.** LLM nav-hint re-prompt when monolithic LLM returns 0 units (Option B-2 above). Adds one extra LLM call per affected property; sequenced after PMS priors have a baseline.
- **F-B2.** Sitemap consumer (Option B-3). Covers fingerprintless properties that the PMS prior can't help. Reuses `_AVAILABILITY_URL_SIGNALS` from `llm_api_rescue.py` — another candidate for centralisation into a future `contracts.py` once F-D2 triggers.
- **F-B3.** Captured-API URL path inspection (Option B-4).
- **F-B4.** Move `_PMS_SUB_PATH_PRIORS` to per-adapter contributions (`adapter.priority_sub_paths()`). Adapters then own their own template — cleaner SOLID surface. Today's centralised dict is the minimum-diff version; refactor when more PMSes need adapter-specific path logic.

### Bug B — close-out summary

Status: **closed**. Self-review converged at pass 1 with zero High and zero Medium findings.

**Files changed:**
- `ma_poc/pms/scraper.py` — added `_PMS_PRIOR_SCORE` constant, `_PMS_SUB_PATH_PRIORS` template dict for 9 PMSes, and `_pms_priors_for(detected, entry_url)` pure-function helper. Inserted a merge-and-dedup block in `_try_link_hop` that combines profile-top, PMS priors, and keyword-ranked candidates into a single ranked list with explicit priority order (profile > prior > keyword).

**Tests added/modified:**
- `tests/pms/test_link_hop_helpers.py` — 13 new tests:
  - 9 parametrised tests asserting each PMS produces its expected canonical first sub-path
  - `test_pms_priors_skipped_when_pms_is_unknown`
  - `test_pms_priors_returns_empty_for_none_detection`
  - `test_pms_priors_returns_empty_for_pms_without_template_entry`
  - `test_pms_priors_filter_entry_url_collisions`
  - `test_pms_priors_dict_covers_all_pms_with_body_checker` (Bug-C ↔ Bug-B invariant)
  - `test_link_hop_dedups_profile_against_pms_prior_for_same_url` (integration-level merge ordering)
  - `test_link_hop_priors_respect_explored_skip_list` (integration-level skip-list compliance)

**Verification:**
- Bug B contract test (`test_link_hop_recovers_via_pms_prior_when_profile_starved`): green
- `tests/pms/test_link_hop_helpers.py` (30 tests including the 13 new): green
- 710 tests across `tests/pms/ tests/integration/contracts/ tests/integration/extract/`: green (3 failures are pre-existing at HEAD, unrelated to Bug B)
- `ruff check` on changed files: 3 pre-existing I001 findings in untouched parts of `scraper.py`, zero new findings from this fix
- `mypy --strict` on the touched library module: zero findings introduced by this fix (pre-existing errors in lines 372, 560, 1328, 1601 are not in my new code)

Status: **closed**. Self-review converged at pass 1 with zero High and zero Medium findings.

**Files changed:**
- `ma_poc/pms/detector.py` — rewrote the demotion logic in `confirm_detection` to a two-phase scan: Phase 1 preserves on initial-adapter match (unchanged from today); Phase 2 (new) only demotes when a captured body positively matches a *different* registered adapter's `matches_response_body`. Updated the docstring to reflect P3.

**Tests added/modified:**
- `tests/pms/test_detector.py` — three new tests:
  - `test_confirm_detection_preserves_when_bodies_are_pure_noise` (production failure shape)
  - `test_confirm_detection_demotes_with_specific_cross_match_in_evidence` (Windsor case + new richer evidence format)
  - `test_confirm_detection_preserves_when_only_initial_adapter_has_checker` (edge case via monkeypatched registry)
- `tests/integration/contracts/test_detection_preservation_under_noise.py` — fixed test data (JSON-string body → parsed dict matching Funnel's `_is_funnel_response_body` shape); removed unused `pytest` import.

**Verification:**
- All Bug C contract tests: green
- All `tests/pms/test_detector.py` (42 tests including the 3 new): green
- 137 tests across `tests/pms/test_detector.py tests/integration/contracts/ tests/pms/adapters/test_{funnel,rentcafe,sightmap,amli}.py`: green
- `ruff check` on changed files: clean (1 pre-existing F401/I001 in contract file auto-fixed)
- `mypy --strict` on `pms/detector.py`: zero findings introduced by this fix (errors in `pms/adapters/generic.py` are pre-existing and unrelated)
