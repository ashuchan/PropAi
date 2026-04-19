# Phase J2 — Layer 2 Discovery & Scheduling

> Decides *which URLs, in what order, on what schedule*. Consumes
> sitemaps, maintains a persistent frontier, routes known-broken
> properties into the DLQ, and fires the carry-forward safety net that
> is currently broken.
>
> Reference: Jugnu architecture §4.2 L2, §3.2 (Discovery gaps), §7.1 J2
> gate.

The 04-17 run had 121 scrape failures and **zero** of them carried
forward. J2 fixes the safety net and adds sitemap + DLQ + persistent
frontier.

---

## Inbound handoff (from J1)

From J1 you must have:

- `ma_poc.fetch.fetch(task: CrawlTask) -> FetchResult` — stable entry.
- Contract types `FetchResult`, `FetchOutcome`, `RenderMode` exported
  from `ma_poc.fetch.contracts`.
- The `fetch.*` event names emitted (even via stub logging).
- Raw HTML written to `data/raw_html/<date>/`.

The J1 observable check was on the limit-50 run via a shim. J2
replaces that shim.

---

## What J2 delivers

```python
# ma_poc/discovery/__init__.py — public API
def build_tasks_for_run(
    csv_rows: list[dict],
    profile_store: ProfileStore,
    state_store: StateStore,
) -> Iterator[CrawlTask]: ...

def record_task_outcome(
    task: CrawlTask,
    fetch_result: FetchResult,
    state_store: StateStore,
) -> None: ...
```

`build_tasks_for_run` is the scheduler. `record_task_outcome` is
called after the full scrape for a property finishes — it updates the
DLQ and the frontier based on the final L4 validation result, not the
raw fetch.

---

## Module breakdown

```
ma_poc/discovery/
├── __init__.py
├── contracts.py          # CrawlTask, TaskReason — see JUGNU_CONTRACTS §2
├── sitemap.py            # Sitemap.xml consumer with ETag cache
├── frontier.py           # Persistent URL frontier across runs
├── scheduler.py          # Turns CSV + profiles into a CrawlTask stream
├── change_detector.py    # Decides HEAD vs GET vs RENDER for a URL
├── dlq.py                # Dead-letter queue — parked properties
└── carry_forward.py      # Safety net: re-emit yesterday's data on failure
```

### Responsibilities

| Module | Owns |
|---|---|
| `contracts.py` | `CrawlTask`, `TaskReason` shapes |
| `sitemap.py` | Fetching & parsing `sitemap.xml` per host, ETag-cached |
| `frontier.py` | SQLite-backed store of URLs to visit, visited set, per-URL state |
| `scheduler.py` | Assembles `CrawlTask`s from inputs + frontier state |
| `change_detector.py` | Chooses `RenderMode.HEAD` / `GET` / `RENDER` based on profile maturity, time-since-render, and sitemap freshness |
| `dlq.py` | Reads/writes a JSONL of parked properties with retry schedule |
| `carry_forward.py` | On fetch failure, re-emit the property record from the previous run's state |

---

## File creation order

1. `ma_poc/discovery/__init__.py` (empty)
2. `ma_poc/discovery/contracts.py`
3. `ma_poc/discovery/frontier.py`
4. `ma_poc/discovery/sitemap.py`
5. `ma_poc/discovery/change_detector.py`
6. `ma_poc/discovery/dlq.py`
7. `ma_poc/discovery/carry_forward.py`
8. `ma_poc/discovery/scheduler.py`
9. Populate `ma_poc/discovery/__init__.py`

Tests under `tests/discovery/` mirror the layout.

---

## Module specifications

### 3. `frontier.py`

SQLite-backed to survive crashes and process restarts. Single table:

```
CREATE TABLE frontier (
    url TEXT PRIMARY KEY,
    host TEXT NOT NULL,
    property_id TEXT NOT NULL,
    first_seen TEXT NOT NULL,      -- ISO timestamp
    last_attempted TEXT,
    last_outcome TEXT,             -- FetchOutcome.value
    consecutive_failures INTEGER DEFAULT 0,
    is_parked INTEGER DEFAULT 0,   -- boolean; mirrors DLQ for fast query
    depth INTEGER DEFAULT 0,       -- 0 = entry URL, 1 = link from entry
    source TEXT NOT NULL           -- 'csv' | 'sitemap' | 'link_exploration'
);
CREATE INDEX frontier_property ON frontier(property_id);
CREATE INDEX frontier_host ON frontier(host);
```

```python
class Frontier:
    def __init__(self, db_path: Path) -> None: ...
    def upsert_url(self, url: str, property_id: str, depth: int, source: str) -> None: ...
    def mark_attempt(self, url: str, outcome: FetchOutcome) -> None: ...
    def park(self, property_id: str) -> None: ...
    def unpark(self, property_id: str) -> None: ...
    def property_urls(self, property_id: str) -> list[dict]: ...
    def by_host(self, host: str) -> list[dict]: ...   # for rate-limit planning
```

**Named tests (8):**

- `test_frontier_upsert_idempotent`
- `test_frontier_mark_attempt_increments_failures`
- `test_frontier_success_resets_failures`
- `test_frontier_park_unpark_round_trip`
- `test_frontier_by_host_groups_correctly`
- `test_frontier_db_survives_reopen` (create, close, reopen, read)
- `test_frontier_handles_unicode_urls`
- `test_frontier_no_sqlite_injection` (url contains `'; DROP TABLE`)

### 4. `sitemap.py`

```python
class SitemapConsumer:
    def __init__(self, fetcher, cond_cache: ConditionalCache) -> None: ...
    async def fetch(self, host: str) -> list[SitemapEntry]:
        """Returns list of (url, lastmod, priority). Empty if no sitemap
        or 304-not-modified since last fetch."""

@dataclass(frozen=True)
class SitemapEntry:
    url: str
    lastmod: datetime | None
    priority: float | None
```

Uses `xml.etree.ElementTree` — no external parser dependency. Handles
both flat `sitemap.xml` and sitemap-index variants. Follows sitemap
index up to 10 child files (hard cap — no runaway).

**Named tests (6):**

- `test_sitemap_parses_flat_xml`
- `test_sitemap_follows_index`
- `test_sitemap_caps_child_files_at_10`
- `test_sitemap_returns_empty_on_404`
- `test_sitemap_caches_via_etag` (second call to same host hits cache)
- `test_sitemap_handles_malformed_xml_gracefully`

### 5. `change_detector.py`

Decides what `RenderMode` to use for a task. Pure function of inputs.

```python
@dataclass(frozen=True)
class ChangeDecision:
    render_mode: RenderMode
    reason: str                   # human-readable
    use_cond_headers: bool        # send If-None-Match etc.

def decide(
    profile: ScrapeProfile | None,
    frontier_entry: dict | None,
    sitemap_lastmod: datetime | None,
    days_since_full_render: int | None,
    force_full: bool = False,
) -> ChangeDecision: ...
```

Decision rules (apply in order, first match wins):

1. `force_full=True` → `RENDER`, reason "manual_force"
2. `days_since_full_render is None or > 7` → `RENDER`, "stale_render_7d"
3. profile is `HOT` and `days_since_full_render < 1` →
   `HEAD`, "hot_profile_fresh"
4. sitemap_lastmod is older than the last successful scrape →
   `HEAD`, "sitemap_unchanged"
5. profile is `WARM` and `days_since_full_render < 3` →
   `GET`, "warm_profile_static"
6. default → `RENDER`, "default_render"

The target from the reference report (§5 of architecture doc) is
**~45% of daily properties skipped via HEAD or sitemap** — this is the
lever that drops the LLM and proxy cost in aggregate.

**Named tests (7):**

- `test_change_force_full_always_renders`
- `test_change_stale_render_after_7_days`
- `test_change_hot_profile_fresh_is_head`
- `test_change_sitemap_unchanged_is_head`
- `test_change_warm_profile_is_get`
- `test_change_cold_profile_always_renders`
- `test_change_decision_is_pure` (same inputs → same outputs, no side effects)

### 6. `dlq.py`

JSONL file at `data/state/dlq.jsonl`. Append-only, compacted nightly.

```python
@dataclass(frozen=True)
class DlqEntry:
    property_id: str
    parked_at: datetime
    reason: str                  # machine-readable code
    last_error_signature: str
    retry_at: datetime           # next scheduled retry

class Dlq:
    def __init__(self, path: Path) -> None: ...
    def park(self, property_id: str, reason: str, err_sig: str) -> None:
        """Schedule hourly retries for first 6h, then daily."""
    def is_parked(self, property_id: str) -> bool: ...
    def due_for_retry(self, now: datetime) -> list[DlqEntry]: ...
    def unpark(self, property_id: str) -> None: ...
    def compact(self) -> None:
        """Collapse multi-line-per-property to latest entry."""
```

**Parking rule** (architecture doc §6.2): a property goes to DLQ when
`consecutive_unreachable >= 3` — not consecutive failures. The
distinction matters: a parse failure does not park; a fetch failure
does.

**Named tests (7):**

- `test_dlq_park_and_query`
- `test_dlq_due_for_retry_hourly_for_6h`
- `test_dlq_due_for_retry_daily_after_6h`
- `test_dlq_unpark_removes_from_due`
- `test_dlq_compact_keeps_only_latest_per_id`
- `test_dlq_file_survives_crash_mid_append` (truncate mid-line, reopen)
- `test_dlq_is_parked_returns_false_for_unparked_id`

### 7. `carry_forward.py`

The currently-broken safety net. On fetch failure **or** on L4
"rejected >50%" signal, re-emit the property's previous successful
record into today's output with a `SCRAPE_OUTCOME: CARRY_FORWARD` tag.

```python
def carry_forward_property(
    property_id: str,
    today_run_dir: Path,
    state_store: StateStore,
    reason: str,
) -> dict | None:
    """Returns the carried record, or None if no prior record exists.

    Writes:
      - appends a 'CARRIED_FORWARD' row to today's properties.json
      - emits event 'discovery.carry_forward_applied'
      - does NOT touch the unit_index (those units are marked
        carry_forward in the daily diff, handled by state_store).
    """
```

Diagnose why it didn't fire in the 04-17 run **before** writing new
logic:

1. Read `ma_poc/scripts/daily_runner.py` — find the carry-forward path.
2. Read `ma_poc/scripts/state_store.py` — find where the previous
   record is looked up.
3. Document the root cause in a comment in `carry_forward.py`.
4. Fix it.

Likely suspect (per architecture doc §3.4): carry-forward is only
called when the scrape raises an exception, but today the scraper
catches the exception internally and returns a result with
`extraction_tier_used == "FAILED"`. The new module must trigger
on *any* failure outcome, not only exceptions.

**Named tests (6):**

- `test_carry_forward_returns_none_when_no_prior_record`
- `test_carry_forward_copies_prior_record_with_tag`
- `test_carry_forward_marks_scrape_outcome_code`
- `test_carry_forward_preserves_unit_identities`
- `test_carry_forward_does_not_fire_when_current_scrape_ok`
- `test_carry_forward_fires_on_fetch_hard_fail` (property 5317 scenario)

### 8. `scheduler.py`

The assembly. Reads CSV + profiles + DLQ + frontier + sitemaps, yields
a stream of `CrawlTask`s in priority order.

```python
class Scheduler:
    def __init__(
        self,
        frontier: Frontier,
        dlq: Dlq,
        sitemap: SitemapConsumer,
        profile_store: ProfileStore,
        change_detector_fn: Callable[..., ChangeDecision],
    ) -> None: ...

    async def build_tasks(
        self,
        csv_rows: list[dict],
        run_date: date,
    ) -> AsyncIterator[CrawlTask]: ...
```

Priority order:

1. `DLQ_REVIVE` tasks whose retry window is now.
2. Scheduled `RENDER` tasks (COLD and WARM profiles, new properties).
3. Scheduled `HEAD`/`GET` tasks (HOT profiles, recent renders).
4. `SITEMAP_DISCOVERED` URLs that point to known properties.

Within a priority bucket, **shuffle by host** to avoid hammering a
single CDN.

**Named tests (8):**

- `test_scheduler_emits_one_task_per_csv_row`
- `test_scheduler_skips_parked_properties`
- `test_scheduler_emits_dlq_revive_for_due_properties`
- `test_scheduler_respects_change_detector_decision` (mock returns HEAD → task.render_mode==HEAD)
- `test_scheduler_prioritises_dlq_revive_over_scheduled`
- `test_scheduler_shuffles_within_priority_by_host`
- `test_scheduler_populates_etag_from_frontier` (when known)
- `test_scheduler_marks_reason_correctly` (scheduled vs retry vs dlq revive)

---

## Event emission (for J5)

New event names L2 emits:

- `discovery.task_enqueued` — per task built
- `discovery.task_skipped_dlq` — when a property is skipped because parked
- `discovery.sitemap_fetched` — with host, url_count, new_urls
- `discovery.carry_forward_applied` — with property_id, reason
- `discovery.dlq_parked` — with property_id, reason
- `discovery.dlq_unparked` — with property_id

---

## Refactoring / code-quality checklist

- [ ] No file over 400 lines.
- [ ] `mypy --strict ma_poc/discovery/` clean.
- [ ] `ruff check ma_poc/discovery/` clean.
- [ ] Test coverage ≥ 85% on `ma_poc/discovery/`.
- [ ] SQLite access uses context managers everywhere (no leaked cursors).
- [ ] `scheduler.build_tasks()` is async-iterable (caller uses
      `async for task in scheduler.build_tasks(...)`).
- [ ] No module imports from `ma_poc.pms` — discovery knows nothing
      about PMSs, only URLs and profiles.

---

## Gate — `scripts/gate_jugnu.py phase 2`

Passes iff:

- All module-level tests pass (~55 tests across 7 modules).
- `ruff` + `mypy --strict` clean on `ma_poc/discovery/`.
- Coverage ≥ 85% on `ma_poc/discovery/`.
- **Observable check:** a deliberately forced failure (e.g. inject
  `ERR_SSL_PROTOCOL_ERROR` for one property) produces:
    - a `CARRIED_FORWARD` row in `properties.json`, **not** a missing
      property
    - an event `discovery.carry_forward_applied` in the event log
      (or stub log)
    - no `NO_CARRY_FORWARD` validation issue
- **Observable check:** on a limit-50 run, at least one property's
  `render_mode` is `HEAD` (i.e. change detection is saving work).
- **Baseline delta:** the timeout rate drops further vs J1 because
  the scheduler respects the change detector's HEAD/GET decisions.

### Injection test for carry-forward

Add a pytest fixture / a CLI flag `--fail-property <cid>` to
`scripts/daily_runner.py` that forces a hard fetch failure for one
property. Used only in the J2 gate observable check; removed before
J8 integration.

---

## Outbound handoff (to J3 Extraction)

- **Module** `ma_poc.discovery` with `build_tasks_for_run` and
  `record_task_outcome` as the only public entry points.
- **Storage** `data/state/frontier.sqlite` — persistent URL state.
- **Storage** `data/state/dlq.jsonl` — parked properties.
- **CrawlTask** now populated with `etag`, `last_modified`,
  `render_mode`, `reason` — adapters in J3 never set these
  themselves.
- **Event names** `discovery.*` fixed.
- **Fix log:** `docs/JUGNU_FIXES.md` documents the root cause of the
  04-17 zero-carry-forward bug.

Commit: `Jugnu J2: discovery, scheduler, DLQ, carry-forward`.

---

*Next: `PHASE_J3_EXTRACTION.md`.*
