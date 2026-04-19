# PropAi Scripts — Implementation Guide

## What this directory is

`scripts/` contains the **production scraping pipeline** — the system that actually runs against live property websites, extracts unit data, tracks state across daily runs, and produces the 46-key property output schema.

---

## Architecture overview

The platform has **two pipeline implementations**. The Jugnu pipeline is the recommended architecture going forward.

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
    |   +-- events.py         # 28 event types, emit() with buffered ledger backend
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

### Legacy Pipeline (`daily_runner.py`) — 7-Phase Extraction

```
CSV input
    |
    v
daily_runner.py          # Orchestrator: loads CSV, resolves identity, runs pipeline,
    |                    # diffs against prior state, writes output + report
    |
    +-- concurrency.py   # System resource detection + concurrent pool management
    |
    +-- identity.py      # 5-tier canonical ID resolution (dedup across runs)
    |
    +-- entrata.py       # Core scraper engine (handles ALL platforms)
    |     +-- Phase 1: Homepage load + full network capture
    |     +-- Phase 2: Noise filtering (global + profile-specific blocklists)
    |     +-- Phase 3: Known pattern extraction (profile mappings → API → JSON-LD → DOM)
    |     +-- Phase 4: Link-by-link exploration with per-page network observation
    |     +-- Phase 5: LLM-assisted API analysis (single API at a time, max 3 calls)
    |     +-- Phase 6: DOM fallback with targeted LLM → legacy LLM → Vision LLM
    |     +-- Phase 7: Availability defaults + profile learning persistence
    |
    +-- services/
    |     +-- profile_store.py, profile_router.py, profile_updater.py
    |     +-- drift_detector.py, llm_extractor.py, vision_extractor.py
    |
    +-- scrape_properties.py   # Unit transformation: raw API bodies -> target schema
    +-- state_store.py         # Persistent JSON state: property_index + unit_index
    +-- validation.py          # Structured issue logging (ERROR/WARNING/INFO + codes)
    |
    +-- Output:
          data/runs/{date}/properties.json   # 46-key records
          data/runs/{date}/report.json/md    # Run summary
          data/runs/{date}/issues.jsonl      # Validation issues
          data/state/property_index.json     # Persisted between runs
          data/state/unit_index.json         # Unit history with diffs
          config/profiles/{canonical_id}.json # Per-property learned extraction profiles
```

---

## The three pipeline paths (and when to use which)

### `jugnu_runner.py` — Jugnu pipeline (RECOMMENDED)

The 5-layer architecture with hard contracts between layers. Uses L2 Scheduler for task generation, L1 Fetcher for stealth requests, L3 PMS adapters for extraction, L4 for validation, and L5 for observability. Produces per-property verdicts and run reports with SLO monitoring.

```bash
python scripts/jugnu_runner.py --csv config/properties.csv
python scripts/jugnu_runner.py --csv config/properties.csv --limit 5
python scripts/jugnu_runner.py --csv config/properties.csv --run-date 2026-04-18
```

### `daily_runner.py` — Legacy pipeline

The full pipeline with state tracking, identity resolution, carry-forward, validation, and the 46-key output schema. Uses `entrata.py` as its scraping engine. **Scraping runs concurrently** via `concurrency.py` — the pool size is auto-detected from system resources.

```bash
python scripts/daily_runner.py --csv config/properties.csv
python scripts/daily_runner.py --csv config/properties.csv --limit 5
python scripts/daily_runner.py --proxy http://user:pass@host:port
```

### `scrape_properties.py` — Simpler batch scraper (no state tracking)

Reads CSV, scrapes each property, writes a single `output/properties.json`. No state persistence, no identity resolution, no carry-forward. Useful for one-off runs or debugging.

```bash
python scripts/scrape_properties.py --csv config/properties.csv --out output/properties.json
```

---

## 7-Phase Extraction Pipeline (entrata.py)

`entrata.py` is the core scraping engine. Despite the filename, it handles **all** multifamily property websites — Entrata, RentCafe, AppFolio, Yardi, custom sites. The pipeline uses a 7-phase approach that is **exploratory and self-learning**: it navigates links systematically, observes network calls per page, uses LLM surgically on individual API responses, and persists what works (and what doesn't) to per-property profiles.

### Phase 1 — Homepage Load + Full Network Capture

- Launches Playwright Chromium with `page.on("response")` handler registered **before** `page.goto()`
- Captures all XHR/fetch responses matching URL patterns (`/api/`, `/availabilities`, `/floor-plans`, `/pricing`, `/units`, etc.)
- Filters out known false-positive hosts (googleapis.com, hotjar.com, sentry.io, meetelise.com, etc.) and path fragments (`/analytics/`, `/beacon`, `/tag-manager/`)
- Collects all internal links with anchor text for later exploration
- Extracts property metadata (name, address, geo, phone) from the homepage

### Phase 2 — Noise Filtering

Three-layer filtering of captured API responses:

1. **Global blocklists** — `_FALSE_POSITIVE_HOSTS` (30+ domains) and `_FALSE_POSITIVE_PATH_FRAGMENTS` (15+ patterns)
2. **Profile-specific blocklist** — `profile.api_hints.blocked_endpoints` (learned from past runs where LLM identified an API as noise)
3. **Content-type filter** — only JSON responses are kept

Links are prioritized using `prioritize_links()`:
1. Profile `winning_page_url` and `availability_links` (known working pages)
2. Anchor text matches ("View Availability", "See Floor Plans", etc.)
3. URL keyword matches (`/floor-plans`, `/apartments`, `/availability`)
4. Exploratory candidates (excludes `profile.navigation.explored_links` that had no data)

### Phase 3 — Known Pattern Extraction

Tries extraction on homepage data using all deterministic methods. First hit with units wins:

1. **Profile LLM field mappings** — replays saved `LlmFieldMapping` json_paths against matching API URLs. No LLM call needed — deterministic extraction from a mapping learned on a prior run.
2. **Profile known endpoints** — checks if any `profile.api_hints.known_endpoints` URL was captured
3. **Global API pattern match** — `parse_api_responses()` with 50+ key name variants, SightMap/RealPage dedicated parsers
4. **Embedded JSON (Tier 1.5)** — SSR data in `<script>` tags, JS globals (`__NEXT_DATA__`, `floorPlans`, etc.)
5. **JSON-LD (Tier 2)** — `<script type="application/ld+json">` blocks (Apartment, ApartmentComplex, Offer schemas)
6. **DOM parsing (Tier 3)** — CSS selector cascade + regex extraction, with profile-learned selectors tried first

If units found here, skips directly to Phase 7.

### Phase 4 — Link-by-Link Exploration with Network Observation

Key behavioral change from the old BFS crawl: instead of visiting all links and checking the cumulative API response list, we **observe which new APIs fire per page navigation**.

For each link in the prioritized queue (capped at `MAX_CRAWL_PAGES=10`):
1. Record `api_count_before` — snapshot the API response list length
2. Navigate to the link, click expanders, wait 1.5s
3. Identify `new_responses = api_responses[api_count_before:]` — only APIs triggered by this page
4. Filter new responses through the profile blocklist
5. Try deterministic extraction (API parser, embedded JSON, JSON-LD, DOM) on this page
6. If promising APIs found but parser failed → collect as `llm_candidates` for Phase 5
7. Also tries Entrata API probe and leasing portal detection on each page

Tracks which links had data vs. didn't — this is persisted to the profile after the run.

### Phase 5 — LLM-Assisted API Analysis (max 3 calls per property)

Analyzes promising but unparsed API responses **one at a time** (not batched with HTML):

1. Sends a SINGLE API response body (~30KB cap) to LLM using `config/prompts/api_analysis.txt`
2. LLM determines: has_unit_data (bool), data_type, noise_reason
3. If units found: extracts them AND provides `json_paths` + `response_envelope` mapping
4. The mapping is saved as `LlmFieldMapping` in the profile — on the next run, this API is extracted deterministically without any LLM call
5. If LLM says no unit data: the API URL is added to `profile.api_hints.blocked_endpoints` with the LLM-provided reason — never analyzed again

**LLM budget**: max 3 API analysis calls + 1 DOM analysis call per property per run.

### Phase 6 — DOM Fallback with Targeted LLM → Legacy LLM → Vision

Three fallback stages if all APIs failed:

**6a — Targeted DOM LLM**: Finds DOM sections with rent signals (`$XXX` patterns, 2+ price matches). Extracts just the relevant container HTML (~20KB, not full page). Sends to LLM using `config/prompts/dom_analysis.txt`. LLM returns units AND CSS selectors, which are saved to the profile.

**6b — Legacy LLM**: Falls back to the original approach — sends trimmed page HTML + top 3 ranked API responses as combined input. Less targeted but catches cases the new approach misses.

**6c — Vision LLM**: Screenshot-based extraction as absolute last resort. Full-page screenshot sent to GPT-4o (Azure) or Claude Sonnet (Anthropic).

### Phase 7 — Finalization + Profile Learning

**Availability defaults**: If units/floor plans are found but have no availability data:
- `availability_status` → `"AVAILABLE"` (if a unit is listed on the site, it's available)
- `availability_date` → today's date

**Profile learning data** attached to scrape results for `profile_updater.py`:
- `_llm_analysis_results`: API URL → `LlmFieldMapping` dict (if units found) or `"noise:reason"` (if blocked)
- `_explored_links`: link → True/False (had data or not)
- `_winning_page_url`: the URL/API that produced the winning data
- `_llm_hints`: CSS selectors or json_paths from LLM analysis

### Why Phase 3 wins most of the time

Modern multifamily websites (Entrata, RentCafe, AppFolio, Yardi) are SPAs that load unit data via API calls. Phase 3 captures this directly from the network — no link exploration or LLM needed. For WARM/HOT profiles, a saved `LlmFieldMapping` or `known_endpoint` produces instant results without even running the generic parser.

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
| `Event` | `ma_poc.observability.events` | `kind` (28 types), `property_id`, `run_id`, `ts`, `payload` dict |

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
