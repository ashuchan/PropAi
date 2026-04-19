# Phase J9 — Bug Hunt & Final Gate

> Systematic pass over the refactored code looking for the bug classes
> that bit the original implementation. Walk the checklist module by
> module; for each item either confirm the bug is absent or open a fix
> commit and re-gate. Once the checklist is green, run the final gate
> over all phases.
>
> Reference: Jugnu architecture §7.1 J9 gate. Bridges to
> `claude_refactor.md` Phase 9 — reuse its checklist where applicable.

---

## Inbound handoff (from J8)

From J8 you must have:

- `scripts/daily_runner.py` running end-to-end through Jugnu layers.
- All phase gates 0–8 individually green.
- A clean `--limit 20` run producing the full artefact set.

---

## What J9 delivers

1. **`docs/BUG_HUNT_CHECKLIST.md`** — the checklist below, committed
   with every item marked `[ ]` initially. Each item is ticked to
   `[x]` as it's verified.
2. **`scripts/gate_jugnu.py`** — the unified gate script that runs
   every phase's gate and the cross-cutting final checks.
3. **`docs/JUGNU_COMPLETE.md`** — a short completion document with
   the before/after metrics table and references to every commit.

---

## Bug hunt checklist

Work through each item. For each: either confirm absence or fix and
re-test.

### Fetch layer (J1)

- [ ] `fetch()` never raises on any input. Fuzz with: `""`,
      `"not a url"`, `"javascript:alert(1)"`, `"http://"` (no host),
      `"http://[::1]"` (IPv6), unicode URLs.
- [ ] Proxy-pool `pick()` returns `None` when pool is empty; caller
      doesn't crash.
- [ ] Rate-limiter `acquire()` eventually returns; no deadlock under
      fuzz (spawn 100 concurrent acquires against a 1-RPS bucket).
- [ ] Conditional cache: deleting `data/cache/conditional.sqlite`
      mid-run does not crash; next fetch runs as a cache miss.
- [ ] Robots fetch timeout does not stall the whole fetcher — cap at
      5s, then default to allow.
- [ ] CAPTCHA detector does not false-positive on common marketing
      pages (sanity-check a page containing `"captcha"` in body text
      but no challenge widget).
- [ ] Browser context is closed in all code paths, including
      exceptions. No zombie contexts after a 100-property run (check
      with `pgrep -a chromium` on Linux).
- [ ] Every `fetch.*` event carries a `property_id` and `task_id`.

### Discovery layer (J2)

- [ ] Scheduler does not yield the same task twice across retries.
- [ ] Frontier deduplicates URLs case-insensitively in host, but
      preserves path case.
- [ ] DLQ retries escalate from hourly to daily at the 6h mark.
- [ ] Carry-forward fires on fetch hard-fail AND on consistent
      empty-records AND on validation-majority-reject.
- [ ] Sitemap consumer caps child-file follow at 10.
- [ ] Change detector is pure: no side effects, same inputs same
      outputs (property test with hypothesis, 100 runs).

### Extraction layer (J3)

- [ ] `detect_pms()` never raises. Fuzz: `None`, `""`, `"not-a-url"`,
      `"javascript:alert(1)"`, binary bytes decoded as latin-1.
- [ ] `detect_pms()` is deterministic — same inputs produce same
      `DetectedPMS` including same `evidence` ordering.
- [ ] `get_adapter()` never returns `None` — unknown PMS returns
      generic.
- [ ] Resolver does not navigate more than 5 hops on any input (test
      with a cycle: `A→B→A`).
- [ ] Resolver cancels in-flight navigation on exception — no
      zombie browser tabs after a timeout.
- [ ] Orchestrator: on SSL error, **no adapter is called**. Verified
      by asserting zero `extract.adapter_selected` events when
      fetch hard-fails.
- [ ] Orchestrator: LLM/Vision only run inside the generic adapter's
      cascade, never inside a specific-PMS adapter. Grep:
      `openai`, `llm_extractor`, `vision_extractor` absent from
      every `adapters/<name>.py` except `generic.py`.
- [ ] No PMS string literal outside its adapter, the detector, or
      the resolver. Banned-string grep over
      `ma_poc/pms/scraper.py`, `ma_poc/pms/resolver.py`,
      `ma_poc/pms/adapters/generic.py`:
      `sightmap`, `rentcafe`, `appfolio`, `entrata`,
      `avaloncommunities`, `onlineleasing`, `realpage`, `yardi`.
- [ ] `tier_used` always follows `<adapter>:<tier_key>` format.

### Validation layer (J4)

- [ ] Every rejection emits a `validate.record_rejected` event with
      at least one reason code.
- [ ] Identity fallback never collides for two distinct real records
      on a sampled fixture of 1,000 units.
- [ ] Validator never mutates its inputs (pure function property
      test).
- [ ] `next_tier_requested` triggers on >50%, not ≥50%, rejection.

### Observability layer (J5)

- [ ] Event ledger is fsync-safe — killing the process mid-write
      leaves the file re-openable; replay skips the truncated line.
- [ ] Cost ledger: concurrent writes from the worker pool don't
      corrupt the DB (use SQLite WAL mode; verify with a 20-worker
      stress test).
- [ ] Replay CLI on a missing cid exits nonzero with a clear
      error, doesn't crash.
- [ ] SLO watcher: threshold comparisons use `>` not `>=` (a value
      exactly at threshold is green, not a violation).
- [ ] `emit()` swallows all exceptions and logs once per failure
      class (not per event).

### Profile layer (J6)

- [ ] Migration is idempotent — running twice leaves disk identical
      (hash compare).
- [ ] Every v1 JSON in `config/profiles/` has a corresponding
      `_audit/<cid>_v1.json` after migration.
- [ ] LRU caps trim on load, not only on append — a v1 profile with
      200 explored links loads as a v2 profile with 50.
- [ ] `profile.stats.total_scrapes` only increments on full scrape
      attempts, not on HEAD-only cache-hit cycles.

### Reporting layer (J7)

- [ ] Per-property report's verdict is the first line after the
      title. Grep any report's line 3: must start with
      `**Verdict:**`.
- [ ] LLM transcripts are always inside `<details>`; no raw LLM
      prompts leak into top-level markdown.
- [ ] Run-level report renders when there are zero properties
      (edge case — empty CSV).
- [ ] Run-level report's PMS table omits PMSs with 0 properties.
- [ ] Verdict `FAILED_UNREACHABLE` beats `FAILED_NO_DATA` when both
      apply.

### Integration (J8)

- [ ] `--limit 0` exits cleanly with a meaningful message, not a
      traceback.
- [ ] `--start-at N` where N > CSV size exits cleanly.
- [ ] Running daily_runner with no proxy (`--proxy ""`) works —
      proxy_pool returns `None`, fetcher proceeds without proxy.
- [ ] Running without Azure OpenAI env vars (`AZURE_OPENAI_API_KEY`
      unset): generic adapter's LLM tier silently skips; pipeline
      still completes; cost is 0.
- [ ] StateStore concurrent access: two daily_runner invocations
      at the same time don't corrupt state (they shouldn't both
      run; add an advisory lock file to enforce).
- [ ] 46-key schema preserved on every property record.

### Cross-cutting

- [ ] No module imports from a higher layer. Grep check:
    - `ma_poc/fetch/` does not import `ma_poc.discovery`,
      `ma_poc.pms`, `ma_poc.validation`.
    - `ma_poc/discovery/` does not import `ma_poc.pms`,
      `ma_poc.validation`.
    - `ma_poc/pms/` does not import `ma_poc.validation`,
      `ma_poc.reporting`.
    - `ma_poc/validation/` does not import `ma_poc.reporting`.
    - (All layers may import `ma_poc.observability` — it's the
      utility belt.)
- [ ] No module over 500 lines.
- [ ] No function over 60 lines.
- [ ] `mypy --strict` clean across every layer.
- [ ] `ruff check` clean across every layer.
- [ ] `pytest` total runtime under 5 minutes on a clean run (tests
      shouldn't do real I/O).

---

## The unified gate script

`scripts/gate_jugnu.py` — one file, one CLI:

```bash
python scripts/gate_jugnu.py phase 0    # J0 gate
python scripts/gate_jugnu.py phase 1    # J1 gate
...
python scripts/gate_jugnu.py phase 9    # J9 gate (cross-cutting + checklist)
python scripts/gate_jugnu.py all        # all phases in order, stop on first fail
python scripts/gate_jugnu.py final      # only J9 cross-cutting checks
```

### Structure

```python
# scripts/gate_jugnu.py
"""
Jugnu unified gate runner.

Every phase has one top-level function. Each returns a GateResult.
"""
from __future__ import annotations
import argparse, json, logging, subprocess, sys
from dataclasses import dataclass, asdict
from datetime import datetime, UTC
from pathlib import Path

@dataclass
class GateResult:
    phase: int
    passed: bool
    reasons: list[str]
    observed: dict                   # metrics observed during the gate

def check_phase_0() -> GateResult: ...
def check_phase_1() -> GateResult: ...
def check_phase_2() -> GateResult: ...
def check_phase_3() -> GateResult: ...
def check_phase_4() -> GateResult: ...
def check_phase_5() -> GateResult: ...
def check_phase_6() -> GateResult: ...
def check_phase_7() -> GateResult: ...
def check_phase_8() -> GateResult: ...
def check_phase_9() -> GateResult:
    """Cross-cutting: checklist + coverage + static + observable."""
    ...

# Each phase's check_phase_N shells out to pytest with -k markers
# that select only that phase's tests, plus runs the specific grep
# and observable assertions documented in PHASE_J{N}_*.md.

def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("phase"); p.add_argument("n", type=int)
    sub.add_parser("all")
    sub.add_parser("final")
    args = parser.parse_args()
    ...
```

### Gate output format

For `phase N`:

```
Jugnu gate — phase {N}
=======================
[✓] tests pass (47/47)
[✓] ruff clean
[✓] mypy strict clean
[✓] coverage 88% (≥ 85%)
[✓] observable: timeout rate 7.2% (< 10%)
[✓] observable: never-fail — no tracebacks in stderr

RESULT: PASS
```

For `all`:

```
Jugnu gate — all phases
========================
Phase 0: PASS
Phase 1: PASS
Phase 2: PASS
Phase 3: FAIL
  - no-pms-literals check: 'entrata' found in ma_poc/pms/scraper.py:142
  - tier namespace check: 'TIER_1_API' does not match '<adapter>:<tier_key>'
Stopping.
```

Persist the result as `data/gates/<ISO>.json`.

---

## Cross-cutting final gate checks (J9 only)

Beyond re-running phase 0–8 gates, J9 adds:

### 1. Static analysis sweep

```bash
ruff check ma_poc/ scripts/
mypy --strict ma_poc/
```

### 2. Coverage thresholds

| Package | Min coverage |
|---|---|
| `ma_poc/fetch/` | 85% |
| `ma_poc/discovery/` | 85% |
| `ma_poc/pms/` | 85% |
| `ma_poc/validation/` | 90% |
| `ma_poc/observability/` | 85% |
| `ma_poc/reporting/` | 85% |
| `ma_poc/models/` | 90% |

### 3. Banned strings grep

```
# PMS literals outside allowed files
forbidden = {"sightmap", "rentcafe", "appfolio", "entrata",
             "avaloncommunities", "onlineleasing", "realpage", "yardi"}
allowed_files = {
    "ma_poc/pms/adapters/{pms}.py",
    "ma_poc/pms/detector.py",
    "ma_poc/pms/resolver.py",
}
# Grep for each; fail if found outside allowed_files.
```

### 4. Layer-import check

```python
LAYER_ORDER = ["fetch", "discovery", "pms", "validation", "reporting"]
# fetch cannot import discovery, pms, validation, reporting
# discovery cannot import pms, validation, reporting
# …and so on
# (observability is allowed from every layer)
```

Run via `pyflakes` or a custom AST walker.

### 5. Checklist completeness

`docs/BUG_HUNT_CHECKLIST.md` — parse `[x]`/`[ ]` markers. Fail if any
`[ ]` remains.

### 6. Observable checks on `--limit 100` run

- Failure rate ≤ 5%.
- LLM cost concentrated entirely in `unknown` PMS rows of the PMS
  table.
- Change-detection skip rate ≥ 30% (once properties have been
  scraped once to populate ETag cache — this is a second-day
  check; document it as "run twice to verify").
- Replay tool can reconstruct any failed property from today's
  artefacts.
- Carry-forward records exist for any fetch-failed property with
  prior data in state.

### 7. Baseline comparison

Re-run `scripts/jugnu_baseline.py` against the new
`data/runs/<today>/`. Compare with `data/baseline/<J0-date>.json`.
Assert:

| Metric | Improvement required |
|---|---|
| Failure rate | ≤ 5% (from baseline ~24%) |
| UNIT_MISSING_ID count | ≥ 95% drop |
| UNIT_INVALID_RENT count | ≥ 95% drop |
| Wasted LLM calls | 0 (from baseline > 0) |
| `api_provider == null` profiles | < 10% of total |
| LLM cost per run | ≤ 10% of baseline |

Write the comparison table into `docs/JUGNU_COMPLETE.md`.

---

## `docs/JUGNU_COMPLETE.md` — the sign-off

Structure:

```markdown
# Jugnu — Completion Report

**Completed:** <ISO date>
**Baseline:** <J0 date>
**Final run:** <J8 final date>

## Summary

Jugnu is complete when every row below is ✓.

| Check | Status | Baseline | Final |
|---|---|---|---|
| Phase gates 0–9 individually green | ✓ | — | — |
| Failure rate ≤ 5% | ✓ | 24.2% | 4.1% |
| UNIT_MISSING_ID dropped ≥ 95% | ✓ | 1,014 | 32 |
| UNIT_INVALID_RENT dropped ≥ 95% | ✓ | 297 | 5 |
| Wasted LLM calls = 0 | ✓ | 14 | 0 |
| `api_provider == null` < 10% | ✓ | 62% | 7% |
| LLM cost per run ≤ 10% baseline | ✓ | $2.40 | $0.19 |
| Change-detection skip ≥ 30% (day 2) | ✓ | 0% | 44% |
| Never-fail contract — no traceback | ✓ | — | — |
| 46-key schema preserved | ✓ | — | — |
| Bug-hunt checklist 100% ticked | ✓ | — | — |

## Layer delivery commits

| Layer | Phase | Commit |
|---|---|---|
| L1 Fetch | J1 | <sha> |
| L2 Discovery | J2 | <sha> |
| L3 Extraction | J3 | <sha> |
| L4 Validation | J4 | <sha> |
| L5 Observability | J5 | <sha> |

## Out of scope — confirmed deferred

- Tier-6 syndication fallback.
- REIT custom stacks beyond AvalonBay.
- Cross-property clustering via `client_account_id`.
- CAPTCHA solver integration.
- Azure Service Bus distributed execution.

## Open follow-ups

(List any items that surfaced during J9 bug hunt that became new
tickets.)
```

---

## Refactoring / code-quality checklist

Done at this phase because J9 is the last chance to clean up:

- [ ] Delete any TODO/FIXME comments that are actionable; file
      tickets for the rest.
- [ ] Remove all `# J1 SHIM`-style temporary comments.
- [ ] Consolidate any duplicated test fixtures across layers.
- [ ] Verify `pyproject.toml` lists exact minimum versions for new
      dependencies (`httpx`, `jinja2` if introduced, etc.).
- [ ] Verify `.gitignore` excludes `data/cache/`, `data/raw_html/`,
      `data/baseline/`, `data/gates/`, `data/migrations/`.
- [ ] Verify `config/profiles/_audit/` is ignored.

---

## Gate — `scripts/gate_jugnu.py phase 9` (= `final`)

Passes iff:

- Phases 0–8 all individually green.
- Checklist 100% ticked.
- Static analysis sweep clean.
- Coverage thresholds met.
- Observable checks on limit-100 run pass.
- Baseline comparison table in `JUGNU_COMPLETE.md` meets every
  target.

**When this gate is green, Jugnu is done.**

---

## Outbound handoff (to production)

- **`docs/JUGNU_COMPLETE.md`** — the sign-off document.
- **All phase commits** merged to main, tagged `jugnu-v1.0`.
- **Runbook update:** `docs/RUNBOOK.md` (out of this instruction
  set's scope but flag it — the existing runbook needs a section
  on the new DLQ retry process, the replay tool, and how to read
  the per-property reports).
- **Monitoring hooks:** SLO violations from `slo_watcher` surface
  in the run report; wiring these to an alerting system is post-
  J9 work.

Commit: `Jugnu J9: bug hunt complete, all gates green — v1.0`.
Tag: `jugnu-v1.0`.

---

*End of Jugnu implementation guide. Return to `CLAUDE_JUGNU.md` for
the phase index.*
