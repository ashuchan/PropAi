# MA Rent Intelligence Platform — Phase A
**POC: 500 properties · 8 weeks · BRD v2.0 April 2, 2026**
**Phase A scope: Weeks 1–3 · PR-01 PR-02 PR-03 PR-04**

---

## Core question this implementation must answer

> Can this architecture take any multifamily property website — regardless of platform, anti-bot protection, or data presentation — and produce clean, accurate, unit-level rent and availability data, correctly matched to a stable unit identity, within 24 hours, at least 95% of the time?

---

## Mandatory development workflow — apply to every module, in order

Do not skip steps. Do not reorder steps. This workflow applies to every file in the project.

**STEP 1 — Read requirements first**
- Read the relevant PR section below before writing any code.
- Identify all acceptance criteria. Add them as a comment block at the top of the file before writing the implementation.
- Identify module dependencies (e.g. `tier1_api.py` depends on `BrowserSession` from `browser.py`). Implement dependencies first.
- All models in `models/` must be fully defined before any pipeline code is written.

**STEP 2 — Generate the full implementation**
- Write complete, working code. No `# TODO: implement` stubs. Every method must be functional.
- Every function/method: docstring specifying what it does, parameters, return type, exceptions raised.
- Use type annotations throughout. Pydantic v2 for all data structures. `async`/`await` for all I/O.
- Add inline comments mapping each code block to the BRD acceptance criterion it satisfies, e.g. `# PR-02 AC: skip if all mechanisms UNCHANGED`.

**STEP 3 — Write unit tests immediately after each module**
- Write the test file for the module you just implemented before moving to the next module.
- Meet or exceed the minimum test counts in the Test Suite section below.
- Cover: happy path, all failure/error paths, all edge cases in the implementation notes.
- Use `pytest` + `pytest-asyncio`. All async test functions need `@pytest.mark.asyncio`.
- Use `respx` for HTTP mocking. Use `pytest-mock` for OpenAI/Anthropic API mocking.
- Fixtures go in `tests/fixtures/`. Use realistic HTML — not lorem ipsum.

**STEP 4 — Run tests and fix failures before moving on**
- Run: `pytest tests/test_{module}.py -v --tb=short`
- ALL tests must pass before moving to the next module.
- Fix the implementation, not the test (unless the test itself is wrong).
- Run `pytest tests/ -v` at the end of each weekly gate to confirm no regressions.

**STEP 5 — Run static analysis after each module**
- Run: `ruff check {module_file}.py` — fix all E/F (error) level findings immediately.
- Run: `mypy {module_file}.py --strict` — fix all type errors before proceeding.
- Fix file-by-file as you go. Do not batch up static analysis errors.
- For third-party libraries without type stubs: add to `[[tool.mypy.overrides]]` in `pyproject.toml` with `ignore_missing_imports = true`. Do not lower the overall `--strict` setting.

**STEP 6 — Run integration validation after each weekly gate**
- After completing each weekly gate: run `python scripts/validate_outputs.py`
- After Week 3 gate: run `python scripts/smoke_test.py` (must pass 5/5)
- If either script reports failures, fix them before marking the gate passed.

**STEP 7 — Bug hunt after ALL modules complete**
- After all Phase A modules are implemented and all tests pass, execute the Bug Hunt section below.
- For each of the 15 bug categories: explicitly state `CHECKED — NOT PRESENT` or `CHECKED — FOUND AND FIXED: [description]`.
- Run the full test suite again after all bug fixes: `pytest . -v --ignore=data --ignore=config`
- Run `python scripts/smoke_test.py` one final time — must pass 5/5.
- Phase A is not complete until smoke_test passes 5/5 and all 15 categories are checked.

---

## Repository structure — create this before any implementation code

```
ma_poc/
├── config/
│   ├── properties.csv          # 500-property input list (provided by RealPage — Day 1 blocker)
│   ├── business_rules.yaml     # Stub now with defaults; Phase B fills with RealPage rules
│   ├── proxy.yaml              # Proxy credentials — env-injected, never commit
│   └── api_catalogue.json      # PMS API URL patterns — built by build_api_catalogue.py Week 1
├── scraper/
│   ├── __init__.py
│   ├── fleet.py                # PR-01: Async scraping coordinator
│   ├── browser.py              # Playwright browser session management
│   ├── change_detection.py     # PR-02: ETag / Sitemap / API hash gate
│   └── proxy_manager.py        # Residential proxy rotation
├── extraction/
│   ├── __init__.py
│   ├── tier1_api.py            # PR-03: API interception
│   ├── tier2_jsonld.py         # PR-03: JSON-LD / Schema.org
│   ├── tier3_templates.py      # PR-03: PMS template dispatcher
│   ├── tier4_llm.py            # PR-03: GPT-4o-mini via Azure OpenAI
│   ├── tier5_vision.py         # PR-04: Vision LLM provider abstraction + Tier 5 fallback
│   ├── vision_banner.py        # PR-04 Role B: Banner capture every property
│   ├── vision_sample.py        # PR-04 Role C: 5–10% daily accuracy sample
│   ├── pipeline.py             # Orchestrates Tiers 1–5 in sequence
│   └── confidence.py           # Composite confidence scoring
├── models/
│   ├── __init__.py
│   ├── unit_record.py          # UnitRecord — forward-compatible with Phase B PostgreSQL schema
│   ├── scrape_event.py         # ScrapeEvent dataclass
│   └── extraction_result.py    # ExtractionResult with tier + confidence
├── storage/
│   ├── __init__.py
│   └── event_log.py            # Append-only JSONL event logger
├── templates/
│   ├── __init__.py
│   ├── rentcafe.py             # Tier 3: RentCafe DOM selectors
│   ├── entrata.py              # Tier 3: Entrata DOM selectors
│   └── appfolio.py             # Tier 3: AppFolio DOM selectors
├── tests/
│   ├── conftest.py             # Shared pytest fixtures
│   ├── test_change_detection.py
│   ├── test_tier1_api.py
│   ├── test_tier2_jsonld.py
│   ├── test_tier3_templates.py
│   ├── test_tier4_llm.py
│   ├── test_tier5_vision.py
│   ├── test_pipeline.py
│   └── fixtures/
│       ├── rentcafe_sample.html
│       ├── entrata_sample.html
│       ├── appfolio_sample.html
│       ├── jsonld_sample.html
│       └── api_response_sample.json
├── scripts/
│   ├── daily_runner.py         # Production entrypoint: orchestrates CSV load, identity
│   │                           # resolution, scraping via entrata.py, state diffing, output
│   ├── entrata.py              # Core scraper engine: API interception, JSON-LD, DOM parsing
│   │                           # (handles ALL platforms despite the name)
│   ├── scrape_properties.py    # Simpler batch scraper (no state tracking)
│   ├── identity.py             # 5-tier canonical ID resolution (dedup across runs)
│   ├── state_store.py          # Persistent JSON state: property_index + unit_index
│   ├── validation.py           # Structured issue logging (ERROR/WARNING/INFO + codes)
│   ├── run_phase_a.py          # BRD-spec Phase A pipeline (not used in production)
│   ├── build_api_catalogue.py  # Week 1: discovers API patterns on 50-property seed set
│   ├── validate_outputs.py     # Post-run metrics + gate validation
│   └── smoke_test.py           # 5-property integration test — must pass 5/5
├── data/
│   ├── raw_html/               # 30-day rolling — git-ignored
│   ├── screenshots/            # 30-day rolling — git-ignored
│   ├── scrape_events.jsonl     # Append-only audit log
│   ├── extraction_output/      # Per-property JSON output (Phase A format)
│   ├── runs/{date}/            # Production output: properties.json, report.json/md, issues.jsonl
│   └── state/                  # Persistent state: property_index.json, unit_index.json
├── requirements.txt
├── pyproject.toml
├── .env.example
└── README.md
```

---

## Production pipeline — daily_runner.py + entrata.py

The production scraping pipeline uses `scripts/daily_runner.py` as the entrypoint — **not** `run_phase_a.py`. The `run_phase_a.py` script is the original BRD-spec Phase A pipeline and is retained for reference but is not used for production runs.

### Architecture

```
daily_runner.py          # Orchestrator: loads CSV, resolves identity, runs pipeline,
    |                    # diffs against prior state, writes output + report
    +-- identity.py      # 5-tier canonical ID resolution (dedup across runs)
    +-- entrata.py       # Core scraper engine (handles ALL platforms)
    |     +-- Tier 1: API interception (XHR/fetch capture during page load)
    |     +-- Tier 2: JSON-LD (Schema.org structured data)
    |     +-- Tier 3: DOM parsing (selector cascade + regex fallback)
    +-- scrape_properties.py   # Unit transformation: raw API bodies -> 46-key schema
    +-- state_store.py   # Persistent JSON state: property_index + unit_index
    +-- validation.py    # Structured issue logging (ERROR/WARNING/INFO + codes)
```

### Running the production pipeline

```bash
# Full daily run (all properties)
python scripts/daily_runner.py --csv config/properties.csv

# Test with N properties
python scripts/daily_runner.py --csv config/properties.csv --limit 5

# With proxy
python scripts/daily_runner.py --proxy http://user:pass@host:port

# Scrape a single property (debug)
python scripts/entrata.py --url https://property-website.com
```

### Output

Production output goes to `data/runs/{date}/`:
- `properties.json` — 46-key property records with nested units
- `report.json` / `report.md` — run summary
- `issues.jsonl` — validation issues

Persistent state in `data/state/`:
- `property_index.json` — tracks first_seen, last_seen, scrape status per property
- `unit_index.json` — unit history with daily diffs (new/updated/unchanged/disappeared)

### Relationship to ma_poc/extraction/ pipeline

The `extraction/` pipeline (`tier1_api.py` through `tier5_vision.py`, `pipeline.py`) and `templates/` directory are the **BRD-spec Phase A implementation** using BeautifulSoup on static HTML with a `BrowserSession` dataclass.

`scripts/entrata.py` is a **parallel implementation** using Playwright directly (live page interaction, `page.query_selector_all`, `page.evaluate`). It handles the same extraction tiers but with different code paths optimized for real-world scraping (multi-page BFS crawling, SightMap dedicated parser, 50+ API key name variants, live DOM selectors).

The two systems do not share extraction code. `daily_runner.py` uses `entrata.py` exclusively.

---

## Environment

### requirements.txt — pin all versions

```
playwright==1.43.0
aiohttp==3.9.5
aiofiles==23.2.1
openai==1.30.0
anthropic==0.26.0
extruct==0.17.0
lxml==5.2.2
beautifulsoup4==4.12.3
rapidfuzz==3.9.1
httpx==0.27.0
tenacity==8.3.0
pydantic==2.7.2
pandas==2.2.2
orjson==3.10.3
pyyaml==6.0.1
python-dotenv==1.0.1
pytest==8.2.0
pytest-asyncio==0.23.6
pytest-mock==3.14.0
respx==0.21.1
pytest-cov==5.0.0
ruff==0.4.7
mypy==1.10.0
structlog==24.2.0
rich==13.7.1
```

### .env.example

```
AZURE_OPENAI_ENDPOINT=https://<resource>.openai.azure.com/
AZURE_OPENAI_API_KEY=<key>
AZURE_OPENAI_DEPLOYMENT_GPT4O_MINI=gpt-4o-mini
AZURE_OPENAI_DEPLOYMENT_GPT4O_VISION=gpt-4o
ANTHROPIC_API_KEY=<key>
PROXY_PROVIDER=brightdata
PROXY_HOST=<host>
PROXY_PORT=<port>
PROXY_USERNAME=<user>
PROXY_PASSWORD=<pass>
MAX_CONCURRENT_BROWSERS=10
PAGE_LOAD_TIMEOUT_MS=30000
SCRAPE_FAILURE_THRESHOLD=0.05
VISION_PROVIDER=azure
VISION_SAMPLE_RATE=0.075
DATA_DIR=./data
RAW_HTML_RETENTION_DAYS=30
SCREENSHOT_RETENTION_DAYS=30
```

### pyproject.toml — required settings

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]

[tool.mypy]
strict = true

[[tool.mypy.overrides]]
module = ["playwright.*", "extruct.*", "rapidfuzz.*", "structlog.*"]
ignore_missing_imports = true
```

---

## Data models — implement all three before any pipeline code

### models/extraction_result.py

```python
from enum import IntEnum, Enum
from pydantic import BaseModel, Field
from typing import Optional, Any
from datetime import datetime

class ExtractionTier(IntEnum):
    API_INTERCEPTION = 1
    JSON_LD          = 2
    PLAYWRIGHT_TPL   = 3
    LLM_GPT4O_MINI   = 4
    VISION_FALLBACK  = 5

class ExtractionStatus(str, Enum):
    SUCCESS = "SUCCESS"
    FAILED  = "FAILED"
    SKIPPED = "SKIPPED"  # Change detection determined no change

class ExtractionResult(BaseModel):
    property_id:           str
    tier:                  Optional[ExtractionTier] = None
    status:                ExtractionStatus
    confidence_score:      float = Field(default=0.0, ge=0.0, le=1.0)
    raw_fields:            dict[str, Any] = Field(default_factory=dict)
    field_confidences:     dict[str, float] = Field(default_factory=dict)
    low_confidence_fields: list[str] = Field(default_factory=list)
    timestamp:             datetime = Field(default_factory=datetime.utcnow)
    error_message:         Optional[str] = None

    @property
    def succeeded(self) -> bool:
        """True only if status SUCCESS and confidence meets 0.7 threshold."""
        return self.status == ExtractionStatus.SUCCESS and self.confidence_score >= 0.7
```

### models/scrape_event.py

```python
from enum import Enum
from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime

class ChangeDetectionResult(str, Enum):
    CHANGED      = "CHANGED"
    UNCHANGED    = "UNCHANGED"
    INCONCLUSIVE = "INCONCLUSIVE"

class ScrapeOutcome(str, Enum):
    SUCCESS = "SUCCESS"
    SKIPPED = "SKIPPED"
    FAILED  = "FAILED"
    PARTIAL = "PARTIAL"

class ScrapeEvent(BaseModel):
    event_id:                 str           # UUID4
    property_id:              str
    scrape_timestamp:         datetime
    extraction_tier:          Optional[int] = None
    change_detection_result:  Optional[ChangeDetectionResult] = None
    scrape_outcome:           ScrapeOutcome
    failure_reason:           Optional[str] = None
    page_load_ms:             Optional[int] = None
    proxy_used:               bool = False
    proxy_provider:           Optional[str] = None
    vision_fallback_used:     bool = False
    banner_capture_attempted: bool = False
    banner_concession_found:  bool = False
    accuracy_sample_selected: bool = False
    raw_html_path:            Optional[str] = None
    screenshot_path:          Optional[str] = None
    confidence_score:         Optional[float] = None
```

### models/unit_record.py

Phase A populates only extractable fields. Enrichment fields (`effective_rent`, `concession`, `days_on_market`, `availability_periods`) are `None` — Phase B PR-07 fills them. **Do not change field names** — Phase B imports this model directly into PostgreSQL.

```python
from enum import Enum
from pydantic import BaseModel, Field
from typing import Optional, Any
from datetime import datetime, date

class AvailabilityStatus(str, Enum):
    AVAILABLE   = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"
    UNKNOWN     = "UNKNOWN"

class DataQualityFlag(str, Enum):
    CLEAN          = "CLEAN"
    SMOOTHED       = "SMOOTHED"
    CARRIED_FORWARD = "CARRIED_FORWARD"
    QA_HELD        = "QA_HELD"

class UnitRecord(BaseModel):
    unit_id:              Optional[str] = None   # Phase B PR-06 entity resolution
    property_id:          str
    unit_number:          str
    floor_plan_id:        Optional[str] = None
    floor:                Optional[int] = None
    building:             Optional[str] = None
    sqft:                 Optional[int] = None
    floor_plan_type:      Optional[str] = None   # "1/1", "2/2", "Studio"
    asking_rent:          Optional[float] = None
    effective_rent:       Optional[float] = None  # Phase B PR-07
    concession:           Optional[dict[str, Any]] = None  # Phase B PR-07
    availability_status:  AvailabilityStatus = AvailabilityStatus.UNKNOWN
    availability_date:    Optional[date] = None
    days_on_market:       Optional[int] = None    # Phase B PR-07
    availability_periods: list[dict] = Field(default_factory=list)  # Phase B PR-07
    scrape_timestamp:     datetime = Field(default_factory=datetime.utcnow)
    extraction_tier:      Optional[int] = None
    confidence_score:     float = Field(default=0.0, ge=0.0, le=1.0)
    data_quality_flag:    DataQualityFlag = DataQualityFlag.CLEAN
    source:               str = "DIRECT_SITE"
    carryforward_days:    int = 0
```

---

## PR-01 — Scraping Fleet (scraper/)

### Acceptance criteria

| Criterion | Implementation |
|---|---|
| 500 properties scraped daily | Load `properties.csv` with `pandas.read_csv(encoding="utf-8-sig")`. STABILISED: 1× daily. LEASE_UP: every 4 hours 8am–9pm **local timezone** (derive from zip, never use UTC). Enforce `MAX_CONCURRENT_BROWSERS` with `asyncio.Semaphore`. |
| Property mix stratification | `properties.csv` required columns: `property_id`, `url`, `type` (STABILISED\|LEASE_UP), `pms_platform` (optional). |
| Residential proxy | `proxy_manager.py` handles rotation. Auto-escalate domains with >2% bot-block failure rate. |
| Raw HTML + screenshots 30-day TTL | Save to `data/raw_html/{property_id}/{date}.html` and `data/screenshots/{property_id}/{date}.png` after page load. Cleanup task enforces TTL. |
| Failure rate <5% per domain / 7-day rolling | Tracked in `scrape_events.jsonl`. `validate_outputs.py` computes and alerts. |
| All scrapes logged | One `ScrapeEvent` per scrape appended to `data/scrape_events.jsonl` via `event_log.py`. Serialise with `model.model_dump(mode="json")` — never `.dict()`. |
| P95 page load <30s | Record actual `page_load_ms` in `ScrapeEvent`. `validate_outputs.py` computes P95 daily. |

### browser.py — critical rules

- One Playwright Chromium browser per property per scrape cycle. **Never share contexts across properties.**
- Register the network interception handler **before** `page.goto()` — Tier 1 API capture requires intercepting XHR/fetch during initial page load.
- User-agent: rotate from a list of 10 realistic desktop Chrome UA strings. Never use the Playwright default headless UA.
- Viewport: `1920×1080` — required for consistent vision screenshots.
- Wait strategy: `wait_until="networkidle"` + `asyncio.sleep(2.0)`. SPA frameworks need both.
- On any exception: save partial HTML if available, log `ScrapeOutcome.FAILED` with `failure_reason`, return — **do not raise**.
- Always `await context.close()` in a `finally` block. **Never call `browser.close()`** — it destroys all concurrent sessions' contexts.

---

## PR-02 — Change Detection Gate (scraper/change_detection.py)

Runs **before** Playwright launches. Skip (carry forward prior data) only if **all** available mechanisms return `UNCHANGED`. Any `CHANGED` or `INCONCLUSIVE` triggers a full scrape.

### Three mechanisms

**Mechanism 1 — ETag/Last-Modified**
- Issue async `HEAD` request with both `If-None-Match` AND `If-Modified-Since` headers.
- 304 response → `UNCHANGED`. Missing both headers in response → `INCONCLUSIVE`.
- Do not launch Playwright for this check.

**Mechanism 2 — Sitemap lastmod**
- Fetch `/sitemap.xml`, parse `<lastmod>` for the exact property URL path.
- Compare to stored value in `data/change_detection_state.json`.
- Match → `UNCHANGED`. No sitemap or no matching entry → `INCONCLUSIVE`.

**Mechanism 3 — API response hash (Tier 1 SPA properties only)**
- Re-issue the known API endpoint from `api_catalogue.json` without a full page load.
- SHA-256 hash of JSON response body. Match → `UNCHANGED`. No known API pattern for property → `INCONCLUSIVE`.

### Skip and state rules

- Skip only when ALL available mechanisms return `UNCHANGED`.
- Override skip if `days_since_full_scrape >= 7` (forced full scrape).
- Track per property in `data/change_detection_state.json`: `last_etag`, `last_lastmodified`, `last_sitemap_lastmod`, `last_api_hash`, `last_full_scrape_date`, `carryforward_days`.
- **Protect all state file writes with `asyncio.Lock`** — concurrent workers must not produce torn writes.
- Increment `carryforward_days` on each skip. Reset to 0 on fresh scrape success.
- Week 2 success signal: skip rate must exceed 50% for STABILISED properties.

> ⚠️ Do not use `browser.evaluate()` or `page.goto()` to check for changes. Any check that launches Playwright defeats the purpose of the gate.

---

## PR-03 — Tiered Extraction (extraction/)

All tiers operate from the single already-loaded Playwright page. `pipeline.py` calls each tier in sequence. First result with `confidence_score >= 0.7` wins. Tier and confidence are always logged.

### extraction/pipeline.py — implement exactly this logic

```python
async def run_extraction_pipeline(session: BrowserSession) -> ExtractionResult:
    """
    Attempt tiers 1–4 in priority order.
    Return first result with confidence >= 0.7 (result.succeeded == True).
    If none succeed, return best result with status=FAILED for Vision fallback decision.
    """
    tiers = [
        (ExtractionTier.API_INTERCEPTION, tier1_extract),
        (ExtractionTier.JSON_LD,          tier2_extract),
        (ExtractionTier.PLAYWRIGHT_TPL,   tier3_extract),
        (ExtractionTier.LLM_GPT4O_MINI,   tier4_extract),
    ]
    best: Optional[ExtractionResult] = None
    for tier_enum, fn in tiers:
        result = await fn(session)
        result.tier = tier_enum
        if result.succeeded:
            return result          # First tier that meets threshold wins; rest are skipped
        if best is None or result.confidence_score > best.confidence_score:
            best = result
    assert best is not None
    best.status = ExtractionStatus.FAILED
    return best  # Caller checks confidence < 0.6 to trigger Vision Tier 5
```

### Tier 1 — API Interception (tier1_api.py)

- Register Playwright route handler before `page.goto()`. Buffer all XHR/fetch responses to `session.intercepted_api_responses` — **instance-scoped, not module-level**.
- Match intercepted URLs against `config/api_catalogue.json` patterns: `/api/`, `/availability`, `/pricing`, `/floorplans`, `/units`, `/apartments`.
- Run `scripts/build_api_catalogue.py` against 50 seed properties in **Week 1 before running on all 500**.
- Confidence = present required fields / total required fields. Required: `unit_number`, `asking_rent`, `availability_status`. Preferred: `sqft`, `floor_plan_type`.

### Tier 2 — JSON-LD / Schema.org (tier2_jsonld.py)

- Use `extruct` library. Target schemas: `Apartment`, `ApartmentComplex`, `Offer`.
- Field mappings: `floorSize → sqft`, `offers[].price → asking_rent`, `offers[].availability → availability_status`.
- Multiple `Apartment` objects on page → multiple `UnitRecord` instances.
- Confidence: 1.0 if all required fields present. Degrade 0.15 per missing required field.

### Tier 3 — Playwright Templates (tier3_templates.py + templates/)

**RentCafe (templates/rentcafe.py)**
- Primary selectors: `.unitContainer`, `.pricingWrapper`, `.floorplanName`, `.unitNumber`, `.rent`, `.availabilityDate`, `.sqft`
- Handle both list view and floorplan-grouped view.

**Entrata (templates/entrata.py)**
- Primary selectors: `.entrata-unit-row`, `.unit-number`, `.unit-price`, `.unit-availability`, `.unit-sqft`
- Handle lazy loading: scroll to bottom + wait for `networkidle`.

**AppFolio (templates/appfolio.py)**
- Primary selectors: `.js-listing-card`, `.listing-unit-detail-table`, `.price`, `.status`, `.sqft`
- Handle paginated unit tables.

**All templates:** If primary selector returns 0 elements, try secondary fallback selectors. If all selectors fail, return `ExtractionStatus.FAILED` — do not raise. Failure automatically triggers Tier 4. Template failure rate must stay **<10% per platform**.

### Tier 4 — LLM Extraction (tier4_llm.py)

Use this **exact system prompt**:

```
You are a real estate data extraction agent.
Extract all apartment unit listings from the provided HTML.
Return a JSON object with this exact structure:
{
  "units": [
    {
      "unit_number": string,
      "floor_plan_type": string,
      "asking_rent": number,
      "availability_status": "AVAILABLE" | "UNAVAILABLE" | "UNKNOWN",
      "availability_date": string | null,
      "sqft": number | null,
      "_confidence": {
        "unit_number": number,
        "asking_rent": number,
        "availability_status": number
      }
    }
  ],
  "property_name": string | null,
  "extraction_notes": string
}
Return ONLY the JSON object. No markdown, no explanation.
```

HTML preparation before sending:
1. Strip all `<script>` and `<style>` tags and their content.
2. If remaining HTML > 60K tokens: extract the pricing/unit section only.
3. Log truncation in `ScrapeEvent.failure_reason` if truncated.
4. Wrap `json.loads()` in `try/except JSONDecodeError` — retry once with a prompt to fix JSON.
5. Backoff: `tenacity` with `random.uniform(0, cap)` jitter for 429 responses. Max 5 retries.

---

## PR-04 — Vision LLM (extraction/tier5_vision.py, vision_banner.py, vision_sample.py)

Three independent roles. All use the already-rendered page — **no new browser session**.

| Role | Trigger | Output |
|---|---|---|
| A — Tier 5 Fallback | Tiers 1–4 all fail or confidence < 0.6 | Unit records with `method=VISION_FALLBACK`. Target: ≤5% of properties. |
| B — Banner Capture | Every property, every scrape, regardless of extraction tier | `{type, value, conditions, start_date, end_date, source="IMAGE_BANNER"}`. `banner_capture_attempted=True` in ScrapeEvent. |
| C — Accuracy Sample | Rotating 5–10% of daily successes | Field-by-field diff vs primary output. Written to `{date}_vision_comparison.json`. Agreement rate logged. |

### VisionProvider abstraction (tier5_vision.py)

```python
from abc import ABC, abstractmethod
from typing import Any

class VisionProvider(ABC):
    @abstractmethod
    async def extract_units_from_screenshot(
        self, images: list[bytes], prompt: str
    ) -> dict[str, Any]: ...

class AzureVisionProvider(VisionProvider):
    """GPT-4o via Azure OpenAI. Model: AZURE_OPENAI_DEPLOYMENT_GPT4O_VISION."""
    ...

class AnthropicVisionProvider(VisionProvider):
    """Claude 3.5 Sonnet. Model: claude-3-5-sonnet-20241022."""
    ...

def get_vision_provider() -> VisionProvider:
    return AnthropicVisionProvider() if os.getenv("VISION_PROVIDER") == "anthropic" \
           else AzureVisionProvider()
```

### Image handling rules

- **Role A:** Capture targeted section screenshots (pricing panel, availability table, concession area) using `page.locator()` — not full-page screenshots. Pass all sections in a single API call.
- **Image size:** Check base64-encoded size before every API call. Azure limit: 20 MB. Anthropic limit: 5 MB. Downsample or crop if oversized.
- **Role C deterministic sample:** Use `hashlib.sha256((property_id + scrape_date).encode()).hexdigest()` — **never built-in `hash()`** (non-deterministic across Python processes).
- **Role C isolation:** Must not modify the primary extraction result. Runs in parallel after primary success. Writes to comparison JSON only.

---

## Phase A output format (no PostgreSQL until Phase B)

### data/scrape_events.jsonl — one ScrapeEvent per line

```json
{"event_id":"a1b2c3","property_id":"P001","scrape_timestamp":"2026-04-08T02:15:00Z","extraction_tier":3,"change_detection_result":"CHANGED","scrape_outcome":"SUCCESS","page_load_ms":4200,"proxy_used":false,"vision_fallback_used":false,"banner_capture_attempted":true,"banner_concession_found":false,"accuracy_sample_selected":false,"confidence_score":0.91}
```

### data/extraction_output/{property_id}/{date}.json

```json
{
  "property_id": "P001",
  "scrape_date": "2026-04-08",
  "extraction_tier": 3,
  "confidence_score": 0.91,
  "units": [
    {
      "unit_number": "101",
      "floor_plan_type": "1/1",
      "asking_rent": 1450.00,
      "availability_status": "AVAILABLE",
      "availability_date": "2026-05-01",
      "sqft": 750,
      "carryforward_days": 0
    }
  ],
  "banner_concession": null
}
```

> ⚠️ Always serialise with `model.model_dump(mode="json")` — never `.dict()`. `.dict()` does not correctly serialise `datetime` to ISO string in Pydantic v2.

---

## Test suite — minimum coverage

Write the test file for each module immediately after implementing it (Workflow Step 3).

| Test file | Min tests | Must cover |
|---|---|---|
| `test_change_detection.py` | 8 | ETag 304 → UNCHANGED; no ETag header → INCONCLUSIVE; sitemap lastmod unchanged; forced scrape after 7 days; `carryforward_days` increments; all-UNCHANGED → SKIPPED; any-CHANGED → full scrape; state file persists across instances. |
| `test_tier1_api.py` | 6 | Matched URL → extraction; non-matching URL ignored; malformed JSON silently discarded; confidence score calculation; `api_catalogue` miss → FAILED; per-field confidence populates `field_confidences`. |
| `test_tier2_jsonld.py` | 5 | Valid Apartment schema → all fields extracted; ApartmentComplex with Offers → unit records; missing sqft → confidence degraded; invalid schema → FAILED; multiple Apartment objects → multiple UnitRecords. |
| `test_tier3_templates.py` | 9 | RentCafe list view; RentCafe floorplan-grouped view; RentCafe all selectors fail → FAILED; Entrata standard; Entrata lazy-load wait; Entrata fail; AppFolio standard; AppFolio paginated table; AppFolio fail. |
| `test_tier4_llm.py` | 6 | Valid JSON response → unit records; `JSONDecodeError` → retry once; 429 rate limit → backoff retry; token count logged; HTML stripped before send; truncation logged in `failure_reason`. |
| `test_tier5_vision.py` | 5 | Azure provider correct image format; Anthropic provider correct image format; Role A targeted section screenshots; Role B banner structured output; Role C field comparison written to file. |
| `test_pipeline.py` | 5 | Tier 1 success → tiers 2–4 not called; Tier 1+2 fail, Tier 3 succeeds; all tiers fail → best result returned as FAILED; confidence < 0.6 → status FAILED for Vision signal; tier logged in `ExtractionResult`. |

### Running tests

```bash
# Run a single module's tests while developing (fast feedback)
pytest tests/test_{module}.py -v --tb=long

# Run all tests — do this at every weekly gate
pytest . -v --tb=short --ignore=data --ignore=config

# With coverage
pytest . -v --ignore=data --ignore=config --cov=. --cov-report=term-missing

# Stop on first failure (useful during active debugging)
pytest tests/ -v --tb=long -x

# Run tests matching a keyword
pytest tests/ -v -k "tier1 or tier2"
```

---

## scripts/validate_outputs.py — required metrics

Run after every daily scrape batch. Reads `scrape_events.jsonl`. Prints structured summary.

| Metric | Target | Alert action if missed |
|---|---|---|
| Total properties scraped (24h) | 500 | Check fleet.py schedule — are all 500 in the run queue? |
| Overall scrape success rate | ≥ 95% | Log failing domains. Check proxy config. |
| Tier distribution: Tiers 1+2 share | > 40% by Week 2 | Low Tier 1 → rebuild `api_catalogue`. Low Tier 3 → fix templates. |
| Change detection skip rate (STABILISED) | ≥ 50% by Week 2 | Verify ETag/sitemap checks functioning correctly. |
| Vision Tier 5 fallback rate | ≤ 5% | If >5%, Tier 4 LLM is failing. Check Azure OpenAI error rate. |
| Banner capture attempted rate | 100% | `vision_banner.py` not running on all non-SKIPPED properties. |
| P95 page load time | < 30,000 ms | Identify slow properties. Check proxy performance. |
| Per-domain failure rate (7-day rolling) | < 5% | Escalate failing domains to proxy. |
| Vision sample rate | 5–10% of successes | Check `VISION_SAMPLE_RATE` env var. |
| Vision validation agreement rate | ≥ 90% field-level | Log top 5 disagreeing fields. Investigate primary pipeline reliability. |

---

## Weekly gates — binary pass/fail

Document any unmet gate with root cause before proceeding.

| Week | Gate | Pass condition |
|---|---|---|
| 1 | All 500 properties scraping daily. API catalogue built. Change detection live. | All 500 have ≥1 `ScrapeEvent`. `api_catalogue.json` has patterns for top-3 PMS platforms. |
| 1 | Tier 1 attempted on correct SPA PMS subset. | `validate_outputs.py` shows Tier 1 attempted on ≥30% of RentCafe/Entrata/AppFolio properties. |
| 2 | Tiers 1–4 live. Tier distribution measurable. | `validate_outputs.py` tier section populated. Tiers 1+2 > 40% of properties. |
| 2 | Change detection skip rate > 50% for STABILISED. | `validate_outputs.py` shows STABILISED skip rate. |
| 3 | Vision Tier 5 fallback live. | Fallback rate ≤ 5% in `validate_outputs.py`. |
| 3 | Banner capture on 100% of non-SKIPPED properties. | All SUCCESS `ScrapeEvent`s have `banner_capture_attempted=True`. |
| 3 | 5–10% accuracy sample live. | Vision comparison JSONs appearing in `extraction_output/`. |
| 3 | All tests passing. Smoke test 5/5. | `pytest . --ignore=data --ignore=config` exits 0. `smoke_test.py` exits 0. |

---

## Bug hunt — 15 mandatory checks (Workflow Step 7)

Run after all modules complete. For each item: state `CHECKED — NOT PRESENT` or `CHECKED — FOUND AND FIXED: [description]`.

| # | Category | What to verify |
|---|---|---|
| 1 | Async context leaks | Every `async with` (browser, page, aiofiles) has a `finally` block that closes the context. |
| 2 | Playwright concurrency | `intercepted_api_responses` is instance-scoped on `BrowserSession`, not module-level. `asyncio.Semaphore` enforces `MAX_CONCURRENT_BROWSERS`. |
| 3 | JSON parsing exceptions | Every `json.loads()` wrapped in `try/except JSONDecodeError` — in tier1, tier4, state file reads, api_catalogue load. |
| 4 | State file race conditions | `change_detection_state.json` writes protected by `asyncio.Lock`. No concurrent writes possible. |
| 5 | LLM token overflow | `<script>`/`<style>` stripped first. Truncation logged in `ScrapeEvent.failure_reason`. Token count logged per call. |
| 6 | Vision API image size | Base64-encoded size checked before every API call. Azure ≤ 20 MB, Anthropic ≤ 5 MB. Oversized images downsampled or cropped. |
| 7 | Screenshot capture timing | Screenshot taken **after** `wait_until="networkidle"` + `asyncio.sleep(2.0)`. Early screenshot misses dynamically loaded unit data. |
| 8 | ETag request headers | Both `If-None-Match` AND `If-Modified-Since` sent. Sending only one misses servers that only support the other. |
| 9 | Confidence score range | All confidence scores clamped to [0.0, 1.0]. Pydantic `Field(ge=0.0, le=1.0)` enforces range but calculations must not produce out-of-range values before clamping. |
| 10 | properties.csv parsing | `pandas.read_csv(encoding="utf-8-sig")` — handles BOM prefix and CRLF line endings. |
| 11 | Lease-up timezone | LEASE_UP scrape schedule uses property local timezone, not UTC. Derive from zip code using `timezonefinder` or similar. |
| 12 | Retention cleanup idempotency | 30-day cleanup is idempotent — running twice in one day does not delete files newer than 30 days. |
| 13 | Retry jitter | Tenacity backoff uses `random.uniform(0, cap)` jitter. Prevents retry storms when multiple workers hit 429 simultaneously. |
| 14 | Pydantic v2 serialisation | `model.model_dump(mode="json")` used everywhere — **never `.dict()`**. `.dict()` does not correctly serialise `datetime` in Pydantic v2. |
| 15 | Vision sample determinism | `hashlib.sha256` used for sample selection — **never built-in `hash()`**. Built-in `hash()` is non-deterministic across Python processes. |

### smoke_test.py — implement with these assertions

```python
# scripts/smoke_test.py
# Load first 5 properties from config/properties.csv
# Run full pipeline for each: change detection → browser → tiers → vision banner
# For each property assert:
#   a. ScrapeEvent written to data/scrape_events.jsonl
#   b. data/extraction_output/{property_id}/{today}.json exists
#   c. scrape_outcome is SUCCESS or SKIPPED — never unexplained FAILED
#   d. confidence_score is in [0.0, 1.0]
#   e. units list is non-empty for SUCCESS outcomes
#   f. banner_capture_attempted == True for all non-SKIPPED scrapes
#   g. extraction_tier is set (1–5) for all SUCCESS outcomes
# Print: "SMOKE TEST: X/5 PASSED"
# Exit code 0 if 5/5, exit code 1 if any failures
```

---

## Phase A completion checklist

All items must be checked before Phase A is declared done.

**Repository**
- [ ] Directory structure matches the Repository Structure section exactly
- [ ] `requirements.txt` with all pinned versions
- [ ] `.env.example` with all environment variables
- [ ] `pyproject.toml` with `asyncio_mode = "auto"` and `mypy` strict settings
- [ ] `README.md` documents setup, env vars, how to run Phase A, how to run tests
- [ ] `config/business_rules.yaml` with defaults: `occupancy_inference_rule: 1`, `move_in_lag_days: 14`, `availability_reappearance_window_days: 7`

**Data models**
- [ ] `models/extraction_result.py` — `ExtractionResult`, `ExtractionTier`, `ExtractionStatus`
- [ ] `models/scrape_event.py` — `ScrapeEvent` with all fields from PR-01
- [ ] `models/unit_record.py` — `UnitRecord` forward-compatible with Phase B PostgreSQL schema

**Scraper (PR-01 + PR-02)**
- [ ] `scraper/browser.py` — Playwright session with interception, proxy, HTML/screenshot storage, `finally` close
- [ ] `scraper/change_detection.py` — All 3 mechanisms + `asyncio.Lock` state file + 7-day forced scrape
- [ ] `scraper/fleet.py` — Async fleet with `asyncio.Semaphore` and STABILISED/LEASE_UP schedules with local timezone
- [ ] `scraper/proxy_manager.py` — Proxy rotation with per-domain failure rate tracking

**Extraction pipeline (PR-03 + PR-04)**
- [ ] `extraction/pipeline.py` — Tier orchestrator returning first `confidence >= 0.7` result
- [ ] `extraction/tier1_api.py` — API interception with `api_catalogue.json` matching
- [ ] `extraction/tier2_jsonld.py` — `extruct`-based JSON-LD parsing with confidence scoring
- [ ] `extraction/tier3_templates.py` — PMS template dispatcher
- [ ] `templates/rentcafe.py` — List view and floorplan-grouped view
- [ ] `templates/entrata.py` — Standard and lazy-load handling
- [ ] `templates/appfolio.py` — Standard and paginated table handling
- [ ] `extraction/tier4_llm.py` — Exact system prompt + HTML stripping + `jitter` backoff
- [ ] `extraction/tier5_vision.py` — `VisionProvider` abstraction + Azure + Anthropic implementations
- [ ] `extraction/vision_banner.py` — Banner capture on every non-SKIPPED property
- [ ] `extraction/vision_sample.py` — Deterministic `hashlib` sample + field comparison JSON

**Storage and scripts**
- [ ] `storage/event_log.py` — Append-only JSONL with `model_dump(mode="json")`
- [ ] `scripts/daily_runner.py` — Production entrypoint: CSV load, identity resolution, scraping via entrata.py, state diffing, 46-key output
- [ ] `scripts/entrata.py` — Core scraper engine: Tier 1 API interception, Tier 2 JSON-LD, Tier 3 DOM parsing (all platforms)
- [ ] `scripts/identity.py` — 5-tier canonical ID resolution with dedup detection
- [ ] `scripts/state_store.py` — Persistent property_index + unit_index with daily diff (new/updated/unchanged/disappeared)
- [ ] `scripts/validation.py` — Structured issue logging with severity codes
- [ ] `scripts/scrape_properties.py` — Simpler batch scraper (no state tracking, for one-off runs)
- [ ] `scripts/run_phase_a.py` — BRD-spec Phase A pipeline (not used in production)
- [ ] `scripts/build_api_catalogue.py` — API pattern discovery on 50-property seed set
- [ ] `scripts/validate_outputs.py` — All 10 metrics computed and printed
- [ ] `scripts/smoke_test.py` — All 7 assertions, exits 0 on 5/5 pass

**Tests**
- [ ] All 7 test files with minimum test counts met
- [ ] `tests/fixtures/` with realistic HTML files for all 3 PMS platforms
- [ ] `pytest . -v --ignore=data --ignore=config` exits with zero failures
- [ ] Coverage report generated

**Bug hunt**
- [ ] `ruff check .` exits with zero E/F errors
- [ ] `mypy . --strict` exits with zero type errors
- [ ] All 15 bug categories explicitly checked and documented
- [ ] `scripts/smoke_test.py` passes 5/5

**Handoff state**
- [ ] `data/runs/{date}/properties.json` populated from at least one full `daily_runner.py` run
- [ ] `data/state/property_index.json` and `data/state/unit_index.json` persisted
- [ ] All weekly gates (Weeks 1–3) met and documented
- [ ] No known failing tests. No known open bugs.