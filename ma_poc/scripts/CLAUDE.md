# PropAi Scripts — Implementation Guide

## What this directory is

`scripts/` contains the **production scraping pipeline** — the system that actually runs against live property websites, extracts unit data, tracks state across daily runs, and produces the 46-key property output schema. This is separate from the `ma_poc/extraction/` tiered pipeline (which is the CLAUDE.md Phase A spec). Both systems exist; this one is what produces real output.

---

## Architecture overview

```
CSV input
    |
    v
daily_runner.py          # Orchestrator: loads CSV, resolves identity, runs pipeline,
    |                    # diffs against prior state, writes output + report
    |
    +-- concurrency.py   # System resource detection + concurrent pool management
    |                    # Auto-sizes worker pool from CPU cores + available RAM
    |
    +-- identity.py      # 5-tier canonical ID resolution (dedup across runs)
    |
    +-- entrata.py       # Core scraper engine (despite the name, handles ALL platforms)
    |     |
    |     +-- Tier 1: API interception (XHR/fetch capture during page load)
    |     +-- Tier 2: JSON-LD (Schema.org structured data)
    |     +-- Tier 3: DOM parsing (selector cascade + regex fallback)
    |     +-- Property metadata extraction (name, address, geo, phone)
    |
    +-- scrape_properties.py   # Unit transformation: raw API bodies -> target schema
    |                          # (unit_id, market_rent_low/high, available_date, ...)
    |
    +-- state_store.py   # Persistent JSON state: property_index + unit_index
    |                    # Tracks new/updated/unchanged/disappeared across runs
    |
    +-- validation.py    # Structured issue logging (ERROR/WARNING/INFO + codes)
    |
    +-- Output:
          data/runs/{date}/properties.json   # 46-key records
          data/runs/{date}/report.json       # run summary
          data/runs/{date}/report.md         # human-readable report
          data/runs/{date}/issues.jsonl      # validation issues
          data/state/property_index.json     # persisted between runs
          data/state/unit_index.json         # unit history with diffs
```

---

## The two pipeline paths (and when to use which)

### `daily_runner.py` — Production pipeline (USE THIS)

The full pipeline with state tracking, identity resolution, carry-forward, validation, and the 46-key output schema. Uses `entrata.py` as its scraping engine. **Scraping runs concurrently** via `concurrency.py` — the pool size is auto-detected from system resources (CPU cores, available RAM, `MAX_CONCURRENT_BROWSERS` env var).

```bash
python scripts/daily_runner.py --csv config/properties.csv
python scripts/daily_runner.py --csv config/properties.csv --limit 5
python scripts/daily_runner.py --proxy http://user:pass@host:port
```

### `run_phase_a.py` — BRD-spec Phase A pipeline

The CLAUDE.md-specified pipeline with change detection, 5-tier extraction, vision fallback, banner capture, accuracy sampling. Outputs a minimal 6-key per-property JSON. Used for Phase A acceptance gates, not for production output.

```bash
python scripts/run_phase_a.py
```

### `scrape_properties.py` — Simpler batch scraper (no state tracking)

Reads CSV, scrapes each property, writes a single `output/properties.json`. No state persistence, no identity resolution, no carry-forward. Useful for one-off runs or debugging.

```bash
python scripts/scrape_properties.py --csv config/properties.csv --out output/properties.json
```

---

## Extraction tiers (entrata.py)

`entrata.py` is the core scraping engine. Despite the filename, it handles **all** multifamily property websites — Entrata, RentCafe, AppFolio, Yardi, custom sites. The tiers run in priority order; first one that produces data wins.

### Tier 1 — API Interception (highest fidelity)

- Registers a Playwright `page.on("response")` handler **before** `page.goto()`
- Captures all XHR/fetch responses whose URLs match patterns: `/api/`, `/availabilities`, `/floor-plans`, `/pricing`, `/units`, `/apartments`, `floorplan`, `availability`, `getFloorPlans`, `getAvailabilities`, `propertyInfo`
- **SightMap dedicated parser**: Joins `data.units[]` to `data.floor_plans[]` by `floor_plan_id`. Extracts unit_number, price, sqft, availability, lease links, concessions
- **Generic API parser**: Unwraps nested JSON envelopes (`data.results.units`, `response.floorPlans`, etc.). Tries 50+ key name variants for each field. Deduplicates by unit_number or floor plan fingerprint
- Produces the richest data: individual unit IDs, exact rents, availability dates, lease links

### Tier 2 — JSON-LD / Schema.org

- Extracts `<script type="application/ld+json">` blocks from the page
- Walks `@graph`, `itemListElement`, nested structures recursively
- Targets: `Apartment`, `ApartmentComplex`, `Offer`, `FloorPlan`, `Residence`, `SingleFamilyResidence`
- Extracts: name, rent (from `offers.lowPrice`/`offers.highPrice`/`offers[].price`), sqft (from `floorSize`), numberOfRooms
- Typically yields floor-plan-level data (not individual units)

### Tier 3 — DOM Parsing (broadest coverage)

- CSS selector cascade from specific to generic:
  ```
  .fp-group -> .floorplan-item -> .floor-plan-card -> .fp-item
  -> [class*='FloorPlan'] -> [class*='floorplan']
  -> .apartment-item -> .unit-card -> .plan-card
  -> [data-floor-plan] -> [data-unit]
  -> article -> .card -> li
  ```
- Requires 2+ matching elements AND at least one containing a `$XXX` pattern (rent signal)
- Extracts fields via **regex on inner text** (not dependent on specific CSS classes):
  - Beds: `(\d+)\s*(?:bed|bd|bedroom)s?`
  - Baths: `(\d+)\s*(?:bath|ba)s?`
  - Sqft: `([\d,]+)\s*(?:sq ft|sqft|sf)`
  - Rent: `\$([\d,]+)(?:/mo)?(?:\s*[-–]\s*\$([\d,]+))?`
  - Unit number: `(?:unit|apt)\s*#?\s*([A-Z]?\d{2,4}[A-Z]?)`
  - Availability date: multiple formats (MM/DD/YYYY, month-name)
  - Concessions: `(\d+)\s+(?:week|month)s?\s+free`
- **Click-to-expand**: Before parsing, clicks buttons matching patterns like "available unit", "view unit", "show more", "view all" (restricted to `button`, `a`, `[role=button]`)

### Why Tier 1 wins most of the time

Modern multifamily websites (Entrata, RentCafe, AppFolio, Yardi) are SPAs that load unit data via API calls. The page DOM often shows summary cards, but the API response has the full unit-level detail. Tier 1 captures this directly from the network — no DOM parsing needed.

---

## Property metadata extraction

`extract_property_metadata()` in `entrata.py` runs on every page load and extracts:

| Field | Sources (priority order) |
|---|---|
| name | og:site_name, og:title, `<title>`, JSON-LD ApartmentComplex.name |
| address | JSON-LD PostalAddress.streetAddress |
| city/state/zip | JSON-LD PostalAddress fields |
| lat/lng | og:latitude/longitude, geo.position, ICBM meta, JSON-LD GeoCoordinates |
| phone | JSON-LD telephone, footer regex `(\d{3})\s*\d{3}\s*\d{4}` |
| total_units | JSON-LD numberOfRooms |

---

## Identity resolution (identity.py)

Each property needs a stable canonical ID so daily runs can diff against previous state. Five-tier cascade:

| Tier | Source | Confidence | Format |
|---|---|---|---|
| 1 | Unique ID column | 1.00 | Raw value |
| 2 | Property ID column | 0.95 | Raw value |
| 3 | Address fingerprint | 0.80 | `sha1(normalized_street\|city\|state\|zip5)` |
| 4 | Geo fingerprint | 0.65 | `geo_{lat:.4f}_{lng:.4f}` |
| 5 | Website host | 0.45 | `web_` + `sha1(normalized_host)` |

Address normalization: lowercases, strips punctuation, collapses street suffixes ("Street" -> "st", "Avenue" -> "ave"), removes unit/apt/suite numbers.

Duplicate detection runs across all rows before scraping:
- **Hard duplicates**: same canonical_id (ERROR)
- **Soft duplicates**: same address fingerprint, different canonical_id (WARNING)
- **Geo duplicates**: same lat/lng fingerprint, different canonical_id (WARNING)

---

## State tracking (state_store.py)

Two persistent JSON files in `data/state/`:

### property_index.json
```json
{
  "canonical_id": {
    "name": "...",
    "website": "...",
    "first_seen_date": "2026-04-10",
    "last_seen_date": "2026-04-13",
    "last_scrape_status": "SUCCESS",
    "last_units_count": 46
  }
}
```

### unit_index.json
```json
{
  "canonical_id": {
    "unit_id": {
      "market_rent_low": 2800,
      "market_rent_high": 2800,
      "available_date": "2026-05-12",
      "first_seen_date": "2026-04-10",
      "last_seen_date": "2026-04-13",
      "carryforward_days": 0,
      "changed_fields": ["market_rent_low"],
      "disappeared_since": null
    }
  }
}
```

### Daily diff logic
- **New unit**: unit_id not in prior index -> `diff.new`
- **Updated**: unit_id exists but rent/date/concessions changed -> `diff.updated` + `changed_fields` tracked
- **Unchanged**: same values as yesterday -> `diff.unchanged`
- **Disappeared**: in yesterday's index but missing from today's scrape -> `diff.disappeared`, flagged with `disappeared_since` date (not deleted, so it can be detected if it reappears)
- **Carry-forward**: if scrape fails for a known property, yesterday's units are copied with `carryforward_days += 1`

### Disappeared properties
After all rows are processed, any canonical_id that was in `property_index` yesterday but not in today's CSV is flagged as `PROPERTY_DISAPPEARED`.

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

---

## Validation (validation.py)

Every issue gets a severity + machine-readable code:

| Code | Severity | Meaning |
|---|---|---|
| `IDENTITY_UNRESOLVED` | ERROR | Cannot resolve any canonical ID |
| `IDENTITY_LOW_CONFIDENCE` | WARNING | Resolved via low-confidence tier (geo/website) |
| `DUPLICATE_IDENTITY` | ERROR | Two CSV rows map to the same canonical_id |
| `SCRAPE_FAILED` | WARNING | Scrape returned errors |
| `SCRAPE_TIMEOUT` | ERROR | Exceeded per-property timeout |
| `UNITS_EMPTY` | WARNING | Scrape succeeded but extracted 0 units |
| `UNIT_INVALID_RENT` | WARNING | Rent outside $200–$50,000 range |
| `UNITS_CARRIED_FORWARD` | INFO | Used yesterday's units because today failed |
| `UNITS_DISAPPEARED` | INFO | Units in prior state missing from today |
| `PROPERTY_DISAPPEARED` | WARNING | Property in state but not in today's CSV |
| `PROPERTY_NEW` | INFO | First time seeing this property |

All issues are written to `data/runs/{date}/issues.jsonl` and summarized in `report.json`.

---

## Implementation principles

### Never-fail contract
- Every scrape is wrapped in `try/except`; no single property can crash the run
- State file writes use atomic temp-file + `os.replace()` so a crash never corrupts state
- Incremental writes to `properties.json` after each property — an interrupted run still leaves a usable file

### Scraping resilience
- `networkidle` timeout is capped at 5 seconds (not blocking), fallback to `domcontentloaded`
- Click-to-expand is best-effort — exceptions are swallowed, scraping continues
- Sub-link discovery is BFS with a hard cap of 40 pages
- Per-property scrape timeout (default 180s) prevents stuck pages from hanging the run

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

---

## Known limitations and future work

- **External-source fields are always null**: 14 fields (Census Block, Tract Code, Construction dates, Market/Submarket names, Asset Grades, etc.) require external APIs. Phase B scope.
- **Amenities extraction**: Not implemented. SightMap stores amenities as filter IDs only; other platforms embed them in free-form text.
- **Effective rent / concession calculation**: Unit-level `concessions` field captures raw text from the website. Computing `effective_rent` (asking_rent minus concession value) is Phase B PR-07.
- **Geo-based timezone for LEASE_UP scheduling**: Only implemented in `run_phase_a.py` via state-based approximation. `daily_runner.py` does not handle LEASE_UP multi-scrape schedules.
- **StateStore is not concurrent**: Post-processing (state upsert, diff, carry-forward) runs sequentially after all scrapes complete. Making StateStore thread-safe would allow fully pipelined processing.
