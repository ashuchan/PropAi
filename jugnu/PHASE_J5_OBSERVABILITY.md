# Phase J5 — Layer 5 Observability & Control

> Replaces print-to-stdout with a structured event ledger, per-property
> cost accounting, a replay tool over stored raw HTML, and SLO
> watchers. J5 is the first phase where we can *diagnose* a failure
> from artefacts — turning parser-regression investigations from days
> into minutes.
>
> Reference: Jugnu architecture §4.2 L5, §3.6 (observability gaps),
> §5.1 end-to-end flow, §7.1 J5 gate.

---

## Inbound handoff (from J1–J4)

Every previous layer (J1 Fetch, J2 Discovery, J3 Extraction, J4
Validation) has been emitting events via a stub `emit()` helper that
writes to `logging.info`. J5 replaces that stub with the real
implementation.

You must have:

- Stub `ma_poc/observability/events.py` exists with the `EventKind`
  enum and the `emit()` signature. Call sites exist in L1–L4.
- Raw HTML for `outcome == OK` fetches is being written to
  `data/raw_html/<date>/<property_id>.html.gz` (from J1).
- `ExtractResult.llm_cost_usd`, `vision_cost_usd`, `llm_calls`,
  `vision_calls` populated by L3 adapters.

If any layer's `emit()` stub has drifted from the event names in
`JUGNU_CONTRACTS.md` §5, fix it before starting J5.

---

## What J5 delivers

Five distinct capabilities, one module each:

1. **Event ledger** — `data/runs/<date>/events.jsonl`, append-only.
2. **Cost ledger** — `data/runs/<date>/cost_ledger.db` (SQLite).
3. **Replay tool** — `scripts/replay.py` reconstructs a property from
   stored raw HTML + events.
4. **SLO watcher** — flags when failure rate, cost, or vision
   fallback exceeds thresholds.
5. **DLQ controller** — reads L2's DLQ, emits `due_for_retry`
   properties back into tomorrow's run plan.

---

## Module breakdown

```
ma_poc/observability/
├── __init__.py
├── events.py              # EventKind enum, Event dataclass, emit()
├── event_ledger.py        # Append to events.jsonl with buffering
├── cost_ledger.py         # SQLite cost accumulator, per-PMS rollup
├── replay_store.py        # Read raw HTML + events for a cid
├── slo_watcher.py         # Threshold checks, emit alerts
└── dlq_controller.py      # DLQ retry scheduler

scripts/
└── replay.py              # CLI wrapping replay_store
```

### Responsibilities

| Module | Owns |
|---|---|
| `events.py` | Event shape, `emit()` entry point, `EventKind` enum |
| `event_ledger.py` | Writing events to disk with buffered flushing, crash-safety |
| `cost_ledger.py` | Running SQLite totals of LLM/Vision/Proxy cost by property and PMS |
| `replay_store.py` | Looking up raw HTML + event stream for a (cid, date) pair |
| `slo_watcher.py` | Checking run-level metrics against thresholds; raising alerts |
| `dlq_controller.py` | Reading DLQ state, deciding retry schedule |
| `scripts/replay.py` | CLI: `python scripts/replay.py --cid 5317 --date 2026-04-17` → rebuild property artifacts |

---

## File creation order

1. `ma_poc/observability/__init__.py`
2. `ma_poc/observability/events.py` — promote from stub to real
3. `ma_poc/observability/event_ledger.py`
4. `ma_poc/observability/cost_ledger.py`
5. `ma_poc/observability/replay_store.py`
6. `ma_poc/observability/slo_watcher.py`
7. `ma_poc/observability/dlq_controller.py`
8. `scripts/replay.py`

---

## Module specifications

### 2. `events.py` — promote stub to real

Replace the stub `emit()` with a real dispatcher.

```python
# ma_poc/observability/events.py
from __future__ import annotations
import threading
from pathlib import Path
from typing import Any
from .event_ledger import EventLedger

_ledger: EventLedger | None = None
_ledger_lock = threading.Lock()

def configure(run_dir: Path, run_id: str) -> None:
    """Called once at daily_runner startup."""
    global _ledger
    with _ledger_lock:
        _ledger = EventLedger(run_dir / "events.jsonl", run_id)

def emit(
    kind: EventKind,
    property_id: str = "",
    task_id: str | None = None,
    **data: Any,
) -> None:
    """Append one event to the ledger. Never raises."""
    if _ledger is None:
        # Not configured yet — falls back to logging
        import logging; logging.getLogger(__name__).info(
            f"event(unconfigured) {kind.value} {property_id} {data}"
        )
        return
    try:
        _ledger.append(Event(
            kind=kind, property_id=property_id, task_id=task_id, data=data
        ))
    except Exception:
        # Never allow observability to kill a scrape
        import logging; logging.getLogger(__name__).warning(
            f"emit failed for {kind.value}", exc_info=True
        )
```

### 3. `event_ledger.py`

```python
class EventLedger:
    def __init__(
        self, path: Path, run_id: str, buffer_size: int = 16,
    ) -> None: ...
    def append(self, event: Event) -> None:
        """Buffered write. Flushes every buffer_size events or 2s."""
    def flush(self) -> None: ...
    def close(self) -> None:
        """Flush and close. Called at run end."""
```

Buffered to avoid fsync on every event; small buffer (16) so crash
loses at most 16 events. Line-buffered at the Python level; append
mode on open. Ruthlessly non-blocking — if disk is full, drop events
and log a single warning per run.

**Named tests (6):**

- `test_ledger_appends_event_in_jsonl_format`
- `test_ledger_buffer_flushes_on_size`
- `test_ledger_buffer_flushes_on_close`
- `test_ledger_appends_from_multiple_threads` (feed 1000 events from 4
  threads, verify all 1000 present in file)
- `test_ledger_prepends_run_id_to_every_event`
- `test_ledger_truncated_mid_line_on_prior_crash_can_be_re_opened`

### 4. `cost_ledger.py`

SQLite-backed running totals. Schema:

```sql
CREATE TABLE cost_entries (
    ts TEXT NOT NULL,
    property_id TEXT NOT NULL,
    pms TEXT,                      -- from extract.pms_detected event
    tier_used TEXT,                -- e.g. "entrata:widget_api"
    category TEXT NOT NULL,        -- 'llm' | 'vision' | 'proxy_mb'
    cost_usd REAL NOT NULL,
    detail TEXT                    -- JSON dict (model name, tokens, etc.)
);
CREATE INDEX cost_by_prop ON cost_entries(property_id);
CREATE INDEX cost_by_pms ON cost_entries(pms);
```

```python
class CostLedger:
    def __init__(self, db_path: Path) -> None: ...
    def record_llm(self, property_id: str, pms: str, tier: str,
                   cost: float, model: str, tokens: int) -> None: ...
    def record_vision(self, property_id: str, pms: str, tier: str,
                      cost: float, model: str) -> None: ...
    def record_proxy_bytes(self, property_id: str, pms: str,
                           bytes_used: int, rate_per_mb: float) -> None: ...
    def rollup_by_pms(self) -> dict[str, dict[str, float]]: ...
    def total(self) -> dict[str, float]: ...
    def wasted_calls(self) -> list[dict]:
        """Properties where units_count==0 but llm cost > 0.
        Used by the J5 SLO watcher and J7's report."""
```

**Named tests (7):**

- `test_cost_record_llm_persists`
- `test_cost_rollup_by_pms_aggregates`
- `test_cost_total_sums_all_categories`
- `test_cost_wasted_calls_identifies_zero_units_with_cost`
- `test_cost_db_survives_reopen`
- `test_cost_concurrent_writes_do_not_corrupt`
- `test_cost_records_detail_as_json`

### 5. `replay_store.py`

```python
@dataclass
class ReplayPayload:
    property_id: str
    date: str
    raw_html: bytes | None
    events: list[Event]
    extract_result: ExtractResult | None  # reconstructed from events

class ReplayStore:
    def __init__(self, runs_root: Path, raw_html_root: Path) -> None: ...
    def load(self, property_id: str, date: str) -> ReplayPayload: ...
    def list_available_dates(self, property_id: str) -> list[str]: ...
```

The replay re-feeds the stored HTML into L3's extractor to reproduce
the failure. Parser regressions become testable: check in the
`raw_html/<date>/<cid>.html.gz` as a fixture, and the new parser
must produce the same units as the old one.

**Named tests (5):**

- `test_replay_loads_raw_html_and_events_for_cid`
- `test_replay_returns_empty_events_if_no_ledger_for_date`
- `test_replay_list_dates_returns_sorted`
- `test_replay_handles_missing_html_gracefully`
- `test_replay_reconstructs_extract_result_from_events`

### 6. `slo_watcher.py`

Pure logic. Input: cost ledger rollup + event counts. Output: list of
SLO violations.

```python
@dataclass(frozen=True)
class SloThresholds:
    success_rate_min: float = 0.95
    llm_cost_per_run_max_usd: float = 1.00
    vision_fallback_max_pct: float = 0.05
    drift_noise_max_pct: float = 0.02

@dataclass(frozen=True)
class SloViolation:
    name: str
    threshold: float
    observed: float
    sample: list[str]          # up to 5 example cids

def check(
    cost_rollup: dict,
    event_counts: dict[EventKind, int],
    property_results: list[dict],
    thresholds: SloThresholds = SloThresholds(),
) -> list[SloViolation]: ...
```

Target SLOs from architecture doc §5.2:
- `success_rate ≥ 95%`
- `llm_cost < $1/day`
- `vision_fallback ≤ 5%`
- `drift_noise < 2%`

**Named tests (6):**

- `test_slo_all_green_returns_empty`
- `test_slo_success_rate_violation`
- `test_slo_llm_cost_violation_samples_top_spenders`
- `test_slo_vision_fallback_violation`
- `test_slo_drift_noise_violation`
- `test_slo_custom_thresholds_respected`

### 7. `dlq_controller.py`

Thin wrapper over the J2 DLQ primitive. Reads DLQ state, decides
retry schedule, emits `discovery.dlq_retry_scheduled` events.

```python
class DlqController:
    def __init__(self, dlq: Dlq, event_emit: Callable) -> None: ...
    def schedule_retries_for(self, run_date: datetime) -> list[str]:
        """Returns list of property_ids due for retry this run."""
    def park_after_validation_failure(
        self, property_id: str, extract_result: ExtractResult,
    ) -> None:
        """Decide whether a consistently empty scrape warrants parking."""
```

Keeps J2's DLQ a pure data structure; the *policy* lives here.

**Named tests (4):**

- `test_dlq_controller_returns_due_ids`
- `test_dlq_controller_emits_event_on_schedule`
- `test_dlq_controller_parks_on_repeated_unreachable`
- `test_dlq_controller_does_not_park_on_parse_failures_alone`

### 8. `scripts/replay.py`

CLI tool. Usage:

```
python scripts/replay.py --cid 5317 --date 2026-04-17
  → reconstructs property 5317's scrape from 2026-04-17.
  → prints event timeline.
  → optionally re-runs L3 extractor on the raw HTML (--rerun flag).
  → writes replay_<cid>_<date>.md with the full story.
```

```python
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cid", required=True)
    parser.add_argument("--date", required=True)
    parser.add_argument("--rerun", action="store_true")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()
    ...
```

**Named tests (4):**

- `test_replay_cli_loads_and_prints_timeline`
- `test_replay_cli_rerun_invokes_extractor`
- `test_replay_cli_writes_markdown_report`
- `test_replay_cli_exits_nonzero_on_missing_data`

---

## Integration — wire L5 into L1–L4

After J5's modules are built and tested in isolation:

1. In `scripts/daily_runner.py` startup, call
   `ma_poc.observability.events.configure(run_dir, run_id)`.
2. In each layer, remove the stub `emit()` redefinition. All layers
   now share the canonical `ma_poc.observability.events.emit()`.
3. In `scripts/daily_runner.py` teardown, call
   `event_ledger.close()` to flush.

Keep the layer modules unchanged — only the `emit` implementation
moves.

---

## Refactoring / code-quality checklist

- [ ] No file over 300 lines.
- [ ] `mypy --strict ma_poc/observability/` clean.
- [ ] `ruff check ma_poc/observability/` clean.
- [ ] Coverage ≥ 85%.
- [ ] No layer imports `ma_poc.observability` directly — callers use
      the module-level `emit()` helper.
- [ ] `emit()` never raises (swallows all exceptions internally).
- [ ] Event ledger uses line-buffered append; crash mid-run leaves a
      valid prefix (truncated JSON line is tolerated — the replay
      tool skips it).

---

## Gate — `scripts/gate_jugnu.py phase 5`

Passes iff:

- All ~30 tests pass.
- Static analysis clean.
- Coverage ≥ 85%.
- **Observable check:** a limit-20 run produces:
    - `data/runs/<date>/events.jsonl` with ≥1 event per `EventKind`
      value that fired this run.
    - `data/runs/<date>/cost_ledger.db` with valid schema and ≥1
      row.
    - `report.md` (produced in J7 later, but the SLO section stub
      goes here) includes a `## SLO status` section.
- **Observable check:** `scripts/replay.py --cid <any_failed_cid>
  --date <yesterday>` runs to completion and produces a markdown
  with:
    - timeline of events
    - `tier_used` that the original run produced
    - a reconstructed property record from the raw HTML
- **Observable check:** the 04-17 property 5317 failure can be
  replayed and shows:
    - `fetch.completed` with `outcome: HARD_FAIL`
    - no `extract.llm_called` event
    - no `extract.vision_called` event
    (i.e. J3's Delta 5 short-circuit is visible in the ledger)

---

## Outbound handoff (to J6 Profile v2)

- **Function** `ma_poc.observability.events.emit()` — canonical, used
  by every layer.
- **Disk layout**:
    - `data/runs/<date>/events.jsonl`
    - `data/runs/<date>/cost_ledger.db`
    - `data/raw_html/<date>/` (populated by J1, consumed by replay)
- **CLI** `scripts/replay.py` stable.
- **Cost rollup function** `CostLedger.rollup_by_pms()` — J7's report
  consumes this.
- **SLO watcher** ready for J8's daily_runner to call.

Commit: `Jugnu J5: observability — event ledger, cost ledger, replay,
SLO, DLQ controller`.

---

*Next: `PHASE_J6_PROFILE_V2.md`.*
