# PropAi Scripts — Implementation Guide

## What this directory is

`scripts/` contains the **production scraping pipeline** — the system that actually runs against live property websites, extracts unit data, tracks state across daily runs, and produces the 46-key property output schema.

---

## Extraction cascade inside GenericAdapter (2026-04-19 refactor)

The Jugnu `GenericAdapter` (`ma_poc/pms/adapters/generic.py`) runs tiers in this order. Each tier emits `extract.tier_attempted` so the per-property report can show exactly what fired.

| Sub-tier | Key | What it does |
|---|---|---|
| 0 | `generic:blocked_filter` | Drops API responses matching `profile.api_hints.blocked_endpoints` before any extractor sees them. Populated by Phase 3 LLM noise classification. |
| 0 | `generic:profile_replay` | Deterministic replay of saved `LlmFieldMapping`. Zero LLM cost when a prior run already learned the API shape. Wins with tier `TIER_1_PROFILE_MAPPING`. |
| 1 | `generic:api_narrow` | Narrow generic API parser (`parse_generic_api`) on responses with unit signals. Now emits `market_rent_low/high` ints alongside `rent_range`. |
| 2 | `generic:api_broad` | Broad parser + host-specific (SightMap, RealPage) parsers. |
| 3 | `generic:jsonld` | JSON-LD Apartment/Offer parser. **Rejects plan-name-only output** (no rent, no sqft) so it doesn't block the LLM sub-tiers. |
| 4 | `generic:embedded_json` | `__NEXT_DATA__` / `__NUXT__` / window-global SSR blobs. |
| 5 | `generic:dom_scan` | CSS-selector DOM cascade. Now filters junk (`MODULE_*`, "Lease Magnet", "Pop-Up") at extract time. |
| 6a | `generic:llm_api_targeted` | `analyze_api_with_llm` on each candidate API (max 3/property). Returns units + `json_paths` + `response_envelope` — persisted as a replayable mapping. Tier `TIER_4_LLM_API`. |
| 6b | `generic:llm_dom_targeted` | `analyze_dom_with_llm` on the tightest rent-containing DOM section (max 1/property). Returns units + CSS selectors. Tier `TIER_4_LLM_DOM`. |
| 6c | `generic:llm` | Monolithic fallback (`extract_with_llm`). Only fires when 6a + 6b returned empty. Captures `navigation_hint` so link-hop can follow the LLM's pointer. |

### LLM gate and budget

- Default: `skip_llm = (ctx.detected.pms != "unknown")`.
- Relaxed (`LLM_GATE_RELAXED`) when the detected adapter returned empty AND the page has ≥5KB text + ≥1 rent signal. Covers SightMap / RentCafe failures where the API wasn't captured.
- Budget: 3 targeted API calls + 1 targeted DOM call + 1 monolithic fallback. Tracked in `ma_poc/observability/cost_ledger.py`.

### Self-learning payload surfaced to the profile updater

Every `scrape_jugnu` result dict now carries:
- `_llm_interactions` — cost-accounting records for every LLM call.
- `_llm_analysis_results` — `{api_url: LlmFieldMapping | "noise:<reason>"}`. Consumed by `services.profile_updater.update_profile_after_extraction` to persist `llm_field_mappings` and `blocked_endpoints`.
- `_llm_field_mappings` — list of the mapping dicts written on this run.
- `_llm_hints` — `{css_selectors: {...}, platform_guess, navigation_hint, ...}`.
- `_llm_navigation_hints` — URLs extracted from `navigation_hint` fields; forwarded to link-hop as priority-1000 candidates.
- `_explored_links` — `{sub_url: had_data_bool}` from link-hop. Populated even when the hop recovered nothing so the profile learns which links NOT to revisit.

### Property context threaded into every prompt

`AdapterContext` (see `ma_poc/pms/adapters/base.py`) carries `property_name`, `city`, `state`, `zip_code`, `pmc` sourced from the CSV row. All three prompt templates (`tier4_extraction.txt`, `api_analysis.txt`, `dom_analysis.txt`) reference these placeholders. Before this change the generic adapter hard-coded them to empty strings.

---

## Architecture overview

### Jugnu Pipeline (`jugnu_runner.py`) — 5-Layer Architecture

```
CSV input
    |
    v
jugnu_runner.py              # Integrated runner wiring all 5 layers
    |
    L2 Scheduler              # Builds prioritised task list from CSV + frontier + DLQ
    |   +-- frontier.py       # SQLite-backed URL frontier with attempt history
    |   +-- sitemap.py        # Sitemap.xml consumer with ETag caching
    |   +-- change_detector.py# Pure function: maturity + frontier → crawl/skip
    |   +-- dlq.py            # Dead-letter queue: hourly→daily retry escalation
    |
    L1 Fetcher                # Stealth HTTP/browser fetch, never raises
    |   +-- fetcher.py        # 9-step flow: robots → cache → rate limit → request → classify → retry
    |   +-- browser_pool.py   # Playwright context pool with semaphore
    |   +-- proxy_pool.py     # Health-weighted proxy selection + quarantine
    |   +-- rate_limiter.py   # Async token bucket per host
    |   +-- stealth.py        # 8 curated browser identities, SHA256 sticky keys
    |   +-- conditional.py    # SQLite ETag/Last-Modified cache
    |   +-- captcha_detect.py # Cloudflare/reCAPTCHA/hCaptcha/PerimeterX detection
    |
    L3 Extraction             # PMS-aware adapter extraction
    |   +-- detector.py       # Offline PMS detection from URL/HTML signals
    |   +-- resolver.py       # CTA-hop + leasing portal resolver
    |   +-- scraper.py        # scrape_jugnu(): detect → resolve → adapt
    |   +-- adapters/         # 10 adapters: RentCafe, Entrata, AppFolio, OneSite,
    |                         #   SightMap, RealPage OLL, AvalonBay, Squarespace, Wix, Generic
    |
    L4 Validation             # Schema enforcement + identity resolution
    |   +-- schema_gate.py    # Rent bounds, sqft bounds, date format checks
    |   +-- identity_fallback.py  # SHA256 fingerprint fallback for missing unit_id
    |   +-- cross_run_sanity.py   # Flags rent swings >20%, sqft changes >5%
    |   +-- orchestrator.py   # Runs gate → fallback → sanity, sets next_tier_requested
    |
    L5 Observability          # Event tracking + cost accounting + SLO
    |   +-- events.py         # 81 event types, emit() with buffered ledger backend
    |   +-- event_ledger.py   # Append-only JSONL, crash-safe reads
    |   +-- cost_ledger.py    # SQLite LLM/vision/proxy cost tracking
    |   +-- slo_watcher.py    # Success >=95%, LLM cost <$1, vision <=5%
    |   +-- replay_store.py   # Load raw HTML + events for debugging
    |   +-- dlq_controller.py # Parks after 3 consecutive unreachable
    |
    Reporting
    |   +-- verdict.py        # Per-property: SUCCESS/FAILED_UNREACHABLE/CARRY_FORWARD/PARTIAL
    |   +-- run_report.py     # JSON + markdown report with SLO section
    |
    +-- Output:
          data/runs/{date}/properties.json   # Property records with nested units
          data/runs/{date}/report.json       # Run summary
          data/runs/{date}/report.md         # Human-readable report with SLO status
          data/runs/{date}/cost_ledger.db    # Per-property cost breakdown
          data/state/frontier.sqlite         # URL frontier with attempt history
          data/state/dlq.jsonl               # Dead-letter queue
```

---

## Running the pipeline

```bash
python scripts/jugnu_runner.py --csv config/properties.csv
python scripts/jugnu_runner.py --csv config/properties.csv --limit 5
python scripts/jugnu_runner.py --csv config/properties.csv --run-date 2026-04-18
```

---

## Output schema (46-key property record)

Each property in `data/runs/{date}/properties.json`:

```json
{
  "Property Name": "San Artes Apartments",
  "Type": "Stabilized",
  "Unique ID": "SMOKE-001",
  "Property ID": "SMOKE-001",
  "Average Unit Size (SF)": 1258,
  "Total Units": 46,
  "Unit Mix": "1BR: 14; 2BR: 24; 3BR: 8",
  "First Move-In Date": "2026-04-11",
  "City": "Scottsdale",
  "State": "AZ",
  "ZIP Code": "85255",
  "Property Address": "8585 E Hartford Dr",
  "Latitude": null,
  "Longitude": null,
  "Property Type": "Garden-Style",
  "Property Status": "Stabilized",
  "Property Style": "Garden-Style",
  "Management Company": "Mark-Taylor",
  "Phone": "(555) 123-4567",
  "Website": "https://example.com",
  "Year Built": null,
  "Stories": null,
  "Census Block Id": null,
  "Tract Code": null,
  "Construction Start Date": null,
  "Construction Finish Date": null,
  "Renovation Start": null,
  "Renovation Finish": null,
  "Development Company": null,
  "Property Owner": null,
  "Region": null,
  "Market Name": null,
  "Submarket Name": null,
  "Asset Grade in Submarket": null,
  "Asset Grade in Market": null,
  "Lease Start Date": null,
  "Update Date": "2026-04-13",
  "_meta": { ... },
  "units": [ ... ]
}
```

Fields set to `null` require external data sources (CoStar, county assessor, Census API) — they cannot be scraped from property websites.

### Unit schema (per unit)

```json
{
  "unit_id": "1004",
  "market_rent_low": 2800,
  "market_rent_high": 2800,
  "available_date": "2026-05-12",
  "lease_link": "https://...",
  "concessions": null,
  "amenities": null
}
```

### V2 unit-dict conventions (2026-04-19 refactor)

The v2 schema transform (`_format_v2_unit` in `jugnu_runner.py` and
`schema_v2.py`) now tolerates several adapter-side naming conventions:

| v2 output key | Read from (in priority order) |
|---|---|
| `unit_id` | `unit_id` → `unit_number` → `_unit_number` |
| `rent_low` / `rent_high` | `market_rent_low/high` → `asking_rent` → parsed from `rent_range` string (e.g. `"$1,200 - $1,500"`) |
| `floor_plan_name` | `_floor_plan` → `floor_plan_name` → `floorplan_name` |
| `area` | `_sqft` → `sqft` → `area` (rejected if outside 150-10000 sqft) |
| `beds` / `baths` | `None` when the source emitted nothing — no silent default to 0 / 1.0 |

`lease_term` and `move_in_date` are plumbed end-to-end; parsers that learn
to extract them don't need a format change. Today they're still mostly
null because the regex/LLM paths don't target them yet.

Junk filters (`is_junk_floor_plan`, `is_junk_unit_number` in
`ma_poc/pms/adapters/_parsing.py`) drop CMS module names (`MODULE_*`,
"Lease Magnet", "Pop-Up", vendor prefixes like `[Riedman]`) and
stop-word unit numbers ("Left", "s", etc). Applied in both
`parse_generic_api` and the v2 transform for belt-and-braces safety.

### Carry-forward persists the full unit (2026-04-19)

`StateStore.upsert_units` now snapshots bedrooms, bathrooms, sqft,
floor_plan_name, unit_number, rent_range, lease_term, and move_in_date
alongside rent/availability. `carry_forward_units` emits the full prior
dict so a CF-SUCCESS record ships with complete data instead of a
rent-only stub.

---

## Implementation principles

### Never-fail contract
- Every scrape is wrapped in `try/except`; no single property can crash the run
- State file writes use atomic temp-file + `os.replace()` so a crash never corrupts state
- Incremental writes to `properties.json` after each property — an interrupted run still leaves a usable file

### Scraping resilience
- `networkidle` timeout is capped at 5 seconds (not blocking), fallback to `domcontentloaded`
- Click-to-expand is best-effort — exceptions are swallowed, scraping continues
- Link exploration capped at `MAX_CRAWL_PAGES=10` pages per property
- Per-property scrape timeout (default 180s) prevents stuck pages from hanging the run
- Profile-learned `explored_links` skip pages that previously had no data
- Profile-learned `blocked_endpoints` skip noise APIs without re-analyzing them

### Data priority rules
- **CSV values always take precedence** over scraped values for fields that exist in the CSV (address, city, state, zip, name)
- **Scraped metadata fills in** only what the CSV left blank
- **Computed aggregates** (avg sqft, unit mix, first move-in) are always recomputed from today's units

### Rent sanity bounds
- Units with rent outside `$200–$50,000/month` are rejected (catches misidentified fields like "rent=14")
- Generic API parser requires each candidate list to have 3+ dicts with BOTH a unit-id key AND a rent-like key before accepting it

### Deduplication
- Unit-level: by `unit_id`, or by `floor_plan|sqft|rent` fingerprint if no unit_id
- Property-level: by canonical_id (identity resolution prevents duplicate scrapes)
- API-level: `seen_api_urls` set prevents processing the same API response twice

---

## CSV input format

The pipeline accepts flexible column names. Both formats work:

```csv
Property Name,Property URL,Property Type,Property ID,City,State,ZIP Code
San Artes,https://example.com,Stabilized,P001,Scottsdale,AZ,85255
```

```csv
name,url,type,property_id,City,State,ZIP
San Artes,https://example.com,Stabilized,P001,Scottsdale,AZ,85255
```

Required: at least a URL column and one identity column (Unique ID, Property ID, or address).

Optional enrichment columns: `Management Company`, `Building Type`, `Total Units (Est.)`, `Year Built`, `Stories`, `Latitude`, `Longitude`.

---

## Relationship to ma_poc/templates/ and ma_poc/extraction/

The `templates/` directory (`rentcafe.py`, `entrata.py`, `appfolio.py`) and `extraction/` pipeline (`tier1_api.py` through `tier5_vision.py`) are the **Phase A BRD-spec implementation**. They use BeautifulSoup on static HTML, operate from a `BrowserSession` dataclass, and output `UnitRecord` / `ExtractionResult` models.

`scripts/entrata.py` is a **parallel implementation** that uses Playwright directly (live page interaction, `page.query_selector_all`, `page.evaluate`). It handles the same extraction tiers but with different code paths optimized for real-world scraping:

- Multi-page crawling (BFS across internal links)
- SightMap dedicated API parser (joins units to floor plans)
- 50+ API key name variants in the generic parser
- `page.evaluate()` for JSON-LD extraction (runs in browser context)
- DOM parsing via live Playwright selectors + regex on `innerText`

The two systems do not share extraction code. When adding new PMS platform support or fixing extraction bugs, changes need to be made in **both places** if you want both pipelines to benefit.

---

## Self-Learning Scrape Profile System

Every property gets a per-property profile stored at `config/profiles/{canonical_id}.json`. The profile learns from each scrape run — recording which APIs work, which are noise, what CSS selectors to use, and what LLM-generated field mappings can be replayed deterministically.

### Profile model (`models/scrape_profile.py`)

```
ScrapeProfile
├── canonical_id: str
├── version: int (auto-incremented on each save)
├── created_at / updated_at: datetime
├── updated_by: str (BOOTSTRAP | LLM_EXTRACTION | LLM_VISION | HUMAN)
│
├── navigation: NavigationConfig
│   ├── entry_url: str                  # Homepage URL
│   ├── availability_page_path: str     # e.g., "/floor-plans"
│   ├── winning_page_url: str           # URL that produced units last time
│   ├── availability_links: list[str]   # All links that led to availability data
│   ├── explored_links: list[str]       # Links explored that had no data (skip next run)
│   ├── requires_interaction: list[ExpanderAction]
│   ├── timeout_ms: int
│   └── block_resource_domains: list[str]
│
├── api_hints: ApiHints
│   ├── known_endpoints: list[ApiEndpoint]
│   │   └── url_pattern, json_paths, provider
│   ├── widget_endpoints: list[str]     # Entrata widget URLs with data
│   ├── api_provider: str               # Detected PMS platform
│   ├── blocked_endpoints: list[BlockedEndpoint]   # Per-property noise blocklist
│   │   └── url_pattern, reason, blocked_at, attempts
│   └── llm_field_mappings: list[LlmFieldMapping]  # Saved for deterministic replay
│       └── api_url_pattern, json_paths, response_envelope, success_count
│
├── dom_hints: DomHints
│   ├── platform_detected: str          # entrata, rentcafe, appfolio, etc.
│   ├── field_selectors: FieldSelectorMap
│   │   └── container, unit_id, rent, sqft, bedrooms, bathrooms, availability_date, floor_plan_name
│   ├── jsonld_present: bool
│   └── availability_page_sections: list[str]  # CSS selectors for unit sections
│
├── confidence: ExtractionConfidence
│   ├── preferred_tier: int (1-5)
│   ├── last_success_tier: int
│   ├── consecutive_successes: int      # Promotes maturity at 3+
│   ├── consecutive_failures: int       # Demotes at 3+
│   ├── last_unit_count: int
│   └── maturity: ProfileMaturity (COLD | WARM | HOT)
│
├── llm_artifacts: LlmArtifacts
│   ├── extraction_prompt_hash: str
│   ├── field_mapping_notes: str
│   ├── api_schema_signature: str
│   ├── dom_structure_hash: str
│   └── last_api_analysis_results: dict[str, str]  # API URL -> "has_units"|"noise"
│
└── cluster_id: str (optional, for cross-property learning — not yet implemented)
```

### BlockedEndpoint — per-property noise learning

When the LLM (Phase 5) analyzes an API response and determines it has no unit data, the URL is saved as a `BlockedEndpoint` with the reason (e.g., "chatbot_config", "analytics_pixel", "cms_gallery_widget"). On subsequent runs, Phase 2 filters these out before any extraction is attempted.

```python
class BlockedEndpoint(BaseModel):
    url_pattern: str         # The API URL to block
    reason: str              # LLM-provided classification
    blocked_at: datetime     # When it was blocked
    attempts: int = 1        # Incremented on re-encounter (max 50 entries)
```

### LlmFieldMapping — deterministic replay without LLM

When the LLM successfully extracts units from an API response (Phase 5), it also provides the `json_paths` mapping (which JSON keys map to which unit fields) and the `response_envelope` (path to the unit list in the JSON structure). This mapping is saved and replayed deterministically on future runs via `apply_saved_mapping()` — no LLM call needed.

```python
class LlmFieldMapping(BaseModel):
    api_url_pattern: str               # The API URL this mapping applies to
    json_paths: dict[str, str]         # field -> key name, e.g. {"rent_low": "minRent"}
    response_envelope: str             # e.g., "data.results.units"
    discovered_at: datetime
    success_count: int = 0             # Incremented on each successful replay (max 20 entries)
```

**Example flow**:
1. Run 1 (COLD profile): Phase 5 LLM analyzes `https://example.com/api/v1/units` and extracts 45 units. Returns `json_paths: {"rent_low": "minRent", "unit_id": "unitNumber", ...}`, `response_envelope: "data.units"`. Saved to profile.
2. Run 2 (WARM profile): Phase 3 sees the same API URL was captured. Calls `apply_saved_mapping()` with the saved mapping. Extracts 45 units deterministically. No LLM call, no cost.

### Profile maturity and routing

**Maturity levels** (`services/profile_router.py`):

| Maturity | Trigger | Behavior |
|---|---|---|
| COLD | New property, or 3+ consecutive failures | Full 7-phase cascade, no shortcuts |
| WARM | 1+ successful extraction | Try `preferred_tier` first, then cascade on failure |
| HOT | 3+ consecutive successes | Skip directly to `preferred_tier`, no fallback cascade |

**Profile routing in `scrape()`**:
- COLD: all phases run in order
- WARM: Phase 3 tries profile-learned patterns first, falls through to Phase 4+ on failure
- HOT: jumps directly to the known-good tier (e.g., if `preferred_tier=1`, only checks API interception)

### Profile update flow (`services/profile_updater.py`)

After every scrape, `update_profile_after_extraction()` is called with the scrape result:

**On successful extraction:**
- Records `winning_page_url` and `availability_page_path`
- Records API URLs that had data as `known_endpoints`
- Records `llm_field_mappings` from Phase 5 analysis
- Records `availability_links` (pages that had data)
- Increments `consecutive_successes`, resets `consecutive_failures`
- Promotes maturity: COLD → WARM (1 success), WARM → HOT (3 consecutive)
- Updates `preferred_tier` (prefers lower tiers that work)

**On failed extraction:**
- Records `blocked_endpoints` with LLM-provided reasons
- Records `explored_links` that had no data (skipped on next run)
- Increments `consecutive_failures`, resets `consecutive_successes`
- Demotes maturity after 3 consecutive failures

**Drift detection** (`services/drift_detector.py`):
- Unit count drops >30% from expected → demotion
- All rents null → severe demotion to COLD
- 3+ consecutive timeouts → demotion

### Profile storage (`services/profile_store.py`)

- Profiles stored at `config/profiles/{canonical_id}.json`
- Audit copies at `config/profiles/_audit/{canonical_id}_{version}.json`
- `bootstrap_from_meta()` creates a COLD profile from CSV metadata + URL-based PMS detection
- All new fields have defaults — existing profiles deserialize without breaking

### LLM prompt templates

Two targeted prompts replace the old "send entire page" approach:

**`config/prompts/api_analysis.txt`** — Used in Phase 5
- Input: ONE API response body + property context
- Output: `has_unit_data`, `data_type`, `noise_reason`, `units[]`, `json_paths{}`, `response_envelope`
- Purpose: classify API as units/noise, extract data, AND provide deterministic mapping for replay

**`config/prompts/dom_analysis.txt`** — Used in Phase 6a
- Input: DOM section HTML (~20KB cap, not full page) + property context
- Output: `units[]`, `css_selectors{}` (container, rent, sqft, etc.)
- Purpose: extract units AND provide CSS selectors for deterministic replay

**`config/prompts/tier4_extraction.txt`** — Used in Phase 6b (legacy fallback)
- Input: trimmed page HTML + top 3 ranked API responses + property context
- Output: `units[]`, `profile_hints{}` (api_urls, json_paths, css_selectors, platform_guess)
- Purpose: broad extraction when targeted approaches fail

---

## Adding a new PMS platform

1. **API patterns**: Add URL match patterns to `ENTRATA_API_PATTERNS` in `entrata.py`. If the platform has a unique API structure (like SightMap), add a dedicated parser function alongside `_parse_sightmap_payload`.

2. **DOM selectors**: Add platform-specific container selectors to `CONTAINER_SELECTORS` in `parse_dom()`. Place them before the generic selectors.

3. **Priority paths**: Add platform-specific subpage paths to `ENTRATA_PRIORITY_PATHS` (e.g., `/floor-plans`, `/availability`).

4. **Expand buttons**: Add button text patterns to `EXPAND_BUTTON_PATTERNS` if the platform uses custom button labels to reveal units.

5. **Test**: Run against a real property URL:
   ```bash
   python scripts/entrata.py --url https://newplatform-property.com
   ```

6. **(Optional) Phase A templates**: If you also want the `run_phase_a.py` pipeline to handle the new platform, add a template in `ma_poc/templates/` and register it in `extraction/tier3_templates.py`.

---

## Common operations

```bash
# Full daily run (all properties)
python scripts/daily_runner.py --csv config/properties.csv

# Test with N properties
python scripts/daily_runner.py --csv config/properties.csv --limit 5

# Resume from row 10
python scripts/daily_runner.py --start-at 10

# Scrape a single property (debug)
python scripts/entrata.py --url https://property-website.com

# With proxy
python scripts/daily_runner.py --proxy http://user:pass@host:port

# Override run date (backfill)
python scripts/daily_runner.py --run-date 2026-04-12
```

---

## Concurrency (concurrency.py)

`daily_runner.py` and `retry_runner.py` scrape properties concurrently using `ThreadPoolExecutor` from `concurrent.futures`. The pipeline is split into three phases:

1. **Pre-filter (sequential)** — handles unresolved identities and duplicate canonical_ids immediately, without launching a browser.
2. **Concurrent scraping (thread pool)** — all scrapeable properties are dispatched to a `ThreadPoolExecutor` via `loop.run_in_executor()`. Each thread gets its own `asyncio` event loop and Playwright instance for true OS-level parallelism. Pool size is auto-detected by `concurrency.SystemResources`.
3. **Sequential post-processing** — state mutations (upsert, diff, carry-forward, record building) run sequentially because `StateStore` is not thread-safe.

**Why threads, not async**: `AsyncPool` (semaphore + gather) runs all scrapes in a single OS thread. Playwright browser launches, DNS resolution, and synchronous parsing block the shared event loop, serializing scrapes in practice. `ThreadPoolExecutor` gives each scrape its own thread and event loop — true parallelism.

### Auto-sizing

`SystemResources.detect()` reads CPU count and available RAM (Windows via `GlobalMemoryStatusEx`, Linux via `/proc/meminfo`, macOS via `sysctl`). The pool size is the **minimum** of three constraints:

| Constraint | Formula | Example (8-core, 2.7GB available) |
|---|---|---|
| RAM-based | `available_RAM × 70% / 250MB per browser` | 7 |
| CPU-based | `cpu_count × 2` (I/O-bound heuristic) | 16 |
| Environment cap | `MAX_CONCURRENT_BROWSERS` env var | 32 (default) |

Result is clamped to `[1, 32]`. To override auto-detection, set `MAX_CONCURRENT_BROWSERS` in `.env`.

### Two pool strategies

| Strategy | Class | Use case |
|---|---|---|
| `AsyncPool` | Semaphore + `asyncio.gather` | I/O-bound Playwright scraping inside a running event loop (used by `daily_runner.py`) |
| `ThreadedPool` | `ThreadPoolExecutor` | Sync callers or CPU-bound post-processing; each thread can optionally spin up its own event loop via `map_async()` |

### Usage

```python
from concurrency import SystemResources, AsyncPool, ThreadedPool, run_concurrent_scrapes

# Auto-detect and run (high-level helper)
results = await run_concurrent_scrapes(scrape_fn, [(url1,), (url2,), ...])

# Manual control
res = SystemResources.detect()
pool = AsyncPool(res.optimal_pool_size())
results = await pool.map(scrape_fn, [(url1,), (url2,), ...])
```

Exceptions are caught per-task and returned inline (never crash the batch). Progress is logged every 10%.

---

## Scraping failure modes and fixes (2026-04-13)

Analysis of the first 78-property production run revealed five failure categories. Each is documented here with root cause and fix so the same mistakes are not repeated.

### 1. Timeout (40% of properties) — sub-page crawl loop

**Root cause**: `entrata.py` BFS-crawled up to `MAX_CRAWL_PAGES = 40` sub-pages, each with a 45s page.goto timeout + 1.5s sleep. On slow sites this easily exceeded the 180s per-property timeout, even when Tier 1 API interception had already captured all the unit data from the homepage.

**Fixes applied**:
- Reduced `MAX_CRAWL_PAGES` from 40 → 10 (most data comes from homepage API capture)
- Added **early-exit**: if any homepage API response contains unit/floorplan signal keys, skip sub-page crawling entirely (`_response_looks_like_units()`)
- Reduced sub-page timeout from 45s → 20s (homepage keeps 45s)

**Lesson**: Always check if data is already available before doing more work. The BFS crawl was designed for sites without APIs, but it ran unconditionally on ALL sites including those where Tier 1 already had full data.

### 2. False-positive API interceptions (noise in 21% of properties)

**Root cause**: `looks_like_availability_api()` matched any URL containing `/api/`, `/units/`, etc. This captured Google Maps, analytics pixels, tag managers, and CMS widget endpoints that contain zero apartment data.

**Fixes applied**:
- Added `_FALSE_POSITIVE_HOSTS` blocklist: googleapis.com, go-mpulse.net, visitor-analytics.io, googletagmanager.com, doubleclick.net, facebook.com, hotjar.com, sentry.io
- Added `_FALSE_POSITIVE_PATH_FRAGMENTS` blocklist: `/tag-manager/`, `/mapsjs/`, `/gen_204`, `/analytics/`, `/gtag/`, `/pixel`, `/beacon`

**Lesson**: Broad URL pattern matching needs a deny-list for known non-property hosts. When adding new API patterns, always test against a diverse property set to check for false positives.

### 3. Narrow unit ID and rent key recognition (missed 16 properties)

**Root cause**: `_UNIT_ID_KEYS` only contained `unit_number`, `unitNumber`, `unit_id`, `unitId`, `UnitNumber`. Many PMS APIs (ResMan, Yardi, custom) use plain `id`, `label`, or `name` as unit identifiers. Similarly, `_RENT_KEYS` only matched flat scalar keys, but some APIs (ResMan) nest rent inside an object: `rent: {min: 1351, max: 1351}`, or a list: `rentTerms: [{rent: 1200, term: 12}]`.

**Fixes applied**:
- Extended `_UNIT_ID_KEYS` with: `id`, `label`, `name`, `ID`, `unit_name`, `unitName`
- Extended `_RENT_KEYS` with: `rentTerms`, `pricing`, `market_rent`
- Added `_extract_rent()` helper that handles flat scalars, nested dicts (`rent.min`/`rent.max`), nested lists (`rentTerms[].rent`), and nested objects (`pricing.effectiveRent`)
- Updated `_get()` in `entrata.py` to unwrap nested dicts for rent/sqft fields

**Lesson**: Never assume all PMS APIs use the same key naming convention. The generic parser gate (requires BOTH an id key AND a rent key in the same list item) is a good filter, but the key sets must be broad enough to cover real-world API schemas. Test against captured `raw_api/` bodies when adding new patterns.

### 4. HTTP → HTTPS redirect stalls (6 timeout properties)

**Root cause**: 6 properties in `properties.csv` used `http://` URLs. Most property sites support HTTPS but the plain HTTP → HTTPS redirect wastes 3-5 seconds per page or hangs entirely when the server forces HSTS with a slow redirect chain.

**Fix applied**: `scrape()` now normalizes `http://` → `https://` at the top of the function before any network calls.

**Lesson**: Always normalize URLs to HTTPS before scraping. If a site genuinely doesn't support HTTPS (rare), it will fail fast with a connection error, which is better than a silent 180s timeout from a redirect loop.

### 5. RealPage API structure not handled

**Root cause**: RealPage (`api.ws.realpage.com`) uses a two-endpoint pattern: `/floorplans` returns `{response: {floorplans: [...]}}` and `/units` returns `{response: [...]}`. The `/units` endpoint can return `null` when no units are available. The generic parser couldn't unwrap this nesting, and the existing parsers didn't recognize the RealPage host.

**Fix applied**: Added `_realpage_units_from_body()` dedicated parser in `scrape_properties.py` that handles both endpoints. When `/units` is null, floorplan-level records are still emitted (beds, baths, sqft — no rent). Wired into `transform_units_from_scrape()` alongside SightMap as a host-specific authoritative parser.

**Lesson**: When a new PMS platform is discovered in `raw_api/` captures, add a dedicated parser rather than stretching the generic one. Dedicated parsers are more reliable and easier to debug. Check for split-endpoint patterns (floorplans + units as separate calls).

### 6. Pipeline errors (5 properties — data quality)

| Error | Cause | Action |
|---|---|---|
| `ERR_SSL_PROTOCOL_ERROR` | Broken SSL certificate | Flag in CSV |
| `ERR_CONNECTION_TIMED_OUT` | Site down or blocking | Flag in CSV |
| `ERR_TOO_MANY_REDIRECTS` | Redirect loop | Flag in CSV |
| `ERR_NAME_NOT_RESOLVED` | Domain doesn't exist | Remove from CSV |

**Lesson**: These are input data problems, not code bugs. Periodically validate `properties.csv` URLs to prune dead/broken sites before scraping.

### 7. Noise-only API captures + all tiers failing (18 properties, 2026-04-13)

**Root cause**: 18 properties had raw API captures but 0 extracted units. Analysis revealed the captured APIs were all noise — chatbot configs (EliseAI, Sierra), CMS widgets (Entrata directions/gallery/amenities widgets), accessibility tools (UserWay), lead forms (G5 Marketing Cloud, Rentgrata), analytics (Wix tag manager), and Google Maps CSP tests. No actual floor plan / unit data was intercepted, and Tiers 2 (JSON-LD) and 3 (DOM) also found nothing.

Five sub-categories:
- **Entrata CMS widgets** (6): Only non-floor-plan widgets captured (directions, gallery, amenities). Floor plan data loads via a different mechanism.
- **Chatbot/leasing assistants** (4): EliseAI, Nestio, ConversionCloud, Sierra chat configs.
- **Wix sites** (3): Only tag-manager and analytics configs. Data is in static HTML.
- **Maps-only** (3): Only Google Maps gen_204 CSP test captured. SSR or inline JS data.
- **G5/accessibility widgets** (2): Lead forms, reviews, UserWay configs.

**Fixes applied**:
- Expanded `_FALSE_POSITIVE_HOSTS` with 13 new domains: meetelise.com, sierra.chat, theconversioncloud.com, nestiolistings.com, rentgrata.com, g5marketingcloud.com, userway.org, omni.cafe, comms.entrata.com
- Expanded `_FALSE_POSITIVE_PATH_FRAGMENTS` with 8 new patterns: `/apartments/module/widgets/`, Entrata chat endpoints, `/tour/availabilities`, `/html_forms/`, `/yext_reviews/`, `/blurb/v1/`
- Added **Tier 1.5 (Embedded JSON)**: Extracts data from inline `<script>` tags and JS globals (window.__NEXT_DATA__, floorPlans, unitData, etc.) — catches SSR sites that embed data in the page rather than fetching via XHR
- Added **Tier 4 (Entrata API probe)**: Detects Entrata-hosted sites and tries known API endpoints (GET/POST to /api/v1/floorplans/, /api/v1/propertyunits/) using the browser's session cookies
- Added **Tier 5 (Leasing portal detection)**: Detects iframes and redirect targets pointing to leasing portals (SightMap, RealPage OLL, RentCafe), navigates into them, and re-runs the full extraction stack
- Added **redirect capture**: When floor-plan page navigation is "interrupted by another navigation" to a leasing portal, follows the redirect instead of treating it as an error

**Lesson**: API interception only works for sites that load unit data via XHR/fetch. Sites using SSR, inline JS, Entrata's widget system, or embedded leasing portals need alternative extraction paths. Always check `raw_api/` to distinguish "parser bug" (data present but unparsed) from "no data captured" (need a different extraction mechanism).

---

## Known limitations and future work

- **External-source fields are always null**: 14 fields (Census Block, Tract Code, Construction dates, Market/Submarket names, Asset Grades, etc.) require external APIs. Phase B scope.
- **Amenities extraction**: Not implemented. SightMap stores amenities as filter IDs only; other platforms embed them in free-form text.
- **Effective rent / concession calculation**: Unit-level `concessions` field captures raw text from the website. Computing `effective_rent` (asking_rent minus concession value) is Phase B PR-07.
- **Geo-based timezone for LEASE_UP scheduling**: Only implemented in `run_phase_a.py` via state-based approximation. `daily_runner.py` does not handle LEASE_UP multi-scrape schedules.
- **StateStore is not concurrent**: Post-processing (state upsert, diff, carry-forward) runs sequentially after all scrapes complete. Making StateStore thread-safe would allow fully pipelined processing.
- **No cross-property learning**: Profiles are per-property only. Sites with identical structure (same PMS, same template) each learn independently. The `cluster_id` field exists on `ScrapeProfile` but clustering logic is not implemented.
- **LLM field mapping drift**: If a PMS API changes its response schema between runs, a saved `LlmFieldMapping` will fail to produce units. The mapping falls through to `parse_api_responses()` in that case, but the stale mapping is not automatically cleared — the drift detector handles this via unit-count-drop detection.

---

## Jugnu Architecture — Detailed Reference

The Jugnu pipeline (`jugnu_runner.py`) reorganises the system into 5 horizontal layers with frozen dataclass contracts between them. This section documents the layer contracts, invariants, and operational details.

### Cross-layer contracts

All inter-layer data flows through frozen dataclasses defined in each layer's `contracts.py`:

| Contract | Source | Fields |
|---|---|---|
| `FetchResult` | `ma_poc.fetch.contracts` | `url`, `outcome` (OK/NOT_MODIFIED/BOT_BLOCKED/RATE_LIMITED/TRANSIENT/HARD_FAIL/PROXY_ERROR), `status_code`, `headers`, `body`, `elapsed_ms`, `render_mode`, `identity_key`, `proxy_label`, `error_signature` |
| `CrawlTask` | `ma_poc.discovery.contracts` | `property_id`, `url`, `reason` (SCHEDULED/CARRY_FORWARD_CHECK/RETRY/SITEMAP_DISCOVERED/DLQ_REVIVE/MANUAL), `render_mode`, `priority` |
| `ExtractResult` | `ma_poc.pms.contracts` | `records` (list of dicts), `tier_used`, `llm_cost_usd`, `vision_cost_usd`, `llm_calls`, `vision_calls`, `errors` |
| `ValidatedRecords` | `ma_poc.validation.contracts` | `accepted` (list), `rejected` (list with reasons), `flagged` (list with flags), `next_tier_requested` (bool) |
| `Event` | `ma_poc.observability.events` | `kind` (81 types), `property_id`, `run_id`, `ts`, `payload` dict |

### Layer invariants

**L1 Fetch**: `fetch()` never raises. Returns `FetchResult` with `outcome` indicating success/failure type. Rate limiter is async token bucket per host. Proxy pool uses health scoring (success +0.05, failure -0.25, quarantine at <0.25). Conditional cache stores ETag/Last-Modified in SQLite with 7-day expiry.

**L2 Discovery**: Scheduler yields each URL at most once per run. Frontier deduplicates by URL. DLQ retries escalate from hourly to daily at the 6-hour mark. Carry-forward fires on fetch hard-fail, empty records, or validation reject.

**L3 Extraction**: `detect_pms()` never raises (fuzz-safe for None, "", binary input). `get_adapter()` never returns None — unknown PMS maps to GenericAdapter. LLM/Vision calls only happen inside GenericAdapter, never in PMS-specific adapters. `tier_used` follows `<adapter>:<tier_key>` format.

**L4 Validation**: Schema gate never raises on malformed input. Identity fallback uses `hashlib.sha256`, never `hash()`. Rent bounds reject negative and >$50K. Cross-run sanity flags but does not reject. `next_tier_requested` only when reject ratio strictly >50%.

**L5 Observability**: `emit()` never raises (swallows all exceptions). Event ledger is append-only; truncated lines from prior crashes are tolerated. Cost ledger is thread-safe with `threading.Lock`. All SQLite writes use threading locks.

### Running the Jugnu pipeline

```bash
# Full run
python scripts/jugnu_runner.py --csv config/properties.csv

# Limited test run
python scripts/jugnu_runner.py --csv config/properties.csv --limit 5

# Override date
python scripts/jugnu_runner.py --csv config/properties.csv --run-date 2026-04-18

# Specify data directory
python scripts/jugnu_runner.py --csv config/properties.csv --data-dir data
```

### Gate validation

```bash
# Check all phase gates
python scripts/gate_jugnu.py all

# Check specific phase (0-9)
python scripts/gate_jugnu.py phase 1

# Run pytest for a phase
python scripts/gate_jugnu.py tests 1
```

### Test suite (161 tests)

| Directory | Layer | Tests |
|---|---|---|
| `tests/fetch/` | L1 Fetch | 43 |
| `tests/discovery/` | L2 Discovery | 35 |
| `tests/pms/` | L3 Extraction | 4 |
| `tests/validation/` | L4 Validation | 30 |
| `tests/observability/` | L5 Observability | 19 |
| `tests/reporting/` | Reporting | 9 |
| `tests/baseline/` | J0 Baseline | 10 |

```bash
# All Jugnu tests
pytest tests/ -v --tb=short

# By layer
pytest tests/fetch/ -v
pytest tests/discovery/ -v
pytest tests/validation/ -v
pytest tests/observability/ -v
pytest tests/reporting/ -v
```

### CSV input format

The Jugnu runner accepts flexible column names:

```
apartmentid,name,address,city,state,zip,website
67598,Lofts at Little Creek,123 Main St,Scottsdale,AZ,85255,http://www.example.com
```

Column mapping:
- `property_id` ← `property_id` | `Unique ID` | `Property ID` | `apartmentid`
- `url` ← `url` | `Website` | `website`

### Key differences from legacy pipeline

| Feature | Legacy (`daily_runner.py`) | Jugnu (`jugnu_runner.py`) |
|---|---|---|
| Fetch | Playwright directly in entrata.py | L1 Fetcher with proxy pool, rate limiter, stealth |
| Scheduling | Sequential CSV iteration | L2 Scheduler with frontier, DLQ, sitemap discovery |
| Extraction | 7-phase monolithic in entrata.py | L3 PMS detection → resolution → adapter dispatch |
| Validation | validation.py issue codes | L4 schema gate + identity fallback + cross-run sanity |
| Observability | scrape_events.jsonl | L5 event ledger + cost ledger + SLO watcher |
| Carry-forward | state_store.py | L2 carry_forward.py (checks fetch outcome first) |
| Error handling | try/except per property | Never-fail contract across all layers |
| State | JSON files (property_index, unit_index) | SQLite (frontier, cache, cost ledger) |
| Reports | report.json/md | report.json/md + SLO section + per-property verdicts |

### Reporting key source of truth (2026-04-19 fix)

`ma_poc/reporting/run_report.py` and `ma_poc/observability/slo_watcher.py`
both read the outcome from **`_meta.verdict`** (SUCCESS /
FAILED_UNREACHABLE / FAILED_NO_DATA) and the tier from
**`_extract_result.tier_used`**.

Previously they read `_meta.scrape_tier_used`, which only the legacy
pipeline writes. Jugnu runs therefore showed `tier=UNKNOWN`,
`failed=0`, and `success_rate observed=0.0000` alongside
`success_rate=100%` — the reporting values contradicted each other. If
you rename or move these keys, update both files together and keep the
integration test in `ma_poc/tests/observability/test_slo_watcher.py`
asserting against a Jugnu-shaped record.

---

## Postgres retention (2026-05-02)

`sync_run_to_pg.py :: _apply_retention(engine)` runs as the last step of
every sync (via `_run_stage2("retention", ...)`) and enforces a
**3-day rolling window** on every per-run table. Upsert-only tables
hold current state and are never trimmed.

| Tier | Tables | Cutoff |
|---|---|---|
| 3-day rolling | `llm_reports`, `llm_diagnostics`, `llm_property_details`, `property_snapshots`, `run_issues`, `run_ledger` | `run_date < today - 3 days` |
| 3-day rolling | `scrape_events` | `scrape_timestamp < now() - 3 days` |
| 3-day rolling | `dlq_entries` | `parked_at < now() - 3 days` |
| Upsert only | `properties`, `units`, `scrape_profiles` | never trimmed |

When adding a new per-run table, add it to `_apply_retention()` as part of
the same change — never reintroduce historical snapshots. The retention
sweep is idempotent and shard-safe (every shard observes the same
wall-clock cutoff).

Read-side consequence: API routes that take a `:date` param respond with
**HTTP 410 Gone** + `{ status: "purged", retentionDays: 3 }` for any date
older than the window. The cutoff is mirrored in
`ma_poc/frontend/api/src/middleware/retention.ts` and must stay in
lock-step with the sweep's 3-day window.

Backfill caveat: `daily_runner --run-date <older-than-3-days>` will write
rows that the same sync immediately deletes. Backfills should be served
from GCS-archived per-run dirs, not re-synced into DB.
