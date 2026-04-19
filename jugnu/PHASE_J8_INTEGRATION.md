# Phase J8 — daily_runner Integration

> Wire L1–L5 into `scripts/daily_runner.py` behind its existing entry
> point. Preserve the 46-key output schema, the state file format, and
> the per-property/run outputs. Replace the J1 shim. End-to-end
> `--limit 20` runs clean.
>
> Reference: Jugnu architecture §7.1 J8 gate, §7.2 non-negotiables.
> Bridges to `claude_refactor.md` Phase 8.

---

## Inbound handoff (from J7)

From J7 you must have:

- `ma_poc.reporting.property_report.build()` and
  `ma_poc.reporting.run_report.build()` producing the expected
  markdown + JSON.
- Verdict computed for every property.

From J6 you must have:

- All v1 profiles migrated to v2.

From J5 you must have:

- `events.configure(run_dir, run_id)` ready to call at startup.
- `CostLedger` ready to write to per-run SQLite.

From earlier: J1 shim in `scripts/daily_runner.py` still present —
about to be removed.

---

## What J8 delivers

A `scripts/daily_runner.py` that runs the full Jugnu pipeline
end-to-end:

```
CSV rows
  → Scheduler (J2) — builds CrawlTasks (carry-forward, DLQ aware)
  → Fetcher (J1) — returns FetchResult
  → Scraper (J3) — returns ExtractResult + profile hints
  → Validator (J4) — returns ValidatedRecords
  → Profile updater (J6 via existing profile_updater.py)
  → state_store (existing)
  → Per-property report (J7)
  → CostLedger (J5) + SloWatcher (J5)
  → Run-level report (J7)
```

The 46-key property record schema is unchanged. Downstream consumers
see no difference.

---

## Integration tasks

### 1. Remove the J1 shim

Delete the temporary `_j1_shim_fetch` helper. Replace with the real
scheduler.

### 2. Wire the scheduler at run start

```python
# scripts/daily_runner.py — top of run_daily()

from ma_poc.discovery import Scheduler, Frontier, Dlq
from ma_poc.discovery.change_detector import decide as decide_change
from ma_poc.discovery.sitemap import SitemapConsumer
from ma_poc.observability import events
from ma_poc.observability.event_ledger import EventLedger
from ma_poc.observability.cost_ledger import CostLedger
from ma_poc.observability.slo_watcher import check as slo_check, SloThresholds

def run_daily(...):
    run_id = f"{run_date}_{uuid.uuid4().hex[:8]}"
    events.configure(run_dir, run_id)

    frontier = Frontier(Path("data/state/frontier.sqlite"))
    dlq = Dlq(Path("data/state/dlq.jsonl"))
    sitemap = SitemapConsumer(fetcher=jugnu_fetch, cond_cache=cond_cache)
    cost_ledger = CostLedger(run_dir / "cost_ledger.db")

    scheduler = Scheduler(frontier, dlq, sitemap, profile_store,
                          change_detector_fn=decide_change)
    ...
```

### 3. Replace the per-property body

Old flow (roughly):

```
_scrape_in_thread(url, proxy, ...) → scrape_property(url, ...) → dict
```

New flow:

```
async for task in scheduler.build_tasks(csv_rows, run_date):
    fetch_result = await jugnu_fetch(task)

    if fetch_result.should_carry_forward():
        cf = carry_forward_property(task.property_id, run_dir,
                                    state_store, reason=...)
        if cf is not None:
            property_records.append(cf)
            continue
        # else fall through — fetch failure with no prior data

    extract_result = await scraper.scrape(
        task=task, fetch_result=fetch_result, page=maybe_page,
        profile=profile_store.load(task.property_id),
    )

    history = state_store.unit_history(task.property_id)
    validated = validate(extract_result, history)

    # L3 may ask for the next tier
    while validated.next_tier_requested and scraper.has_more_tiers():
        extract_result = await scraper.scrape_next_tier(...)
        validated = validate(extract_result, history)

    # existing post-scrape pipeline
    property_record = transform_units_from_scrape(validated, ...)
    property_records.append(property_record)

    # Update profile (existing logic, now reading from ValidatedRecords)
    profile = update_profile_after_extraction(
        profile, extract_result.to_dict_for_profile_updater(),
        len(validated.accepted), profile_store)

    # Record outcome for frontier + DLQ
    record_task_outcome(task, fetch_result, state_store)

    # Per-property report
    md = property_report.build(task.property_id, run_dir, ...)
    (run_dir / "reports" / f"{task.property_id}.md").write_text(md)
```

**Rule:** if a new call path looks longer than the old, you've broken
the never-fail contract somewhere. Audit your try/except coverage.

### 4. End-of-run artefacts

```python
# at end of run_daily()
cost_rollup = cost_ledger.rollup_by_pms()
slo_violations = slo_check(cost_rollup, event_counts, property_records)

md, j = run_report.build(
    run_dir=run_dir,
    property_records=property_records,
    cost_rollup=cost_rollup,
    slo_violations=slo_violations,
    ...,
)
(run_dir / "report.md").write_text(md)
(run_dir / "report.json").write_text(json.dumps(j, indent=2, default=str))

events.close()   # flushes the ledger
```

### 5. Preserve the 46-key schema

The `TARGET_PROPERTY_FIELDS` set in `scrape_properties.py` is frozen.
`transform_units_from_scrape()` must continue to produce a dict with
exactly those 46 keys for each property record. Add any new Jugnu
metadata under a single `_meta` sub-key, not as new top-level keys.

```python
property_record["_meta"] = {
    "_detected_pms": {
        "pms": validated.source_extract.adapter_name,
        "confidence": detected.confidence,
    },
    "_fetch": {
        "outcome": fetch_result.outcome.value,
        "attempts": fetch_result.attempts,
        "render_mode": fetch_result.render_mode.value,
    },
    "_tier": validated.source_extract.tier_used,
    "_cost_usd": {
        "llm": validated.source_extract.llm_cost_usd,
        "vision": validated.source_extract.vision_cost_usd,
    },
}
```

### 6. Concurrency — keep what works

The existing `ThreadPoolExecutor` pool in `concurrency.py` works;
keep it. The asyncio inner loop now runs Jugnu's pipeline per
property instead of the old `scrape_property` directly. No change to
the pool sizing logic.

```python
def _run_jugnu_pipeline_in_thread(task, profile) -> dict:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_jugnu_pipeline_for_task(task, profile))
    except Exception as e:
        return {"errors": [str(e)], "property_id": task.property_id,
                "_exception": e}
    finally:
        _close_event_loop(loop)
```

### 7. StateStore — backward compat

The existing `data/state/property_index.json` and
`data/state/unit_index.json` formats are frozen. If a new field is
needed (e.g. `consecutive_unreachable`), add it on the profile —
StateStore is for *units and properties*, not for scheduling state.
Scheduling state lives in the new `frontier.sqlite` and `dlq.jsonl`.

---

## Named tests (`tests/integration/test_daily_runner_jugnu.py`)

Use a fake fetcher + fake scraper via monkeypatch — no live Playwright
or network in integration tests.

| Test | Check |
|---|---|
| `test_daily_runner_end_to_end_limit_3` | Run against 3 fake properties; assert `report.md` and `report.json` exist, 3 property reports exist, no exceptions |
| `test_daily_runner_preserves_46_key_schema` | One fake property → output has exactly `TARGET_PROPERTY_FIELDS` keys plus `_meta` |
| `test_daily_runner_fetch_hard_fail_produces_failed_unreachable` | Inject SSL error → property report verdict is `FAILED_UNREACHABLE`; 0 LLM calls; carry-forward attempted |
| `test_daily_runner_validated_rejects_trigger_next_tier` | Scraper returns 10 records, 6 rejected → scraper's next-tier method called |
| `test_daily_runner_updates_profile_stats` | After run, profile on disk has `stats.total_scrapes` incremented |
| `test_daily_runner_parks_after_3_unreachable` | Same property fails 3 runs consecutively → ends up in DLQ |
| `test_daily_runner_dlq_revive_schedules_retry` | Property parked yesterday + retry window hit → scheduler emits a `DLQ_REVIVE` task |
| `test_daily_runner_never_crashes_on_pathological_input` | Fuzz: malformed CSV rows, missing URLs, unicode names → run completes |
| `test_daily_runner_writes_events_jsonl` | Events file exists with ≥ one event per `EventKind` fired |
| `test_daily_runner_old_profile_json_loads_without_error` | Seed `config/profiles/` with one v1 profile → run loads it, produces v2 after |
| `test_daily_runner_emits_cost_ledger` | `cost_ledger.db` exists and has rows |
| `test_daily_runner_report_has_slo_section` | `report.md` has `## SLO status` section with at least one row |

---

## Refactoring / code-quality checklist

- [ ] `scripts/daily_runner.py` drops below 800 lines (current is
      ~1,300; many helpers move to `ma_poc/discovery/` and
      `ma_poc/pms/`).
- [ ] No Jugnu layer is imported from `daily_runner.py` other than
      through a 1-line top-level import.
- [ ] Per-property loop body is ≤ 60 lines. If it's longer, extract
      a `_process_property(task, profile) -> PropertyResult` helper.
- [ ] `try/except` around every layer call — the never-fail
      invariant is visible.
- [ ] `ruff` + `mypy --strict` clean on `scripts/daily_runner.py`.
- [ ] No print statements left in the hot path (logs only).
- [ ] The existing `--limit`, `--start-at`, `--proxy`, `--run-date`,
      `--csv` CLI flags all still work.

---

## Gate — `scripts/gate_jugnu.py phase 8`

Passes iff:

- All 12 integration tests pass.
- Static analysis on `scripts/daily_runner.py` clean.
- **Observable check:** `python scripts/daily_runner.py --limit 3`
  runs end-to-end against the real property CSV without raising.
- **Observable check:** output properties.json has records with
  schema-identical top-level keys to the J0 baseline
  (`TARGET_PROPERTY_FIELDS`). No new top-level keys, no missing
  keys. `_meta` is the only addition and it lives one level deep.
- **Observable check:** `--limit 20` run has:
    - `report.md` with PMS table, failure signatures, cost summary,
      SLO status
    - Every property has a report file
    - Events ledger exists and is parseable
    - Cost ledger SQLite exists and queries cleanly
- **Observable check:** forcing property 5317's scenario (inject
  SSL error for that cid) produces:
    - verdict FAILED_UNREACHABLE
    - 0 LLM transcripts
    - carry-forward record if prior data existed, else empty record
      tagged FAILED_UNREACHABLE (not FAILED)
- **Observable check:** LLM cost summed across all properties for
  the --limit 20 run is ≤ 10% of the J0 baseline per-property LLM
  cost × 20 properties.

---

## Outbound handoff (to J9 Gate)

- **Entry point** `scripts/daily_runner.py` end-to-end through
  Jugnu.
- **Disk layout** fully populated per run date:
    - `data/runs/<date>/properties.json`
    - `data/runs/<date>/report.md` + `report.json`
    - `data/runs/<date>/reports/<cid>.md` (per property)
    - `data/runs/<date>/events.jsonl`
    - `data/runs/<date>/cost_ledger.db`
    - `data/runs/<date>/raw_api/<cid>.json` (unchanged — debug aid)
    - `data/raw_html/<date>/<cid>.html.gz` (for replay)
- **State layout** (persistent):
    - `data/state/property_index.json` (unchanged)
    - `data/state/unit_index.json` (unchanged)
    - `data/state/frontier.sqlite` (new, J2)
    - `data/state/dlq.jsonl` (new, J2)
    - `data/cache/conditional.sqlite` (new, J1)
- **Cost delta vs J0** recorded for J9's gate to verify.

Commit: `Jugnu J8: daily_runner integration — full pipeline`.

---

*Next: `PHASE_J9_GATE.md`.*
