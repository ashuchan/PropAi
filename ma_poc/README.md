# MA Rent Intelligence Platform — Phase A POC

Multifamily rent intelligence pipeline that scrapes 500+ property websites daily,
extracts unit-level rent and availability data through a 7-phase extraction pipeline
with self-learning per-property profiles.

## Setup

```bash
cd ma_poc
python -m venv .venv
.venv\Scripts\activate   # Windows
pip install -r requirements.txt
playwright install chromium
cp .env.example .env     # then fill in keys
```

## Architecture

The platform uses the **Jugnu Pipeline** as its sole production pipeline.
`daily_runner.py` and `retry_runner.py` were deleted in the 2026-05-06 refactor (PR-9).

### Jugnu Pipeline

A 5-layer horizontal architecture with hard contracts between layers:

```
jugnu_runner.py
  L1 Fetch     → Stealth browser pool, proxy rotation, rate limiting, conditional cache
  L2 Discovery → Scheduler, frontier, sitemap consumer, DLQ, carry-forward
  L3 Extraction→ PMS detection → resolution → adapter extraction (10 adapters)
  L4 Validation→ Schema gate, identity fallback, cross-run sanity checks
  L5 Observability → Event ledger, cost ledger, SLO watcher, replay store
```

**Key properties:**
- Never-fail contract: no single property crashes the run
- LLM as teacher not worker: median property makes 0 LLM calls once profile is warm
- Deterministic hashing (`hashlib.sha256`, never `hash()`)
- Pydantic v2 serialisation (`model_dump(mode="json")`, never `.dict()`)

## Running

```bash
# Full daily run (all properties)
python scripts/jugnu_runner.py --csv config/properties.csv

# Test with N properties
python scripts/jugnu_runner.py --csv config/properties.csv --limit 5

# With proxy (not yet wired — uses env var PROXY_HOST)
python scripts/jugnu_runner.py --csv config/properties.csv --limit 5

# Override run date
python scripts/jugnu_runner.py --csv config/properties.csv --run-date 2026-04-18
```

### Retry Failed Properties

```bash
# Retry failures from the latest run (auto-detects)
python scripts/jugnu_retry_runner.py --retry-errors

# Retry failures from a specific date
python scripts/jugnu_retry_runner.py --retry-errors --run-date 2026-04-17

# Resume an interrupted run
python scripts/jugnu_retry_runner.py --resume

# Retry with a limit
python scripts/jugnu_retry_runner.py --retry-errors --limit 10

# Retry legacy runs (provide CSV for URL lookup)
python scripts/jugnu_retry_runner.py --retry-errors --run-date 2026-04-17 --csv config/properties.csv
```

`--retry-errors` retries: FAILED properties + any property with 0 units.
`--resume` retries: everything that isn't a clean success.

Results are merged into the existing `properties.json` — successful retries replace their prior failed records.

### Single property debug

```bash
python scripts/entrata.py --url https://property-website.com
```

### Gate Checks

```bash
# Check all Jugnu phase gates
python scripts/gate_jugnu.py all

# Check a specific phase
python scripts/gate_jugnu.py phase 1

# Run tests for a phase
python scripts/gate_jugnu.py tests 1
```

## Jugnu Layer Architecture

### L1 — Fetch (`ma_poc/fetch/`)

Stealth HTTP/browser fetching with never-raise contract.

| Module | Purpose |
|---|---|
| `contracts.py` | `FetchResult`, `FetchOutcome`, `RenderMode` frozen dataclasses |
| `fetcher.py` | Main orchestrator: robots → cache → rate limit → request → classify → retry |
| `browser_pool.py` | Playwright context pool with semaphore |
| `proxy_pool.py` | Health-weighted proxy selection with quarantine |
| `rate_limiter.py` | Async token bucket per host |
| `stealth.py` | Identity pool with 8 curated browser fingerprints |
| `conditional.py` | SQLite-backed ETag/Last-Modified cache |
| `retry_policy.py` | Exponential backoff with jitter, identity/proxy rotation |
| `response_classifier.py` | HTTP status → FetchOutcome mapping |
| `captcha_detect.py` | Cloudflare/reCAPTCHA/hCaptcha/PerimeterX detection |
| `robots.py` | robots.txt consumer with 24h TTL cache |

### L2 — Discovery (`ma_poc/discovery/`)

URL scheduling, deduplication, and failure recovery.

| Module | Purpose |
|---|---|
| `contracts.py` | `CrawlTask`, `TaskReason` frozen dataclasses |
| `scheduler.py` | Builds prioritised task list: DLQ revive → render → head/get → sitemap |
| `frontier.py` | SQLite-backed URL frontier with consecutive failure tracking |
| `sitemap.py` | Sitemap.xml consumer with ETag caching, 10-child cap |
| `change_detector.py` | Pure function: profile maturity + frontier state → crawl/skip decision |
| `dlq.py` | Dead-letter queue with hourly→daily retry escalation |
| `carry_forward.py` | Safety net: copies prior data on fetch failure |

### L3 — Extraction (`ma_poc/pms/`)

PMS-aware extraction with 10 adapters.

| Module | Purpose |
|---|---|
| `detector.py` | Offline PMS detection from URL/HTML signals |
| `resolver.py` | CTA-hop + leasing portal resolver (follows redirects to PMS pages) |
| `scraper.py` | Orchestrator: detect → resolve → adapt. `scrape_jugnu()` entry point. Link-hop acts on LLM `navigation_hint` when extraction is empty. |
| `adapters/` | RentCafe, Entrata, AppFolio, OneSite, SightMap, RealPage OLL, AvalonBay, Squarespace, Wix, Generic |
| `adapters/tier_orchestrator.py` | `GenericAdapter` class (cascade orchestration, split from `generic.py` in PR-4) |
| `adapters/tiers/` | Sub-tier classes: `blocked_filter`, `profile_replay`, `api_narrow`, `api_broad`, `html_tiers`, `llm_tiers` |

`AdapterContext` threads `property_name`, `city`, `state`, `zip_code`,
`pmc` from the CSV row into every extraction call. All three LLM prompt
templates reference these placeholders.

The generic adapter runs a profile-aware cascade: blocked-endpoint
filter → saved `LlmFieldMapping` replay → narrow API → broad API →
JSON-LD (rent/sqft-gated) → embedded JSON → DOM scan → targeted API
LLM (max 3) → targeted DOM LLM (max 1) → monolithic LLM. See
`scripts/CLAUDE.md` for the full table.

**Extraction engine (PR-1–PR-3):** Phases 1–7 are implemented as classes in
`extraction/engine/phases/`. `scrape()` in `entrata.py` is a 15-line
orchestration shell using `PhaseContext` and `PhaseRegistry`.

**Extraction parsers (PR-5):** Host-specific parsers (`GenericApiParser`,
`SightMapParser`, `RealPageParser`) live in `extraction/parsers/` behind the
`UnitParser` protocol.

**LLM services (PR-6):** `services/llm/` contains `monolithic.py`,
`api_analyzer.py`, `dom_analyzer.py`, `mapping_replay.py`. `llm_extractor.py`
is a backward-compat re-export shim.

### L4 — Validation (`ma_poc/validation/`)

Schema enforcement, identity resolution, cross-run sanity.

| Module | Purpose |
|---|---|
| `schema_gate.py` | Rent bounds ($0–$50K), sqft bounds, date format checks |
| `identity_fallback.py` | SHA256 fingerprint of (floor_plan, beds, baths, sqft, rent) |
| `cross_run_sanity.py` | Flags rent swings >20%, sqft changes >5% (flags, never rejects) |
| `orchestrator.py` | Runs gate → fallback → sanity, sets `next_tier_requested` |

### L5 — Observability (`ma_poc/observability/`)

Event tracking, cost accounting, SLO monitoring.

| Module | Purpose |
|---|---|
| `events.py` | 28 event types, `emit()` function, buffered ledger backend |
| `event_ledger.py` | Append-only JSONL with crash-safe reads |
| `cost_ledger.py` | SQLite-backed LLM/vision/proxy cost tracking |
| `slo_watcher.py` | Reads `_meta.verdict` + `_extract_result.tier_used`. Success rate >=95%, LLM cost <$1, vision fallback <=5% |
| `replay_store.py` | Load raw HTML + events for property replay debugging |
| `dlq_controller.py` | Policy layer: parks after 3 consecutive unreachable |

### Reporting (`ma_poc/reporting/`)

| Module | Purpose |
|---|---|
| `verdict.py` | Per-property verdict: SUCCESS, FAILED_UNREACHABLE, CARRY_FORWARD, etc. |
| `run_report.py` | Run-level JSON + markdown report with SLO section. Reads outcome from `_meta.verdict` and tier from `_extract_result.tier_used`. |
| `property_report.py` | Per-property markdown report including LLM Interactions section |

### Self-learning loop

After every scrape, `services.profile_updater.update_profile_after_extraction`
consumes these result-dict keys to persist learned knowledge on
`config/profiles/{canonical_id}.json`:

| Result key | Consumed as |
|---|---|
| `_winning_page_url` | `profile.navigation.winning_page_url` |
| `_raw_api_responses` | `profile.api_hints.known_endpoints` (for successful Tier-1 runs) |
| `_llm_analysis_results` | Dict-valued entries → `profile.api_hints.llm_field_mappings` (replayable without LLM next run). `"noise:<reason>"` strings → `profile.api_hints.blocked_endpoints` (filtered before extraction). |
| `_llm_hints.css_selectors` | `profile.dom_hints.field_selectors` |
| `_llm_hints.platform_guess` | `profile.dom_hints.platform_detected`, `profile.api_hints.api_provider` |
| `_explored_links` | `profile.navigation.availability_links` (had data) / `explored_links` (empty) |

Maturity rules: 1 success → WARM, 3 consecutive successes → HOT, 3
consecutive failures → COLD. HOT profiles get a fast path via
`TIER_1_PROFILE_MAPPING` that replays saved mappings without any LLM call.

## Inputs

- `config/properties.csv` — property list (columns: `apartmentid`, `name`, `address`, `city`, `state`, `zip`, `website`)
- `config/profiles/` — per-property learned extraction profiles (auto-generated)
- `config/prompts/` — LLM prompt templates
- `.env` — API keys (Azure OpenAI, Anthropic), proxy config

## Output

Production output in `data/runs/{YYYY-MM-DD}/`:
- `properties.json` — property records with nested units
- `report.json` / `report.md` — run summary with SLO status
- `cost_ledger.db` — per-property LLM/vision cost breakdown

Persistent state in `data/state/`:
- `frontier.sqlite` — URL frontier with attempt history
- `dlq.jsonl` — dead-letter queue for retry scheduling
- `property_index.json` — tracks first_seen, last_seen, scrape status per property
- `unit_index.json` — unit history with daily diffs

## Tests

```bash
# All tests
pytest . -v --tb=short --ignore=data --ignore=config

# By layer
pytest tests/fetch/ -v          # L1 fetch (43 tests)
pytest tests/discovery/ -v      # L2 discovery (35 tests)
pytest tests/pms/ -v            # L3 extraction (4 tests)
pytest tests/validation/ -v     # L4 validation (30 tests)
pytest tests/observability/ -v  # L5 observability (19 tests)
pytest tests/reporting/ -v      # Reporting (9 tests)
pytest tests/baseline/ -v       # Baseline metrics (10 tests)

# Gate check
python scripts/gate_jugnu.py all
```

## Documentation

- [scripts/CLAUDE.md](scripts/CLAUDE.md) — full implementation guide (7-phase extraction engine, Jugnu layers, profile system, failure modes, refactor history)
- [docs/BUG_HUNT_CHECKLIST.md](docs/BUG_HUNT_CHECKLIST.md) — 40-item bug hunt checklist across all layers
- [docs/JUGNU_BASELINE.md](docs/JUGNU_BASELINE.md) — baseline metrics from pre-Jugnu runs
- [../CLAUDE.md](../CLAUDE.md) — BRD Phase A spec (reference)

## Refactor history (2026-05-06)

9-PR SRP refactoring completed on branch `claude/analyze-refactoring-plan-9nMdw`:

| PR | Change |
|----|--------|
| PR-1 | Extracted `BrowserSession`, `NetworkCapture`, `NoiseFilter` → `extraction/engine/` |
| PR-2 | Extracted `Phase1HomepageLoad`, `Phase2NoiseFilter` as classes; wired `PhaseRegistry` |
| PR-3 | Extracted Phases 3–7 as classes; `scrape()` → 15-line orchestrator |
| PR-4 | Split `generic.py` → `tier_orchestrator.py` + `tiers/` sub-tier classes |
| PR-5 | Added `extraction/parsers/` with `UnitParser` protocol + 3 concrete parsers |
| PR-6 | Split `llm_extractor.py` → `services/llm/` with `monolithic`, `api_analyzer`, `dom_analyzer` |
| PR-7 | Decomposed `daily_runner.py` utilities → `scripts/state/` + `orchestration/` + `reporting/` |
| PR-8 | Added `RetryPipeline` class in `scripts/orchestration/` |
| PR-9 | Deleted `daily_runner.py` + `retry_runner.py`; Jugnu is the sole pipeline |
