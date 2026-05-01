# PropAi — MA Rent Intelligence Platform: Authoritative Architecture Context

> **AUTHORITY NOTICE**
> This document is derived from direct inspection of the live codebase at `github.com/ashuchan/PropAi`.
> It overrides all prior memory, BRD documents, spec docs, and general knowledge about this project.
> When this document and any other source conflict, this document wins.
> Last synced: April 2026.

---

## 1. What This System Does

Scrapes multifamily apartment property websites at scale, extracts structured unit-level rent and availability data, and stores it for downstream analytics. Phase 1 POC targets 5,000 properties across major US markets.

**Container entrypoint (production):** `ma_poc/scripts/jugnu_shard_entry.py` — slices the CSV and invokes `jugnu_runner.py` per shard
**Runner (called by shard entry):** `ma_poc/scripts/jugnu_runner.py` — per-shard property loop
**Primary scrape orchestrator:** `ma_poc/pms/scraper.py::scrape_jugnu()` and `scrape()`
**Output (per shard):** `/tmp/data/v2/runs/{date}/properties.json` → GCS `runs/{date}/shard_{idx}/` → Cloud SQL via PG sync

---

## 2. Infrastructure — GCP, Not Azure

> This is frequently wrong in older spec docs. The deployed infra is GCP.

| Resource | GCP Service | Purpose |
|---|---|---|
| Compute | **Cloud Run Jobs** (`google_cloud_run_v2_job`) | Sharded scrape job + single-task retry job |
| Database | **Cloud SQL** (PostgreSQL, psycopg v3) | Persistent structured storage; IAM auth, private IP |
| File storage | **Cloud Storage** (GCS bucket) | Property CSV, per-shard run artifacts, profiles |
| Scheduling | **Cloud Scheduler** | Triggers `jugnu-scrape-{env}` daily; retry job paused by default |
| Secrets | **Secret Manager** | `OPENROUTER_API_KEY`, `ANTHROPIC_API_KEY`, `PROXY_POOL_URLS` |
| Container registry | **Artifact Registry** | Docker image `{repo}/jugnu:{tag}` |
| Networking | VPC + VPC Connector | Cloud SQL private IP; egress `PRIVATE_RANGES_ONLY` |

**IaC:** Terraform under `infra/` — `artifact_registry`, `iam`, `storage`, `cloud_sql`, `secrets`, `cloud_run_jobs`, `scheduler`.

---

## 2a. Multi-Shard Deployment — How the Scrape Job Parallelises

This is the critical deployment detail. There are **two separate Cloud Run Jobs** and a dedicated shard entry point. `jugnu_runner.py` is never the container entrypoint directly.

### Two Cloud Run Jobs

**`jugnu-scrape-{env}`** — the daily scrape job
- `parallelism = var.default_task_count` — all tasks run concurrently
- `task_count = var.default_task_count` — total task instances per execution
- `timeout = 14400s` (4 hours hard ceiling)
- `max_retries = 1`
- Container entrypoint: `python ma_poc/scripts/jugnu_shard_entry.py`

**`jugnu-retry-{env}`** — the retry job
- `parallelism = 1`, `task_count = 1` — always single task, no sharding
- `timeout = 3600s` (1 hour)
- `max_retries = 0` — no Cloud Run-level retry on the retry job itself
- Container entrypoint: `python ma_poc/scripts/jugnu_retry_entry.py`
- **Currently paused** — Cloud Scheduler trigger disabled by default; manual execution only

### Property List Sharding — `jugnu_shard_entry.py`

This is the actual container entrypoint for the scrape job. It handles sharding before `jugnu_runner.py` ever runs.

**Environment variables consumed (auto-injected by Cloud Run):**

| Variable | Source | Purpose |
|---|---|---|
| `CLOUD_RUN_TASK_INDEX` | Auto-injected by Cloud Run | This task's 0-based index (0 to task_count-1) |
| `CLOUD_RUN_TASK_COUNT` | Auto-injected by Cloud Run | Total tasks in this execution |
| `CSV_GCS_URI` | Terraform env | `gs://{bucket}/property-list/properties.csv` |
| `BUCKET_NAME` | Terraform env | GCS bucket for artifact upload |
| `RUN_DATE` | Optional | YYYY-MM-DD; defaults to UTC today |
| `LIMIT` | Optional | Cap properties per shard (smoke tests) |
| `SCHEMA_VERSION` | Terraform env | `v2` in production |
| `BROWSERS_PER_TASK` | Terraform env | Controls `AsyncPool` size inside the runner |
| `DATABASE_URL` | Terraform env | `postgresql+psycopg://{worker_sa}@{sql_private_ip}:5432/jugnu` |
| `CLOUD_SQL_INSTANCE` | Terraform env | Cloud SQL connector instance connection name |
| `LLM_PROVIDER` | Terraform env | `anthropic` \| `openrouter` \| `azure` |
| `OPENROUTER_MODEL` / `ANTHROPIC_MODEL` | Terraform env | Text model IDs |
| `OPENROUTER_VISION_MODEL` / `ANTHROPIC_VISION_MODEL` | Terraform env | Vision model IDs |
| `OPENROUTER_API_KEY` / `ANTHROPIC_API_KEY` | Secret Manager | LLM API credentials |
| `PROXY_POOL_URLS` | Secret Manager | Comma-separated proxy URLs with embedded creds |
| `SMOKE_MODE=proxy_check` | Optional override | Short-circuits to `check_proxy.py` for proxy health verification |

**Shard slicing algorithm (ceiling division):**
```python
shard_size = math.ceil(total_rows / task_count)
start = task_idx * shard_size
end   = min(start + shard_size, total_rows)
```

Example: 500 properties, 5 tasks → each shard gets 100 rows (task 0: rows 0-99, task 1: rows 100-199, …). Last shard may be smaller if `total % task_count != 0`.

Shard CSV is written to `/tmp/shard_{task_idx}.csv` and passed to `jugnu_runner.py --csv`.

### Per-Shard Execution Flow

```
Cloud Run Job execution starts (N tasks in parallel)
  │
  ├─ Each task runs jugnu_shard_entry.py independently:
  │
  │  1. Download full CSV from GCS → /tmp/properties.csv
  │  2. Slice rows for CLOUD_RUN_TASK_INDEX → /tmp/shard_{idx}.csv
  │  3. subprocess.run(jugnu_runner.py --csv /tmp/shard_{idx}.csv ...)
  │       └─ runner writes output to /tmp/data/v2/runs/{date}/
  │  4. _sync_to_postgres()  ← always runs, even if runner_exit != 0
  │       └─ sync_run_to_postgres(shard_id=str(task_idx))
  │       └─ fast-returns 0 if properties.json missing (runner crashed)
  │  5. _upload_artifacts()  ← always runs (finally block)
  │       └─ entire /tmp/data/v2/runs/{date}/ → gs://{bucket}/runs/{date}/shard_{idx}/
  │       └─ dlq.jsonl → gs://{bucket}/runs/{date}/shard_{idx}/dlq.jsonl
  │
  │  Exit code: max(runner_exit, sync_exit)
  │    runner exits 1 if ANY property failed (normal for 500-property shards)
  │    sync_exit 1 if PG sync failed (surfaced to Cloud Run for retry)
```

### Critical Invariant — Sync Always Runs

The PG sync is **not gated on `runner_exit == 0`**. This is intentional and documented in the code:

> "The runner exits 1 whenever any property fails (common with 500-property shards); gating sync on that turned every partial run into a zero-rows-in-DB deploy."

`_sync_to_postgres()` fast-returns 0 only when `properties.json` doesn't exist (runner crashed before writing output). A partial run (e.g. 140/499 succeeded) still syncs its 140 rows.

### Intra-Task Concurrency

Within each shard task, `jugnu_runner.py` uses `AsyncPool` backed by `SystemResources.detect()`:

```
pool_size = min(RAM/250MB × 0.7, cpu×2, BROWSERS_PER_TASK env var)
            clamped to [1, 32]
```

This is **within-task** browser-level concurrency — separate from the across-task shard parallelism. Total concurrent browsers across the whole execution = `task_count × pool_size`.

### Artifact Layout in GCS

Each shard writes to its own prefix. No cross-shard file collision:

```
gs://{bucket}/
  property-list/
    properties.csv                    ← input (full list, all shards read this)
  runs/{date}/
    shard_0/
      properties.json                 ← this shard's property records
      report.json / report.md
      property_reports/*.md
      llm_report.json
      llm_reports/*.json
      llm_diagnostics/*.json
      events.jsonl
      cost_ledger.db
      dlq.jsonl                       ← cross-run state, uploaded per shard
    shard_1/
      ...
    shard_N/
      ...
```

### Output Directory Layout (local, inside container)

`jugnu_runner._resolve_data_dirs()` roots output under schema version:

```
/tmp/data/                            ← --data-dir
  v2/                                 ← SCHEMA_VERSION=v2 injects this prefix
    runs/{date}/                      ← run_dir (properties.json written here)
    state/                            ← frontier.sqlite, dlq.jsonl
    cache/                            ← conditional.sqlite (ETag cache)
```

`jugnu_shard_entry._resolve_run_dir()` mirrors this logic exactly and **must stay in sync** with `jugnu_runner._resolve_data_dirs()`. A drift between the two breaks the GCS upload and PG sync.

### Database Connection

- **Dialect:** `postgresql+psycopg://` — uses psycopg v3 (not psycopg2; only v3 is in `requirements.txt`)
- **Auth:** Cloud SQL IAM authentication via the Cloud SQL Python Connector. Worker SA OAuth token is used as the Postgres password. Password auth is disabled (`cloudsql.iam_authentication=on`).
- **Network:** Private IP via VPC Connector. Egress is `PRIVATE_RANGES_ONLY`.
- **Schema version:** Alembic migration `0002_v2_strict` — the Postgres schema is v2. Worker SA does not have `CREATE` on `schema public` — alembic owns DDL. `create_schema=False` is passed for Postgres targets.

---

## 2b. Multi-Shard DB Write Concurrency (`sync_run_to_pg.py`)

All `N` shard tasks call `sync_run_to_postgres()` concurrently against the same Cloud SQL instance. The sync is designed to be fully concurrent-safe. Here is exactly how each table handles it.

### Two-Stage Transaction Model

**Stage 1 — Single shared SQLAlchemy session, one `dst.transaction()` block:**

Everything that goes through the store interface runs inside one session transaction per shard. If any write in Stage 1 fails, the whole stage rolls back — the DB is left in its pre-sync state for that shard.

**Stage 2 — Raw `engine.begin()` connections, each managing their own transaction:**

The aggregate-merge tables (`run_reports`, `llm_reports`) run outside Stage 1 because they use `SELECT ... FOR UPDATE` to serialise concurrent shards. Nesting `engine.begin()` inside a SQLAlchemy session causes deadlocks on SQLite (tests) and is unsupported on Postgres. Stage 2 runs sequentially after Stage 1 commits.

### Per-Table Concurrency Strategy

| Table | Write pattern | Multi-shard safety mechanism |
|---|---|---|
| `properties` | `upsert` keyed on `canonical_id` | Safe: CSV slices are disjoint by property — no two shards write the same `canonical_id` |
| `units` | `upsert_units` keyed on `(canonical_id, unit_key)` | Safe: same disjoint-by-property guarantee |
| `scrape_profiles` | `put` (upsert) keyed on `canonical_id` | Safe: disjoint by property |
| `scrape_events` | upsert keyed on `event_id = sha256(canonical_id \| run_date)` | Stable hash → shard retries upsert in place, no duplicates |
| `extraction_results` | upsert keyed on `(run_date, property_id)` | Disjoint by property |
| `property_snapshots` | Delete-then-insert scoped to **this batch's `canonical_ids`** only | `write_properties` deletes only the cids in this shard's `properties.json`, never `WHERE run_date=X` — other shards' rows untouched |
| `run_issues` | Delete-then-append scoped to **this batch's `canonical_ids`** | `_delete_run_rows_in_session` deletes only this shard's cids before re-appending — uses shared session so delete + insert commit atomically |
| `run_ledger` | Delete-then-append scoped to **this batch's `canonical_ids`** | Same as `run_issues` |
| `llm_property_details` | `on_conflict_do_update` keyed on `(run_date, property_id)` | Disjoint by property |
| `property_reports` | `on_conflict_do_update` keyed on `(run_date, canonical_id)` | Disjoint by property |
| `llm_diagnostics` | `on_conflict_do_update` keyed on `(run_date, canonical_id, kind)` | Disjoint by property |
| `run_reports` | `SELECT ... FOR UPDATE` + merge into `extra.shards[shard_id]` | Serialised at the DB row level — see below |
| `llm_reports` | `SELECT ... FOR UPDATE` + merge into `payload.shards[shard_id]` | Serialised at the DB row level — see below |
| `runs` (registry) | `INSERT ... ON CONFLICT DO NOTHING` | First shard creates the row; subsequent shards are no-ops |
| `dlq_entries` | `on_conflict_do_update` keyed on `property_id` | DLQ is global cross-run state; last-writer-wins is acceptable (single source of truth is the JSONL file) |

### The `SELECT ... FOR UPDATE` Merge Pattern

`run_reports` and `llm_reports` are single-row-per-`run_date` aggregate tables. All shards write to the **same row**. The concurrency protocol is:

```
1. INSERT stub row with empty shards dict
   ON CONFLICT DO NOTHING          ← first shard creates it; others skip
2. SELECT ... FOR UPDATE           ← row-level lock; serialises all concurrent shards
3. Read existing extra.shards dict
4. Write this shard's data under shards[shard_id]
5. Recompute top-level aggregates as sum/merge across ALL shards seen so far
6. UPDATE the row with new shards dict + recomputed aggregates
7. COMMIT                          ← releases FOR UPDATE lock
```

This means at any point during the run, the `run_reports` row reflects the correct aggregate over however many shards have completed so far. The last shard to commit produces the final correct totals.

**Shard retries are safe:** A shard that fails and retries simply overwrites `shards[shard_id]` with its updated data and the aggregate is recomputed correctly.

**`FOR UPDATE` on SQLite:** SQLite ignores `FOR UPDATE` but serialises all writes via its database-level write lock — the merge is still correct in tests.

### Aggregate Recomputation Logic

For `run_reports`, top-level fields are recomputed from `extra.shards` on every write:

| Field | Merge strategy |
|---|---|
| `totals.properties` | Sum across shards |
| `totals.succeeded` | Sum across shards |
| `totals.failed` | Sum across shards |
| `totals.carry_forward` | Sum across shards |
| `totals.success_rate_pct` | Recomputed: `succeeded / properties × 100` |
| `tier_distribution` | Per-key sum across shards |
| `cost` | Per-key float sum across shards |
| `slo_violations` | Concatenated, deduplicated by JSON key |

For `llm_reports`, top-level fields recomputed similarly:

| Field | Merge strategy |
|---|---|
| `calls` | Sum across shards |
| `total_cost_usd` | Sum across shards |
| `total_tokens_in` | Sum across shards |
| `total_tokens_out` | Sum across shards |
| `by_property` | Shallow merge (dict update) — safe because `by_property` keys are `canonical_id`s, which are disjoint across shards |

### The Fundamental Safety Guarantee

The entire concurrency model rests on one invariant: **the CSV slicing in `jugnu_shard_entry._slice_csv()` produces disjoint property sets across shards.** Each `canonical_id` appears in exactly one shard's `properties.json`. This makes every property-keyed write inherently non-conflicting. The only tables that need special serialisation (`run_reports`, `llm_reports`) are the run-level aggregates that intentionally span all shards.

### Known Concurrency Gap — DLQ

`dlq.jsonl` is cross-run global state. Each shard uploads its own copy of `dlq.jsonl` to `gs://{bucket}/runs/{date}/shard_{idx}/dlq.jsonl`. The sync writes DLQ entries to `dlq_entries` with last-writer-wins semantics. This is correct for the current use case (the DLQ is written by `jugnu_runner` during the scrape, not during sync), but if two shards both attempted to park or unpark the same property in the same run, the last sync to complete would win. In practice this cannot happen because a property is processed by exactly one shard — but it is an undocumented assumption that breaks if the CSV slice invariant is ever violated (e.g. duplicate rows in the input CSV).

### LLM Provider Configuration

The LLM provider is **runtime-configurable** via `LLM_PROVIDER` env var. Both API keys are always mounted; only the matching one is used:

| `LLM_PROVIDER` value | Text provider | Vision provider |
|---|---|---|
| `openrouter` | `OPENROUTER_MODEL` via OpenRouter API | `OPENROUTER_VISION_MODEL` |
| `anthropic` | `ANTHROPIC_MODEL` via Anthropic API | `ANTHROPIC_VISION_MODEL` |
| `azure` | Direct Azure OpenAI httpx (rescue path only) | — |

Switching providers is a `tfvars` change only — no infra rewrite. `llm.factory.get_text_provider()` reads `LLM_PROVIDER` at runtime.

> **Note on older docs:** BRD and earlier spec docs reference Azure OpenAI as the primary provider. This is outdated. The production Terraform configures `LLM_PROVIDER` as `openrouter` or `anthropic`. Azure is a fallback path inside `llm_api_rescue.py` only.

---

## 3. Five-Layer Pipeline Architecture

Each layer has frozen dataclass contracts. A failure in one layer returns a structured failure result — it never raises and never kills the run.

```
CSV row
  │
  ▼
[L2 Discovery]  scheduler.py → CrawlTask
  │
  ▼
[L1 Fetch]      fetcher.py → FetchResult
  │
  ▼
[L3 Extraction] scraper.py → scrape_jugnu() → AdapterResult → dict (46-key)
  │
  ▼
[L4 Validation] schema_gate.py + identity_fallback.py + cross_run_sanity.py → ValidatedRecords
  │
  ▼
[L5 Observability] events.py + report writers → ledger, markdown, JSON reports
```

---

## 4. Key Data Contracts

### 4.1 CrawlTask (L2 → L1)
```
property_id: str
url: str
reason: TaskReason       # SCHEDULED | DLQ_REVIVE | manual_force
render_mode: RenderMode  # RENDER | GET  (HEAD exists but is dead code)
priority: int            # 0=DLQ, 1=RENDER, 2=GET
budget_ms: int           # 180_000 standard; 15_000 sitemap
parent_task_id: None | str
```

### 4.2 FetchResult (L1 → L3)
```
url: str
outcome: FetchOutcome    # OK | SOFT_FAIL | HARD_FAIL | NOT_MODIFIED
status_code: int | None
headers: dict
body: bytes | str | None
elapsed_ms: int
render_mode: RenderMode
identity_key: str        # which stealth identity was used
proxy_label: str
error_signature: str | None  # "ERR_SSL_PROTOCOL_ERROR" etc.
network_log: list[dict]  # [{url, status, content_type, body_size, body}] — XHR captures
```

### 4.3 AdapterContext (L3 internal — scraper → adapter)
```
base_url: str
detected: DetectedPMS
profile: ScrapeProfile | None
expected_total_units: int | None
property_id: str
fetch_result: FetchResult | None
property_name: str
city: str
state: str
zip_code: str
pmc: str                  # management company
_api_responses: list[dict]  # parsed network_log entries, set dynamically
```

### 4.4 AdapterResult (adapter → scraper)
```
units: list[dict]
tier_used: str            # e.g. "TIER_1_API", "TIER_4_LLM_API"
winning_url: str | None
api_responses: list[dict]
blocked_endpoints: list[tuple[str, str]]
llm_field_mappings: list[dict]
errors: list[str]
confidence: float
```

### 4.5 UnitRecord (canonical output model — `ma_poc/models/unit_record.py`)
```python
unit_id: str | None          # Phase B entity resolution
property_id: str
unit_number: str
floor_plan_id: str | None
floor: int | None
building: str | None
sqft: int | None
floor_plan_type: str | None  # "1/1", "2/2", "Studio"
asking_rent: float | None
effective_rent: float | None    # Phase B only
concession: dict | None         # Phase B only
availability_status: AvailabilityStatus  # AVAILABLE | UNAVAILABLE | UNKNOWN
availability_date: date | None
days_on_market: int | None      # Phase B only
scrape_timestamp: datetime
extraction_tier: int | None
confidence_score: float          # 0.0–1.0
data_quality_flag: DataQualityFlag  # CLEAN | SMOOTHED | CARRIED_FORWARD | QA_HELD
source: str                      # "DIRECT_SITE"
carryforward_days: int
```

**Phase A populates:** `unit_id`, `unit_number`, `floor_plan_type`, `sqft`, `asking_rent`, `availability_status`, `availability_date`, `confidence_score`, `extraction_tier`.
**Phase B fills:** `effective_rent`, `concession`, `days_on_market`, `availability_periods`.

The v2 internal unit dict shape (used in `llm_api_rescue._normalize_units`) uses different field names:
```
beds, baths, area, rent_low, rent_high, unit_id, floor_plan_name, available_date, lease_term
```
These are mapped to UnitRecord fields during `jugnu_runner._format_v2_unit()`.

---

## 5. ScrapeProfile Model (`ma_poc/models/scrape_profile.py`)

Per-property self-learning profile. Stored at `config/profiles/{canonical_id}.json` (GCS in production).

```
canonical_id: str
version: int              # incremented on every save
schema_version: "v2"

navigation:
  entry_url: str | None
  availability_page_path: str | None
  winning_page_url: str | None   # exact URL that produced units last time
  requires_interaction: list[ExpanderAction]  # click-to-expand selectors
  timeout_ms: int
  availability_links: list[str]  # links that previously had data → prioritise
  explored_links: list[str]      # links tried with no data → skip (cap: 50)

api_hints:
  known_endpoints: list[ApiEndpoint]        # URLs that returned unit data
    └─ url_pattern, json_paths, provider
  widget_endpoints: list[str]               # Entrata widget URLs
  api_provider: str | None                  # "rentcafe" | "entrata" | "appfolio" etc.
  client_account_id: str | None
  wait_for_url_pattern: str | None
  blocked_endpoints: list[BlockedEndpoint]  # noise URLs → never retry (cap: 50)
    └─ url_pattern, reason, blocked_at, attempts
  llm_field_mappings: list[LlmFieldMapping] # LLM-learned JSON path replay (cap: 20)
    └─ api_url_pattern, json_paths, response_envelope, success_count

dom_hints:
  platform_detected: str | None
  field_selectors: FieldSelectorMap         # CSS selectors for DOM extraction
    └─ container, unit_id, rent, sqft, bedrooms, bathrooms, availability_status/date, floor_plan_name
  jsonld_present: bool
  availability_page_sections: list[str]

confidence:
  preferred_tier: int | None
  last_success_tier: int | None
  consecutive_successes: int
  consecutive_failures: int
  last_unit_count: int
  maturity: COLD | WARM | HOT
  consecutive_unreachable: int

llm_artifacts:
  extraction_prompt_hash: str | None
  field_mapping_notes: str | None
  api_schema_signature: str | None
  dom_structure_hash: str | None
  last_api_analysis_results: dict[str, str]  # url → "has_units"|"noise"

stats:
  total_scrapes, total_successes, total_failures
  total_llm_calls, total_llm_cost_usd
  last_tier_used, last_unit_count
  p50/p95_scrape_duration_ms
  consecutive_llm_rescue_failures: int  # gates F2 rescue (cap: 3 before skip)
```

**Maturity transitions** (in `profile_updater.py`):
- `consecutive_successes >= 3` → **HOT**
- `consecutive_successes >= 1` → **WARM**
- `consecutive_failures >= 3` → **COLD**

**ProfileStore** (`services/profile_store.py`): file-based JSON. `load()` returns `None` if not found. `save()` increments version, writes current + audit copy at `_audit/{id}_{version}.json`. In production, `base_dir` points to a GCS-mounted path.

---

## 6. L3 Extraction — How `scraper.py` Works

`scrape_jugnu()` is the Jugnu entry point (takes `CrawlTask + FetchResult`). It delegates to `scrape()`.

`scrape()` runs the following sequence. Every step has error handling and never raises:

```
Step 1: detect_pms(url, csv_row)                  # offline, URL + CSV signals
Step 2: page_html from fetch_result.body           # no re-fetch in Jugnu path
Step 3: Unreachable error check on page content
Step 4: re-detect with page HTML if higher confidence
Step 5: resolve_target()                           # CTA-hop / iframe / redirect resolution
         (skipped if page=None — fetch-only mode)
Step 6: get_adapter(pms_name)                      # registry lookup → PMS adapter or generic
Step 6b: confirm_detection()                       # body-shape check; demotes to "unknown" if
          no captured response matches the adapter's expected envelope
Step 7: adapter.extract(page, ctx)                 # PMS-specific or generic extraction
F2:     LLM rescue                                 # if adapter returns empty + has API responses
         (only for generic/entrata/appfolio; skips if consecutive_rescue_failures >= 3)
Step 8: generic fallback                           # if PMS adapter failed, try generic
Step 9: populate legacy result dict                # 46-key shape
```

After `scrape()` returns, `scrape_jugnu()` also runs:

```
Option B: _try_link_hop()  # if units=[] AND fetch_result has HTML
           → rank internal links by keyword score
           → fetch top 3 sub-URLs via L1
           → re-run scrape() on each (not recursive — scrape() not scrape_jugnu())
           → return first sub-result with units
           → also records explored/availability links for profile learning
```

### 6.1 PMS Detection (`ma_poc/pms/detector.py`)

Two-pass: (1) offline from URL + CSV, (2) re-run with page HTML; higher confidence wins.

`confirm_detection()` — post-capture body-shape check. If the URL-predicted adapter's envelope doesn't match any captured XHR body, demotes to `"unknown"` → generic adapter. This catches false positives (e.g. vanity domain that looks like RentCafe but isn't).

### 6.2 Adapter Registry (`ma_poc/pms/adapters/registry.py`)

`get_adapter(pms_name)` — never returns None. Falls back to `GenericAdapter`.

Known adapters: `rentcafe`, `entrata`, `appfolio`, `sightmap`, `generic` (+ others).

**PMS-specific adapters:** deterministic parsing, no LLM. Own their PMS quirks entirely.
**GenericAdapter:** runs the full tier cascade including LLM paths. Is the fallback for unknown/undetected PMS.

### 6.3 Extraction Tiers

| Tier label | Description |
|---|---|
| `TIER_1_API` | Known API endpoint from profile, deterministic JSON parse |
| `TIER_1_PROFILE_MAPPING` | LLM-learned field mapping replay (deterministic, no LLM) |
| `TIER_1_5_EMBEDDED` | Embedded JSON blobs (__NEXT_DATA__, __NUXT__, etc.) |
| `TIER_1_SIGHTMAP` | SightMap widget API |
| `TIER_1_WIDGET` | Entrata/other widget endpoints |
| `TIER_2_JSONLD` | JSON-LD schema.org extraction |
| `TIER_3_DOM` | CSS selector DOM parsing |
| `TIER_3_DOM_LLM` | DOM parsing with LLM assistance |
| `TIER_4_LLM` | Monolithic LLM extraction (HTML + top API responses) |
| `TIER_4_LLM_API` | Targeted per-API-response LLM analysis |
| `TIER_4_LLM_DOM` | Targeted per-DOM-section LLM analysis |
| `TIER_4_ENTRATA_API` | Entrata-specific API LLM path |
| `TIER_5_PORTAL` | Portal subdomain navigation |
| `TIER_5_5_EXPLORATORY` | Exploratory link following |
| `TIER_5_VISION` | Vision model screenshot extraction (last resort) |
| `TIER_1_API_LLM_RESCUE` | F2 rescue — generic adapter |
| `TIER_1_API_ENTRATA_LLM_RESCUE` | F2 rescue — entrata adapter |
| `TIER_1_API_APPFOLIO_LLM_RESCUE` | F2 rescue — appfolio adapter |

### 6.4 F2 LLM Rescue (`services/llm_api_rescue.py`)

Fires when: adapter returns empty units AND has captured API responses AND pms_name in `{generic, entrata, appfolio}` AND `consecutive_rescue_failures < 3`.

Logic:
1. Filter candidates — drop empty bodies, blocked endpoints, non-JSON
2. Rank candidates by `looks_like_availability_api(url)` + `response_looks_like_units(body)` + key overlap
3. Loop through top 3 candidates, call LLM per endpoint (max 2 LLM calls total)
4. On first result passing `property_passes_quality_gate()` → return immediately with field mappings
5. Failed endpoints → add to `blocked_endpoints`

**Hard caps:** `MAX_LLM_CALLS_PER_PROPERTY = 2`, `MAX_CANDIDATES = 3`, `MAX_BODY_ARRAY_ITEMS = 200`, `MAX_BODY_TOKENS = 12_000`.

---

## 7. L4 Validation (`ma_poc/validation/schema_gate.py`)

### `is_substantive(unit)` — single-unit check
Returns True if at least one of these fields is present and non-null:
`beds`, `rent_low`, `floor_plan_name`, `area` (v2 names)
or `bedrooms`, `asking_rent`, `market_rent_low`, `sqft`, `floor_plan_type` (v1 aliases)

**Important:** A unit with only `floor_plan_name` and no rent passes this check. This is a presence-of-any check, not a full-coverage check.

### `property_passes_quality_gate(units, threshold=0.5)` — batch check
Returns True when `>=threshold` fraction of units are substantive. Empty list always fails.

### `check(record)` — full record validation
- Rent: must be 0–50,000 if present
- Sqft: must be 0–20,000 if present
- Date: must parse as ISO-8601 if present
- Unit ID: if missing, tries `compute_fallback_id(record)` (SHA256-based composite); if fallback also fails → `IDENTITY_FALLBACK_INSUFFICIENT`

Returns `SchemaGateResult(accepted=dict | None, rejection_reasons=list, inferred_id=bool)`.

---

## 8. Profile Updater (`services/profile_updater.py`)

Called after every extraction. Updates profile based on what worked.

Key updates:
- **Streak tracking:** `consecutive_successes++` or `consecutive_failures++`
- **Maturity promotion/demotion**
- **Winning page URL** → `profile.navigation.winning_page_url`
- **API endpoints with data** → `profile.api_hints.known_endpoints`
- **Widget endpoints** → `profile.api_hints.widget_endpoints`
- **LLM field mappings** → `profile.api_hints.llm_field_mappings` (via `save_llm_field_mapping()`)
- **Blocked endpoints** → `profile.api_hints.blocked_endpoints` (via `update_profile_blocklist()`)
- **DOM CSS selectors** → `profile.dom_hints.field_selectors`
- **Explored links** (had data vs no data) → `profile.navigation.availability_links` or `explored_links`
- **LLM analysis results** (per-URL "has_units" or "noise" verdicts)

Helper functions that can be called independently:
- `update_profile_blocklist(profile, api_url, reason)` — adds/increments blocked endpoint
- `save_llm_field_mapping(profile, mapping_dict)` — upserts field mapping by url_pattern
- `update_rescue_counter(profile, rescue_succeeded)` — manages `consecutive_llm_rescue_failures`
- `record_explored_link(profile, link, had_data)` — routes to availability_links or explored_links

---

## 9. L2 Discovery — Scheduler Key Decisions

**Change detector decision (first match wins):**
1. `force_full=True` → RENDER
2. `days_since_full_render > 7` or never rendered → RENDER
3. `maturity=HOT AND days < 1` → GET (conditional headers)
4. `sitemap_lastmod < frontier.last_attempted` → GET
5. `maturity=WARM AND days < 3` → GET
6. Default → RENDER

**HEAD is dead code** — change_detector never produces it, fetcher's HEAD path yields `body=None` which fails extraction.

**DLQ:** `consecutive_failures >= 3` → parked. Retry cadence: hourly for 6h, then daily. DLQ parked properties are skipped at scheduler, not at fetch time.

---

## 10. L1 Fetch Key Behaviours

- **Never raises.** Returns `FetchResult` with failure outcome codes.
- **Stealth identity:** SHA256 hash of `property_id` → one of 8 identity slots. Same identity per property across runs.
- **Proxy health:** Start 1.0. Success +0.05 (cap 1.0). Failure -0.25 (floor 0.1). Quarantine at < 0.25.
- **Rate limiter:** Per-host token bucket, default 2 req/sec. Respects `Crawl-delay` from robots.txt.
- **Network capture body cap:** 256 KB per XHR response.
- **Render timeout:** `min(budget_ms, 20_000)` ms per attempt. 3 retry attempts.
- **Carry-forward branch:** If `fetch_result.outcome != OK` and yesterday's data exists → emit yesterday's record as `CARRY_FORWARD`. This stamps `_meta.verdict = "SUCCESS"` with `verdict_reason = "carry_forward_applied"` — the CARRY_FORWARD verdict string in `verdict.py` is never actually produced (known issue).

---

## 11. Key Thresholds Reference

| Concern | Value | Location |
|---|---|---|
| DLQ parking | `consecutive_failures >= 3` | `dlq.py`, `frontier.py` |
| HOT profile | `consecutive_successes >= 3` | `profile_updater.py:171` |
| WARM profile | `consecutive_successes >= 1` | `profile_updater.py:173` |
| COLD demotion | `consecutive_failures >= 3` | `profile_updater.py:175` |
| Quality gate (batch) | 50% substantive units | `schema_gate.py` |
| Max rent | $50,000 | `schema_gate.py:19` |
| Max sqft | 20,000 sqft | `schema_gate.py:20` |
| Rescue LLM cap | 2 calls per property | `llm_api_rescue.py:33` |
| Rescue candidates | top 3 | `llm_api_rescue.py:35` |
| Rescue skip after | 3 consecutive failures | `profile_updater.py` |
| LLM body cap | 12,000 tokens | `llm_api_rescue.py:34` |
| LLM array cap | 200 items | `llm_api_rescue.py:35` |
| Link-hop max | 3 sub-URLs | `scraper.py` (default `max_hops=3`) |
| Blocked endpoints cap | 50 per profile | `profile_updater.py` |
| LLM field mappings cap | 20 per profile | `profile_updater.py` |
| Explored links cap | 30 per profile | `profile_updater.py` |
| v2 area sanity | 150–10,000 sqft | `jugnu_runner._format_area` |
| Rent sanity (LLM) | $200–$50,000 | `llm_extractor.py:310-314` |
| Rent swing flag | >20% | `cross_run_sanity.py:54` |
| Rent swing high | >50% | `cross_run_sanity.py:52` |
| Sqft change flag | >5% | `cross_run_sanity.py:62` |
| Max concurrent browsers | `min(RAM/250MB × 0.7, cpu×2, env)` clamped [1, 32] | `concurrency.py` |
| Stealth identity pool | 8 identities | `stealth.py` |
| Network body cap | 256 KB | `fetcher.py:384` |
| Null-field-recovery | ≤5 units processed | `jugnu_runner` |
| Null-field-recovery confidence gate | ≥0.85 | `jugnu_runner` |
| Drift detector drop | <0.7 × expected count | `drift_detector.py:32` |

---

## 12. LLM Integration

### Provider chain
- **Primary:** OpenRouter via `llm.factory.get_text_provider()` → `provider.complete(system, prompt, max_tokens=4096)`
- **Rescue fallback:** Direct Azure OpenAI httpx call in `llm_api_rescue.py` (Azure endpoint/key from env)
- **Vision:** Separate `VISION_PROVIDER` env var; used only in Tier 5

### Prompt files
- `config/prompts/tier4_extraction.txt` — monolithic HTML+API extraction
- `config/prompts/api_analysis.txt` — targeted single-API analysis (`analyze_api_with_llm`)
- `config/prompts/dom_analysis.txt` — targeted DOM section analysis (`analyze_dom_with_llm`)
- `config/prompts/llm_api_rescue.txt` — rescue prompt (inferred from rescue service)

### LLM output contract (from `llm_extractor._normalize_units`)
```json
{
  "units": [
    {
      "unit_id": "string | null",
      "floor_plan_name": "string | null",
      "bedrooms": "number | null",
      "bathrooms": "number | null",
      "sqft": "number | null",
      "market_rent_low": "number | null",
      "market_rent_high": "number | null",
      "available_date": "YYYY-MM-DD | null",
      "availability_status": "AVAILABLE|UNAVAILABLE|WAITLIST|UNKNOWN",
      "confidence": "0.0–1.0"
    }
  ],
  "profile_hints": {
    "api_urls_with_data": [],
    "json_paths": {},
    "css_selectors": {},
    "platform_guess": null,
    "navigation_hint": "",
    "field_mapping_notes": ""
  }
}
```

### LLM cost accounting
Every LLM interaction is logged via `llm.interaction_logger.make_interaction()` and attached to `result["_llm_interactions"]`. Cost is summed in `ExtractResult.llm_cost_usd`. Run-level cost is aggregated in `llm_report.json`.

---

## 13. Observability

**Event ledger:** `emit(EventKind.X, property_id, **kwargs)` — swallows all exceptions. Fire-and-forget.

Key event kinds:
```
DETECTOR_SIGNALS, HTML_CHARACTERIZED, PMS_DETECTED, ADAPTER_SELECTED
TIER_WON, TIER_FAILED
LINK_HOP_STARTED, LINK_HOP_FETCHED, LINK_HOP_RECOVERED
LLM_RESCUE_ATTEMPTED, LLM_RESCUE_SUCCEEDED, LLM_RESCUE_FAILED
```

**Run outputs:**
- `data/runs/{date}/properties.json` — all property records (46-key schema)
- `data/runs/{date}/report.json` and `report.md` — run summary
- `data/runs/{date}/property_reports/{canonical_id}.md` — per-property detail
- `data/runs/{date}/llm_report.json` — run-wide LLM cost aggregate
- `data/runs/{date}/llm_reports/{canonical_id}.json` — per-property LLM detail
- `data/runs/{date}/llm_diagnostics/{canonical_id}_adapter_debug.json` — F1 artifact
- `data/runs/{date}/llm_diagnostics/{canonical_id}_null_field_recovery.json` — F2 artifact

**Persistent state:**
- `data/state/frontier.sqlite` — URL frontier (dedup, last attempted, etag)
- `data/state/dlq.jsonl` — dead-letter queue
- `data/state/cache/conditional.sqlite` — ETag/Last-Modified cache

---

## 14. Known Issues and Design Gaps (from live codebase)

These are confirmed issues in the current code, not speculation:

1. **Carry-forward verdict mislabelling.** CF path stamps `verdict=SUCCESS` with `verdict_reason=carry_forward_applied`. The `CARRY_FORWARD` verdict string in `verdict.py` is never produced. Filters on `verdict==CARRY_FORWARD` return zero.

2. **HEAD render mode is dead code.** `change_detector` never produces HEAD. The fetcher's HEAD path yields `body=None` which fails extraction. The code branch exists but is unreachable.

3. **Quality gate passes partial records.** `is_substantive()` returns True on a unit with only `floor_plan_name` and no rent. 50 units with floor plan names but zero rent pass the quality gate and are emitted as success.

4. **No cross-endpoint merging.** `rescue_from_api_responses()` returns on the first passing candidate. If `/floorplans` gives beds/baths/sqft and `/availability` gives rent, nothing attempts both and joins them.

5. **LLM budget not enforced by a shared counter.** Each tier (API, DOM, monolithic) has its own cap. All three can fire on the same property for a total of up to 5 LLM calls. There is no single counter that blocks tier N+1 if budget is exhausted.

6. **Two-pass detection events.** Events only emit for the final detection. Intermediate signals that were overridden are not logged, making misrouting diagnosis hard.

7. **Confirmation check is asymmetric.** Under-detection (URL says "unknown" but body matches a known adapter) is not caught. Only over-detection (URL predicts PMS, body doesn't match) is corrected.

8. **Drift detector depends on `last_unit_count`.** COLD→WARM transitions have no expected count, so drift never fires on first success.

9. **`consecutive_llm_rescue_failures` counter on `stats`.** This field controls F2 rescue firing. If it hits 3, rescue is permanently skipped for the property until manually reset.

10. **`skip_llm = (detected.pms != "unknown")`.** Any detected PMS adapter gets zero LLM assistance even when it produces zero units — the relaxed gate requires ≥5KB text + ≥1 rent signal to re-enable LLM.

11. **Null-field-recovery hard-codes 5 units.** Properties with 50 units where 30 have null rent_low will only recover 5.

12. **Rent sanity lower bound $200/month.** Student housing, room rentals, and subsidised units below $200/month are silently rejected.

---

## 15. Coding Conventions (Enforced — Do Not Change)

- **Pydantic v2:** Always `model.model_dump(mode="json")`. Never `.dict()`.
- **Hashing:** Always `hashlib.sha256`. Never built-in `hash()` (non-deterministic across processes).
- **Concurrency:** `asyncio.Semaphore` for browser concurrency. `asyncio.Lock` for state file writes.
- **Playwright resource cleanup:** Always `context.close()`, never `browser.close()`.
- **Test command:** `pytest . --ignore=data --ignore=config`
- **Never-fail contract:** Every layer catches exceptions and returns structured failures. No raw `raise` in pipeline code.
- **LLM as teacher:** LLM extraction fires to populate deterministic profile hints. On WARM/HOT profiles, LLM should not fire at all.
- **Adapters never import LLM modules.** Only the scraper orchestrator calls LLM services. PMS-specific adapters are deterministic.
- **Profile writes go through `ProfileStore.save()`.** Never write profile JSON directly.
- **Field names are frozen.** `UnitRecord` field names are shared with Phase B. Do not rename.

---

## 16. Module Map

```
ma_poc/
├── scripts/
│   ├── jugnu_shard_entry.py         # Container entrypoint: CSV download, shard slice, runner subprocess, PG sync, GCS upload
│   ├── jugnu_runner.py              # Per-shard property loop: L1→L4 + profile learning + output
│   ├── jugnu_retry_entry.py         # Retry job entrypoint (single-task, no sharding)
│   ├── sync_run_to_pg.py            # sync_run_to_postgres() — FS output → Cloud SQL (Stage 1 session txn + Stage 2 FOR UPDATE merge)
│   ├── concurrency.py               # AsyncPool, SystemResources.detect(), optimal_pool_size()
│   └── check_proxy.py               # Proxy health check (SMOKE_MODE=proxy_check)
├── pms/
│   ├── scraper.py               # scrape() + scrape_jugnu() — L3 orchestrator
│   ├── detector.py              # detect_pms(), confirm_detection()
│   ├── resolver.py              # resolve_target() — CTA hop / iframe / redirect
│   └── adapters/
│       ├── base.py              # AdapterContext, AdapterResult, PmsAdapter Protocol
│       ├── registry.py          # get_adapter()
│       ├── generic.py           # GenericAdapter — full tier cascade
│       ├── entrata.py           # EntraAdapter
│       ├── rentcafe.py          # RentCafeAdapter
│       ├── appfolio.py          # AppFolioAdapter
│       └── sightmap.py          # SightMapAdapter
├── models/
│   ├── scrape_profile.py        # ScrapeProfile + sub-models
│   └── unit_record.py           # UnitRecord (canonical output)
├── discovery/
│   ├── scheduler.py             # Scheduler — CSV → CrawlTask
│   ├── change_detector.py       # RENDER vs GET decision
│   ├── dlq.py                   # Dead-letter queue
│   └── contracts.py             # CrawlTask, TaskReason
├── fetch/
│   ├── fetcher.py               # fetch() — CrawlTask → FetchResult
│   ├── contracts.py             # FetchResult, FetchOutcome, RenderMode
│   ├── stealth.py               # Identity pool
│   ├── proxy_pool.py            # Proxy health scoring
│   ├── rate_limiter.py          # Per-host token bucket
│   └── captcha_detect.py        # looks_like_captcha()
├── validation/
│   ├── schema_gate.py           # is_substantive(), property_passes_quality_gate(), check()
│   ├── identity_fallback.py     # compute_fallback_id()
│   └── cross_run_sanity.py      # Rent/sqft swing checks
├── services/
│   ├── profile_store.py         # ProfileStore — load/save/bootstrap
│   ├── profile_updater.py       # update_profile_after_extraction() + helpers
│   └── llm_api_rescue.py        # rescue_from_api_responses() — F2 LLM rescue
├── extraction/
│   └── heuristics.py            # looks_like_availability_api(), response_looks_like_units()
├── observability/
│   └── events.py                # emit(), EventKind
└── reporting/
    └── property_report.py       # Per-property markdown report

services/
└── llm_extractor.py             # extract_with_llm(), analyze_api_with_llm(), analyze_dom_with_llm()

llm/
├── factory.py                   # get_text_provider()
└── interaction_logger.py        # make_interaction()

infra/
└── main.tf + modules/           # Terraform — GCP infra
    ├── artifact_registry/
    ├── iam/
    ├── storage/
    ├── cloud_sql/
    ├── secrets/
    ├── cloud_run_jobs/           # Compute — scrape_job_id + retry_job_id
    └── scheduler/               # Cloud Scheduler (retry paused by default)

config/
├── profiles/                    # Per-property ScrapeProfile JSON files
│   └── _audit/                  # Versioned audit copies
└── prompts/                     # LLM prompt templates
    ├── tier4_extraction.txt
    ├── api_analysis.txt
    └── dom_analysis.txt

data/
├── runs/{date}/                 # Per-run outputs
└── state/                       # Persistent state (frontier, DLQ, cache)

---

### Unit Identity Resolution (Post PR #21)

Unit identity uses a two-tier cascade:

**Tier 1 — Natural key:** `unit_id` from the PMS API (e.g., unit number "101").
Set by all API-level parsers in `scrape_properties.py`.

**Tier 2 — Stable fallback:** `compute_fallback_unit_id(unit, property_id)` from
`ma_poc/scripts/identity_fallback.py`. Called by `_add()` in `scrape_properties.py`
when `unit_id` is absent. Hash inputs: `property_id | floor_plan | beds | baths | sqft±10`.
**Rent and available_date are never hashed** — they are attributes, not identity.
The resulting `inferred_{sha256[:16]}` ID is written onto the unit record before it
reaches `upsert_units`.

**Field stripping** (`_sqft`, `_floor_plan`, `_bedrooms`) now occurs AFTER
`state.upsert_units()` so physical attributes are persisted in the state snapshot
and available for carry-forward.

**Carry-forward** fires when `scrape_failed OR not target_units AND state.is_known(cid)`.
Properties are pre-registered before the first scrape so COLD properties are
immediately eligible.

**Grace period:** units missing from a partial scrape are not immediately marked
disappeared. `absent_streak` is incremented per run; `disappeared` is only appended
to the diff after `absent_streak >= disappeared_grace_days` (default: 2).
```