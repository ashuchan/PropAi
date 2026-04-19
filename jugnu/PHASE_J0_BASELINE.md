# Phase J0 — Baseline Metrics

> Capture current-pipeline metrics **before** touching any code. Every
> later phase is measured against this baseline. Without J0 numbers,
> later gates cannot tell whether we improved things.
>
> Reference: Jugnu architecture §7.1 (J0), §2.2 (what the 04-17 run
> told us).

---

## Inbound handoff (from — prerequisite reading)

- `CLAUDE_JUGNU.md` — read in full.
- `JUGNU_CONTRACTS.md` — skim; J0 doesn't consume any contract yet, but
  knowing the shape helps when interpreting what we're baselining.

You do **not** need to have read `claude_refactor.md` yet.

---

## Goal

Produce a written, reproducible baseline document that answers:

1. What percentage of properties succeed today?
2. Where does LLM cost go?
3. How many properties have no `api_provider` detected?
4. How many failures carry forward, and how many should have?
5. What are the most common error signatures?

Every number here becomes a target to move in a later phase.

---

## Files to create

| Path | Purpose |
|---|---|
| `scripts/jugnu_baseline.py` | One-shot script that reads the latest run and emits metrics |
| `docs/JUGNU_BASELINE.md` | Human-readable baseline document (output of the script + hand-notes) |
| `tests/baseline/__init__.py` | empty |
| `tests/baseline/test_jugnu_baseline.py` | Named tests (below) |

No production code changes in J0. This is pure instrumentation.

---

## `scripts/jugnu_baseline.py` — specification

### Inputs

- The most recent directory under `data/runs/` (by name-sort; the
  dated directories are `YYYY-MM-DD` which sort lexicographically).
- All files under `config/profiles/*.json`.

### Metrics to produce

Each metric gets its own section in the output markdown. Produce
**both** a machine-readable JSON (`data/baseline/<date>.json`) and the
markdown.

1. **Run-level totals**
   - `csv_rows`, `properties_ok`, `properties_failed`, failure rate %
   - `properties_carry_forward`, `properties_dlq_eligible` (failed ≥3
     consecutive runs — compute from current run + previous 2 runs if
     available; otherwise just current-run failures)

2. **Tier distribution**
   - Count by `extraction_tier_used` (from `properties.json`).
   - % of total.

3. **LLM cost breakdown**
   - Total `$` spent, from `llm_report.json` if present, else sum
     `_llm_interactions[].cost_usd` across properties.
   - Count of properties that made ≥1 LLM call.
   - Count of properties that made ≥1 Vision call.
   - Avg `$` per LLM-using property.
   - **Wasted LLM calls** — properties where `units` is empty AND
     `LLM Calls Made > 0`. The property-5317 pattern.

4. **Failure breakdown**
   - For properties with `extraction_tier_used == "FAILED"`, group by
     the first 80 chars of the first error string.
   - Report count and sample canonical_ids (up to 5) per group.

5. **Profile maturity distribution**
   - Count of profiles by `confidence.maturity` (COLD/WARM/HOT).
   - Count where `api_hints.api_provider` is `null` or missing.
   - Same as % of total profiles.

6. **Timing**
   - If `scraped_at` is recorded per property, compute p50 and p95
     per-property scrape duration from `report.md` or the properties
     records.
   - If no timing available, emit `null` — don't invent numbers.

7. **Change-detection skip rate** (baseline will be 0 — current
   pipeline doesn't do this, and that's the point)
   - Always 0% today; reports this so later phases can show
     improvement.

### Script structure

```python
# scripts/jugnu_baseline.py
"""
Jugnu J0 — capture baseline metrics from the latest run.

Usage: python scripts/jugnu_baseline.py [--run-dir data/runs/2026-04-17]
"""
from __future__ import annotations
import argparse, json, logging
from pathlib import Path
from dataclasses import dataclass, asdict

log = logging.getLogger("jugnu_baseline")

@dataclass
class BaselineMetrics:
    run_dir: str
    totals: dict
    tier_distribution: dict
    llm_cost: dict
    failure_signatures: list[dict]
    profile_maturity: dict
    timing: dict
    change_detection: dict

def find_latest_run_dir(runs_root: Path) -> Path: ...
def load_properties_json(run_dir: Path) -> list[dict]: ...
def compute_totals(props: list[dict]) -> dict: ...
def compute_tier_distribution(props: list[dict]) -> dict: ...
def compute_llm_cost(props: list[dict], run_dir: Path) -> dict: ...
def compute_failure_signatures(props: list[dict]) -> list[dict]: ...
def compute_profile_maturity(profiles_dir: Path) -> dict: ...
def compute_timing(props: list[dict], run_dir: Path) -> dict: ...
def write_markdown(metrics: BaselineMetrics, out_path: Path) -> None: ...
def main() -> int: ...
```

Keep each function **under 40 lines**. Each one has a single
responsibility (SRP). The main() function wires them.

### Output markdown structure

Write to `docs/JUGNU_BASELINE.md` with this exact skeleton:

```markdown
# Jugnu Baseline — captured {ISO date}

Source run: `data/runs/{date}/`

## 1. Totals
<table: metric, value, notes>

## 2. Tier distribution
<table: tier, count, pct>

## 3. LLM cost
<table: metric, value>

### Wasted LLM calls
Properties with empty `units` that nevertheless made LLM calls:
<list of canonical_ids with cost>

## 4. Failure signatures
<table: signature, count, sample cids>

## 5. Profile maturity
<table: maturity, count, pct>

Properties with `api_provider == null`: {N} ({pct}%).

## 6. Timing
<p50 / p95 per-property scrape seconds>

## 7. Change detection
Current skip rate: 0% (not implemented).

## 8. Targets for Jugnu (to be filled by the human)

| Metric | Current (J0) | Target (post-J9) |
|---|---|---|
| Success rate | {auto-filled} | ≥ 95% |
| LLM cost / run | {auto-filled} | ≤ {auto-filled × 0.1} |
| Wasted LLM calls | {auto-filled} | 0 |
| api_provider == null | {auto-filled} | < 10% |
| Change-detection skip | 0% | ≥ 30% |
| Failure rate | {auto-filled} | ≤ 5% |
```

Leave the §8 target column blank for the human to approve. Fill the
"Current (J0)" column automatically.

---

## Named tests (write these first)

All under `tests/baseline/test_jugnu_baseline.py`.

| Test | What it checks |
|---|---|
| `test_baseline_finds_latest_run` | Given a tree with `data/runs/2026-04-13/` and `data/runs/2026-04-15/`, `find_latest_run_dir` returns the 04-15 path |
| `test_baseline_handles_empty_runs_dir` | Empty `data/runs/` → `find_latest_run_dir` raises `FileNotFoundError` with a clear message |
| `test_baseline_tier_distribution_aggregates_correctly` | Fixture `properties.json` with 3 TIER_1_API, 2 TIER_3_DOM, 1 FAILED → distribution has 3/2/1 |
| `test_baseline_llm_wasted_calls_counted` | Fixture with one record where `units=[]` and `_llm_interactions=[{cost_usd: 0.001}]` → wasted_count==1 |
| `test_baseline_failure_signature_grouping` | 5 failures with error "ERR_SSL_PROTOCOL_ERROR" + 3 with "Timeout" → two signature groups |
| `test_baseline_profile_maturity_counts_null_providers` | 3 profiles where 2 have `api_hints.api_provider==null` → count is 2 |
| `test_baseline_handles_missing_profiles_dir` | No `config/profiles/` → `compute_profile_maturity` returns zeros with a warning log, doesn't raise |
| `test_baseline_writes_both_json_and_md` | After `main()`, both `data/baseline/<date>.json` and `docs/JUGNU_BASELINE.md` exist |
| `test_baseline_is_idempotent` | Running twice on same data produces same numbers |

Use `tmp_path` fixtures. No live filesystem writes outside the tmp dir
during tests.

---

## Refactoring / code-quality checklist

Even this small script holds to the conventions:

- [ ] No function > 40 lines.
- [ ] Type hints on every signature.
- [ ] `ruff check` clean, `mypy --strict` clean.
- [ ] Docstring on every public function.
- [ ] `BaselineMetrics` is a frozen dataclass.
- [ ] Uses `logging`, not `print`.
- [ ] No module-level side effects (no disk writes at import time).

---

## Gate — `scripts/gate_jugnu.py phase 0`

Passes iff **all** of:

- `docs/JUGNU_BASELINE.md` exists and its `## 1. Totals` section has
  at least one filled-in row (not the `{auto-filled}` placeholder).
- `scripts/jugnu_baseline.py` runs to completion on the real
  `data/runs/*` without raising.
- A `data/baseline/<date>.json` file exists for the source run.
- All 9 tests in `tests/baseline/` pass.
- `ruff check scripts/jugnu_baseline.py` returns no issues.
- `mypy --strict scripts/jugnu_baseline.py` returns no errors.

**Do not proceed to J1 until this gate is green.**

---

## Outbound handoff (to phase J1)

After J0 is complete, the following must exist and be referenced from
later phases:

- **File** `docs/JUGNU_BASELINE.md` — the baseline numbers. J9's gate
  script re-reads this to compute % improvement.
- **File** `scripts/jugnu_baseline.py` — can be re-run after J9 to
  produce a "post-Jugnu" comparison.
- **Number** `baseline.failure_rate_pct` — J1's gate asserts the new
  fetch layer reduces this by specific margins on a limit-50 run.
- **Number** `baseline.llm_cost_per_run_usd` — J8's gate asserts the
  end-to-end run's LLM cost is ≤ 10% of this.
- **Number** `baseline.api_provider_null_pct` — J6's gate asserts
  migration reduces this below 10%.
- **File** `data/baseline/<date>.json` — machine-readable for gate
  scripts.

Commit this phase with message: `Jugnu J0: baseline metrics captured`.

---

*Next: `PHASE_J1_FETCH.md`.*
