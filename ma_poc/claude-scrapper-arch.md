# Self-Learning Scrape Profile — Architecture & Implementation Status

## Implementation Status (2026-04-14)

**All phases below are IMPLEMENTED.** The original instructions below served as the spec. Key changes from the original spec:

### What's built (beyond the original spec)

1. **7-Phase Pipeline** — The `scrape()` function in `entrata.py` was restructured from the original linear tier cascade into a 7-phase pipeline with exploratory link navigation and per-page network observation. See `scripts/CLAUDE.md` for the full pipeline documentation.

2. **LLM-Assisted API Discovery (Phase 5)** — Instead of only using LLM as a fallback extractor (send whole page), LLM now analyzes individual API responses one at a time (`services/llm_extractor.py:analyze_api_with_llm()`). It classifies each API as units/noise and provides `json_paths` + `response_envelope` for deterministic replay.

3. **Per-Property API Blocklist** — `BlockedEndpoint` model added to `ScrapeProfile`. When LLM identifies an API response as noise (chatbot config, analytics, etc.), the URL is saved to the profile's blocklist and filtered out on future runs.

4. **LLM Field Mapping Replay** — `LlmFieldMapping` model added. LLM-generated json_paths mappings are saved to profiles and replayed deterministically via `apply_saved_mapping()` on subsequent runs — no LLM call needed.

5. **Targeted DOM Analysis** — `analyze_dom_with_llm()` sends specific DOM sections (not full pages) to LLM using `config/prompts/dom_analysis.txt`. Returns units AND CSS selectors for profile persistence.

6. **Availability Defaults** — If units/floor plans are found but have no availability data, status defaults to `AVAILABLE` and date to today.

7. **Explored Link Tracking** — `NavigationConfig.explored_links` records links that had no data, skipping them on future runs. `availability_links` records links that worked.

### Updated model schema

The `ScrapeProfile` model (`models/scrape_profile.py`) now includes:
- `BlockedEndpoint` — per-property API noise blocklist (url_pattern, reason, attempts)
- `LlmFieldMapping` — deterministic replay mapping (api_url_pattern, json_paths, response_envelope, success_count)
- `ApiHints.blocked_endpoints: list[BlockedEndpoint]`
- `ApiHints.llm_field_mappings: list[LlmFieldMapping]`
- `NavigationConfig.availability_links: list[str]`
- `NavigationConfig.explored_links: list[str]`
- `DomHints.availability_page_sections: list[str]`
- `LlmArtifacts.last_api_analysis_results: dict[str, str]`

### New prompt templates

- `config/prompts/api_analysis.txt` — targeted single-API analysis (Phase 5)
- `config/prompts/dom_analysis.txt` — targeted DOM section analysis (Phase 6a)
- `config/prompts/tier4_extraction.txt` — legacy broad extraction (Phase 6b fallback)

### New functions in services/llm_extractor.py

- `analyze_api_with_llm()` — single API response → (units, mapping, is_noise)
- `analyze_dom_with_llm()` — DOM section → (units, css_selectors)
- `apply_saved_mapping()` — deterministic extraction from saved LlmFieldMapping

### New functions in services/profile_updater.py

- `update_profile_blocklist()` — add/update blocked endpoints
- `save_llm_field_mapping()` — save/update LLM field mappings
- `record_explored_link()` — track links with/without data

### New helper functions in scripts/entrata.py

- `filter_network_noise()` — 3-layer filtering (global + profile + content-type)
- `prioritize_links()` — sort links by profile knowledge → anchor text → URL keywords
- `try_known_patterns()` — profile mappings → known endpoints → global parser
- `explore_link_with_observation()` — navigate + observe per-page network calls
- `apply_availability_defaults()` — set AVAILABLE + today when missing
- `_extract_dom_sections_with_rent_signals()` — find DOM sections for targeted LLM

---

## Original Mission

Implement the self-learning scrape profile architecture into the existing MA POC codebase. The system teaches itself how to scrape each property by using LLM extraction as a one-time teacher — extracting data AND generating reusable hints (CSS selectors, API URL paths, JSON field mappings) that are stored in a per-property profile. On subsequent runs the profile drives deterministic extraction without LLM calls.

**Cardinal rule:** This is an enhancement to the existing pipeline, not a rewrite. Every new module must integrate cleanly with the existing `daily_runner.py` → `entrata.py` → templates flow. Reuse existing functions. Do not duplicate logic that already works.

---

## Codebase orientation — read these files first

Before writing ANY code, read and understand these files completely. They are your source of truth for conventions, data shapes, and integration points.

### Core pipeline (modify these)
- `scripts/entrata.py` — The scraper. Contains `scrape_property()`, `parse_api_responses()`, `parse_jsonld()`, `parse_dom()`, `click_expanders()`, `looks_like_availability_api()`, `_response_looks_like_units()`. The tier cascade (Tiers 1→3) is in `scrape_property()` starting at the comment `# ── 4. Extraction: Tier 1 — API`. **This is where Tier 4 and Tier 5 are inserted — after the `"⚠ ALL TIERS FAILED"` log line, before `results["extraction_tier_used"] = "FAILED"`.**
- `scripts/daily_runner.py` — Orchestrator. Contains `run_daily()` which loops over CSV rows, calls `_scrape_in_thread()` → `scrape_property()`, then `transform_units_from_scrape()`. The profile-guided router inserts at the top of the per-property loop (after identity resolution, before the scrape call).
- `scripts/retry_runner.py` — Retry orchestrator. Same per-property loop structure as daily_runner. Must also load profiles and use the router.

### Templates (reference, extend pattern)
- `templates/appfolio.py` — AppFolio DOM template. Study `_find_containers()` cascade and `_extract_from_card()` for the pattern profile-generated selectors should follow.
- `templates/_common.py` — Shared parsing utilities: `parse_rent()`, `parse_sqft()`, `parse_availability()`, `parse_floor()`, etc. Reuse these in LLM output normalization.

### Models and validation
- `models/unit_record.py` — Pydantic v2 model for `UnitRecord`. All extracted units must conform to this schema. Use `model_dump(mode="json")`.
- `scripts/validate_outputs.py` — Validation constants and issue codes: `UNITS_EMPTY`, `UNIT_MISSING_ID`, `UNIT_INVALID_RENT`, `SCRAPE_TIMEOUT`, `SCRAPE_FAILED`, `SCRAPE_NO_APIS`, `PIPELINE_EXCEPTION`. Add new codes: `LLM_EXTRACTION_USED`, `VISION_EXTRACTION_USED`, `PROFILE_UPDATED`, `PROFILE_DRIFT_DETECTED`.

### Configuration
- `config/properties.csv` — Property list with URL, canonical ID, management company, PMS platform hints.
- Env vars: `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_DEPLOYMENT` (for GPT-4o-mini), `VISION_PROVIDER` (for Tier 5).

---

## Implementation plan — execute in this order

### Phase 1: Scrape profile schema and store

**Step 1.1 — Create `models/scrape_profile.py`**

Define the ScrapeProfile as a Pydantic v2 model. This is the central data structure.

```python
from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime
from enum import Enum

class ProfileMaturity(str, Enum):
    COLD = "COLD"
    WARM = "WARM"  
    HOT = "HOT"

class ApiEndpoint(BaseModel):
    url_pattern: str          # URL or glob pattern
    json_paths: dict[str, str] = {}  # field_name → jq-style path
    provider: Optional[str] = None   # "sightmap", "knock", "entrata_api", etc.

class FieldSelectorMap(BaseModel):
    container: Optional[str] = None       # CSS selector for unit card container
    unit_id: Optional[str] = None
    rent: Optional[str] = None
    sqft: Optional[str] = None
    bedrooms: Optional[str] = None
    bathrooms: Optional[str] = None
    availability_status: Optional[str] = None
    availability_date: Optional[str] = None
    floor_plan_name: Optional[str] = None

class ExpanderAction(BaseModel):
    selector: str
    action: str = "click"  # "click" or "scroll_into_view"

class NavigationConfig(BaseModel):
    entry_url: Optional[str] = None
    availability_page_path: Optional[str] = None
    requires_interaction: list[ExpanderAction] = []
    timeout_ms: int = 60000
    block_resource_domains: list[str] = []

class ApiHints(BaseModel):
    known_endpoints: list[ApiEndpoint] = []
    api_provider: Optional[str] = None
    wait_for_url_pattern: Optional[str] = None

class DomHints(BaseModel):
    platform_detected: Optional[str] = None
    field_selectors: FieldSelectorMap = FieldSelectorMap()
    jsonld_present: bool = False

class ExtractionConfidence(BaseModel):
    preferred_tier: Optional[int] = None     # 1-5
    last_success_tier: Optional[int] = None
    consecutive_successes: int = 0
    consecutive_failures: int = 0
    last_unit_count: int = 0
    maturity: ProfileMaturity = ProfileMaturity.COLD

class LlmArtifacts(BaseModel):
    extraction_prompt_hash: Optional[str] = None
    field_mapping_notes: Optional[str] = None
    api_schema_signature: Optional[str] = None
    dom_structure_hash: Optional[str] = None

class ScrapeProfile(BaseModel):
    canonical_id: str
    version: int = 1
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    updated_by: str = "BOOTSTRAP"  # BOOTSTRAP | LLM_EXTRACTION | LLM_VISION | HUMAN
    
    navigation: NavigationConfig = NavigationConfig()
    api_hints: ApiHints = ApiHints()
    dom_hints: DomHints = DomHints()
    confidence: ExtractionConfidence = ExtractionConfidence()
    llm_artifacts: LlmArtifacts = LlmArtifacts()
    cluster_id: Optional[str] = None
```

**Step 1.2 — Create `services/profile_store.py`**

File-based profile store. Profiles live at `config/profiles/{canonical_id}.json`. Audit copies at `config/profiles/_audit/{canonical_id}_{version}.json`.

Required methods:
- `load(canonical_id: str) -> Optional[ScrapeProfile]` — returns None if no profile exists
- `save(profile: ScrapeProfile) -> None` — increments version, writes file + audit copy
- `bootstrap_from_meta(canonical_id: str, meta: dict, website: str) -> ScrapeProfile` — creates initial COLD profile from existing `_meta` fields and URL-based PMS detection
- `list_by_maturity(maturity: ProfileMaturity) -> list[ScrapeProfile]`

PMS detection heuristic for bootstrap (put in a helper `_detect_platform(url: str) -> Optional[str]`):
- URL contains `rentcafe.com` or path `/apartments/` with `/default.aspx` → `"rentcafe"`
- URL domain matches known Entrata patterns or has `/floor-plans` responding → `"entrata"`
- Known AppFolio JS listing patterns → `"appfolio"`
- Check management company field against known PMS mappings
- Default: `None` (independent/unknown)

**Step 1.3 — Create `config/profiles/` directory**

Create the directory structure. Add a `.gitkeep` and `_audit/.gitkeep`.

---

### Phase 2: LLM extraction service (Tier 4)

**Step 2.1 — Create `services/llm_extractor.py`**

This is the Tier 4 implementation. It receives page content and returns structured unit data plus profile hints.

**Input preparation function: `prepare_llm_input()`**

```
Inputs:
  - page_html: str (raw HTML from Playwright page.content())
  - api_responses: list[dict] (from entrata.py's api_responses list — each has "url" and "body")
  - property_context: dict (name, city, total_units from CSV row, current profile if any)

Processing:
  1. HTML trimming: Remove <head>, all <script> (except JSON-LD), <style>, <svg>, <noscript>,
     <nav>, <footer>, cookie/consent banners. Keep <main> or largest content div.
     Use BeautifulSoup. Target: <15,000 tokens (~60KB text).
  2. API response selection: If api_responses is non-empty, rank by key overlap with
     unit-like field names (rent, price, sqft, bed, bath, available, unit, floor, plan).
     Include top 3 most promising responses as JSON.
  3. Combine into a structured input dict.
```

**LLM call function: `extract_with_llm()`**

```
- Use Azure OpenAI GPT-4o-mini (same client pattern as any existing LLM integration in the codebase)
- System prompt instructs the model to return JSON with two keys:
    "units": [{unit_id, floor_plan_name, bedrooms, bathrooms, sqft, 
               market_rent_low, market_rent_high, available_date, 
               availability_status, confidence}]
    "profile_hints": {
        api_urls_with_data: [str],
        json_paths: {field_name: "$.path.to.field"},
        css_selectors: {container: str, rent: str, sqft: str, ...},
        platform_guess: str | null,
        field_mapping_notes: str
    }
- Temperature: 0.0 (deterministic)
- max_tokens: 4096
- Parse response with json.loads(). Wrap in try/except — if JSON parse fails,
  try extracting JSON from markdown code fences.
- Normalize each unit record through templates/_common.py parse functions
  (parse_rent, parse_sqft, etc.) to match existing output format.
- Return: (units: list[dict], hints: dict, raw_response: str)
```

**Prompt template** — store at `config/prompts/tier4_extraction.txt`:

```
You are a real estate data extraction specialist. Extract unit-level 
apartment availability data from the provided website content.

PROPERTY CONTEXT:
- Name: {property_name}
- City: {city}, {state}
- Expected total units: {total_units}
- Website: {website}

CONTENT TO ANALYZE:
{content_type}: (HTML or API JSON or both — see below)

---

{trimmed_content}

---

OUTPUT FORMAT — respond with ONLY a JSON object, no markdown fences:
{
  "units": [
    {
      "unit_id": "string or null",
      "floor_plan_name": "string or null", 
      "bedrooms": number_or_null,
      "bathrooms": number_or_null,
      "sqft": number_or_null,
      "market_rent_low": number_or_null,
      "market_rent_high": number_or_null,
      "available_date": "YYYY-MM-DD or null",
      "availability_status": "AVAILABLE|UNAVAILABLE|WAITLIST|UNKNOWN",
      "confidence": 0.0-1.0
    }
  ],
  "profile_hints": {
    "api_urls_with_data": ["URLs from the API JSON that contained unit data"],
    "json_paths": {"rent": "$.path.to.rent", "unit_id": "$.path.to.id"},
    "css_selectors": {
      "container": "CSS selector for the repeating unit/floor-plan card",
      "rent": "CSS selector for rent within the container",
      "sqft": "CSS selector for sqft",
      "bedrooms": "CSS selector for bedroom count",
      "availability_date": "CSS selector for date"
    },
    "platform_guess": "entrata|rentcafe|appfolio|sightmap|knock|yardi|custom|null",
    "field_mapping_notes": "Free text: describe how data is organized on this site"
  }
}

RULES:
- Extract ALL available units, not just a sample.
- If data is floor-plan-level (not unit-level), extract floor plans.
- For rent ranges like "$1,200 - $1,500", set market_rent_low=1200, market_rent_high=1500.
- For CSS selectors: prefer class-based selectors over nth-child or positional selectors.
- confidence: 1.0 = certain, 0.7 = likely correct, <0.5 = guessing.
- If no units are found, return {"units": [], "profile_hints": {...}} with notes explaining why.
```

**Step 2.2 — Create `services/vision_extractor.py`**

Tier 5 implementation. Called when Tier 4 also fails (no HTML-parseable content).

```
Input: 
  - Full-page screenshot (PNG bytes from page.screenshot(full_page=True))
  - Cropped pricing section screenshot (if detectable)
  - Property context (same as Tier 4)

Use: Azure OpenAI GPT-4o Vision API (or Claude 3.5 Sonnet based on VISION_PROVIDER env var)

Output: Same (units, profile_hints, raw_response) tuple as Tier 4.

Prompt: Similar to Tier 4 but adapted for image input. Include instruction
to describe what visual layout elements contain the data (for future DOM mapping).
```

**Step 2.3 — Integrate Tiers 4-5 into `entrata.py`**

Modify `scrape_property()` in `entrata.py`. Find this exact code block:

```python
                    # ── Tier 3 — DOM ─────────────────────────────────────
                    units = await parse_dom(page, base_url)
                    if units:
                        print(f"  ✅ TIER 3 (DOM): {len(units)} units/floor plans")
                        results["extraction_tier_used"] = "TIER_3_DOM"
                    else:
                        page_url = page.url
                        print(f"  ↳ Tier 3: DOM parsing found 0 units on {page_url[:80]}")
                        print(f"  ⚠ ALL TIERS FAILED — no units extracted. "
                              f"Check raw_api/ for captured responses "
                              f"or add DOM selectors for this site.")
                        results["extraction_tier_used"] = "FAILED"
```

Replace the else block (after Tier 3 fails) with:

```python
                    else:
                        page_url = page.url
                        print(f"  ↳ Tier 3: DOM parsing found 0 units on {page_url[:80]}")
                        
                        # ── Tier 4 — LLM extraction ─────────────────────
                        from services.llm_extractor import extract_with_llm, prepare_llm_input
                        
                        llm_input = prepare_llm_input(
                            page_html=await page.content(),
                            api_responses=api_responses,
                            property_context={
                                "property_name": results.get("property_name", ""),
                                "website": base_url,
                            }
                        )
                        llm_units, llm_hints, _ = await extract_with_llm(llm_input)
                        
                        if llm_units:
                            units = llm_units
                            print(f"  ✅ TIER 4 (LLM): {len(units)} units/floor plans")
                            results["extraction_tier_used"] = "TIER_4_LLM"
                            results["_llm_hints"] = llm_hints
                        else:
                            # ── Tier 5 — Vision LLM ─────────────────────
                            from services.vision_extractor import extract_with_vision
                            
                            screenshot = await page.screenshot(full_page=True)
                            vision_units, vision_hints, _ = await extract_with_vision(
                                screenshot=screenshot,
                                property_context={
                                    "property_name": results.get("property_name", ""),
                                    "website": base_url,
                                }
                            )
                            
                            if vision_units:
                                units = vision_units
                                print(f"  ✅ TIER 5 (Vision): {len(units)} units")
                                results["extraction_tier_used"] = "TIER_5_VISION"
                                results["_llm_hints"] = vision_hints
                            else:
                                print(f"  ⚠ ALL TIERS (1-5) FAILED — no units extracted.")
                                results["extraction_tier_used"] = "FAILED"
```

**IMPORTANT:** Do NOT restructure the existing Tier 1-3 code. Insert Tiers 4-5 as an extension after the existing cascade, exactly where indicated.

Also pass the loaded profile into `scrape_property()` by adding an optional `profile: Optional[ScrapeProfile] = None` parameter. Use profile hints to:
- Set `timeout_ms` from `profile.navigation.timeout_ms` instead of hardcoded 60000/45000
- Use `profile.navigation.availability_page_path` in the floor-plan page candidates list (prepend it before the hardcoded `/floor-plans`, `/floorplans`, `/apartments`)
- Use `profile.navigation.block_resource_domains` to add route interception that aborts requests to analytics/tracking domains
- If `profile.api_hints.known_endpoints` is non-empty, use `profile.api_hints.wait_for_url_pattern` in a `page.wait_for_response()` call after page load

---

### Phase 3: Profile updater and drift detector

**Step 3.1 — Create `services/profile_updater.py`**

Called after every successful extraction (any tier). Analyses what worked and writes it into the profile.

```python
def update_profile_after_extraction(
    profile: ScrapeProfile,
    scrape_result: dict,      # The results dict from scrape_property()
    units_extracted: int,
    store: ProfileStore,
) -> ScrapeProfile:
    """
    Update profile based on what worked during this scrape.
    """
    tier = scrape_result.get("extraction_tier_used")
    
    # Record success/failure streak
    if units_extracted > 0 and tier and tier != "FAILED":
        profile.confidence.consecutive_successes += 1
        profile.confidence.consecutive_failures = 0
        profile.confidence.last_success_tier = int(tier.split("_")[1]) if "_" in tier else None
        profile.confidence.last_unit_count = units_extracted
        
        # Extract tier number for preferred_tier
        tier_num = {"TIER_1_API": 1, "TIER_2_JSONLD": 2, "TIER_3_DOM": 3, 
                    "TIER_4_LLM": 4, "TIER_5_VISION": 5}.get(tier)
        if tier_num and (profile.confidence.preferred_tier is None 
                         or tier_num < profile.confidence.preferred_tier):
            profile.confidence.preferred_tier = tier_num
    else:
        profile.confidence.consecutive_failures += 1
        profile.confidence.consecutive_successes = 0
    
    # Promote/demote maturity
    if profile.confidence.consecutive_successes >= 3:
        profile.confidence.maturity = ProfileMaturity.HOT
    elif profile.confidence.consecutive_successes >= 1:
        profile.confidence.maturity = ProfileMaturity.WARM
    elif profile.confidence.consecutive_failures >= 3:
        profile.confidence.maturity = ProfileMaturity.COLD
    
    # Record API URLs that had data
    if tier == "TIER_1_API":
        raw_apis = scrape_result.get("_raw_api_responses", [])
        for api in raw_apis:
            url = api.get("url", "")
            if _response_looks_like_units(api.get("body")):
                if not any(ep.url_pattern == url for ep in profile.api_hints.known_endpoints):
                    profile.api_hints.known_endpoints.append(
                        ApiEndpoint(url_pattern=url)
                    )
    
    # Record LLM-generated hints
    llm_hints = scrape_result.get("_llm_hints")
    if llm_hints and tier in ("TIER_4_LLM", "TIER_5_VISION"):
        profile.updated_by = "LLM_EXTRACTION" if tier == "TIER_4_LLM" else "LLM_VISION"
        
        # API hints from LLM
        for api_url in (llm_hints.get("api_urls_with_data") or []):
            if not any(ep.url_pattern == api_url for ep in profile.api_hints.known_endpoints):
                profile.api_hints.known_endpoints.append(
                    ApiEndpoint(
                        url_pattern=api_url,
                        json_paths=llm_hints.get("json_paths", {}),
                    )
                )
        
        # DOM hints from LLM
        css = llm_hints.get("css_selectors") or {}
        if css.get("container"):
            profile.dom_hints.field_selectors = FieldSelectorMap(
                container=css.get("container"),
                rent=css.get("rent"),
                sqft=css.get("sqft"),
                bedrooms=css.get("bedrooms"),
                bathrooms=css.get("bathrooms"),
                availability_date=css.get("availability_date"),
                unit_id=css.get("unit_id"),
            )
        
        if llm_hints.get("platform_guess"):
            profile.dom_hints.platform_detected = llm_hints["platform_guess"]
            profile.api_hints.api_provider = llm_hints["platform_guess"]
        
        if llm_hints.get("field_mapping_notes"):
            profile.llm_artifacts.field_mapping_notes = llm_hints["field_mapping_notes"]
    
    # Navigation hints from the actual crawl
    crawled = scrape_result.get("property_links_crawled", [])
    if crawled and not profile.navigation.availability_page_path:
        for url in crawled:
            path = urllib.parse.urlparse(url).path
            if any(k in path.lower() for k in ["floor", "plan", "avail", "rent", "unit"]):
                profile.navigation.availability_page_path = path
                break
    
    profile.updated_at = datetime.utcnow()
    profile.version += 1
    store.save(profile)
    return profile
```

**Step 3.2 — Create `services/drift_detector.py`**

```python
def detect_drift(
    profile: ScrapeProfile,
    units_extracted: int,
    scrape_result: dict,
) -> tuple[bool, list[str]]:
    """
    Compare extraction results to profile expectations.
    Returns (drift_detected: bool, reasons: list[str]).
    """
    reasons = []
    
    if profile.confidence.maturity == ProfileMaturity.COLD:
        return False, []  # No expectations to drift from
    
    expected = profile.confidence.last_unit_count
    
    # Unit count drop >30%
    if expected > 0 and units_extracted < expected * 0.7:
        reasons.append(f"unit_count_drop: expected ~{expected}, got {units_extracted}")
    
    # All fields null (extracted shells without data)
    if units_extracted > 0:
        units = scrape_result.get("units", [])
        null_rents = sum(1 for u in units if not u.get("rent_range") and not u.get("market_rent_low"))
        if null_rents == len(units) and len(units) > 0:
            reasons.append(f"all_rents_null: {null_rents}/{len(units)} units have no rent data")
    
    # Scrape timeout pattern
    if scrape_result.get("_timeout"):
        if profile.confidence.consecutive_failures >= 2:
            reasons.append("timeout_pattern: 3+ consecutive timeouts")
    
    return len(reasons) > 0, reasons


def apply_drift_demotion(profile: ScrapeProfile, reasons: list[str]) -> ScrapeProfile:
    """Demote profile maturity based on drift signals."""
    severe = any("all_rents_null" in r or "timeout_pattern" in r for r in reasons)
    
    if severe:
        profile.confidence.maturity = ProfileMaturity.COLD
        profile.confidence.consecutive_successes = 0
    elif profile.confidence.maturity == ProfileMaturity.HOT:
        profile.confidence.maturity = ProfileMaturity.WARM
        profile.confidence.consecutive_successes = 0
    
    return profile
```

**Step 3.3 — Integrate into `daily_runner.py`**

In the `run_daily()` function, modify the per-property processing loop. Find the section where `_scrape_in_thread()` is called. Before the scrape call, add profile loading. After extraction, add profile update and drift detection.

```python
# At top of file, add imports:
from services.profile_store import ProfileStore
from services.profile_updater import update_profile_after_extraction
from services.drift_detector import detect_drift, apply_drift_demotion

# Before the per-property loop:
profile_store = ProfileStore(Path("config/profiles"))

# Inside the loop, BEFORE the scrape call:
profile = profile_store.load(cid)
if profile is None:
    profile = profile_store.bootstrap_from_meta(cid, row, url)

# Pass profile to scrape_property (modify the _scrape_in_thread wrapper):
# Add profile=profile to the scrape_property() call

# AFTER transform_units_from_scrape(), add:
profile = update_profile_after_extraction(
    profile, scrape_result, len(target_units), profile_store
)

drift_detected, drift_reasons = detect_drift(profile, len(target_units), scrape_result)
if drift_detected:
    profile = apply_drift_demotion(profile, drift_reasons)
    profile_store.save(profile)
    per_prop_issues.append(V.warning(
        "PROFILE_DRIFT_DETECTED",
        f"drift detected: {'; '.join(drift_reasons)}",
        canonical_id=cid, row_index=idx,
    ))
```

Apply the same integration to `retry_runner.py` — the pattern is identical.

---

### Phase 4: Profile-guided routing

**Step 4.1 — Create `services/profile_router.py`**

```python
from models.scrape_profile import ScrapeProfile, ProfileMaturity

class RouteDecision:
    def __init__(self, skip_to_tier: Optional[int] = None, 
                 run_full_cascade: bool = True,
                 custom_timeout_ms: Optional[int] = None,
                 entry_url: Optional[str] = None,
                 block_domains: list[str] = None):
        self.skip_to_tier = skip_to_tier
        self.run_full_cascade = run_full_cascade
        self.custom_timeout_ms = custom_timeout_ms
        self.entry_url = entry_url
        self.block_domains = block_domains or []

def route(profile: ScrapeProfile) -> RouteDecision:
    """Determine extraction strategy from profile maturity."""
    
    if profile.confidence.maturity == ProfileMaturity.HOT:
        return RouteDecision(
            skip_to_tier=profile.confidence.preferred_tier,
            run_full_cascade=False,
            custom_timeout_ms=profile.navigation.timeout_ms,
            entry_url=profile.navigation.entry_url,
            block_domains=profile.navigation.block_resource_domains,
        )
    
    if profile.confidence.maturity == ProfileMaturity.WARM:
        return RouteDecision(
            skip_to_tier=profile.confidence.preferred_tier,  # try this first
            run_full_cascade=True,  # fall back to cascade if preferred fails
            custom_timeout_ms=profile.navigation.timeout_ms,
            entry_url=profile.navigation.entry_url,
            block_domains=profile.navigation.block_resource_domains,
        )
    
    # COLD: full cascade, no shortcuts
    return RouteDecision(
        run_full_cascade=True,
        custom_timeout_ms=profile.navigation.timeout_ms if profile.navigation.timeout_ms != 60000 else None,
        block_domains=profile.navigation.block_resource_domains,
    )
```

**Step 4.2 — Wire routing into `scrape_property()`**

At the top of `scrape_property()` in `entrata.py`, after browser context creation, apply route decisions:

- If `route_decision.block_domains` is non-empty, add `await page.route()` to abort requests to those domains
- If `route_decision.entry_url` is set, use it instead of `base_url` for the homepage load
- If `route_decision.custom_timeout_ms` is set, use it for `_goto_robust()` timeout
- If `route_decision.skip_to_tier` is set and `run_full_cascade` is False, skip directly to that tier's extraction logic

---

### Phase 5: Profile-guided DOM extraction

**Step 5.1 — Create `services/profile_dom_extractor.py`**

When a profile has `dom_hints.field_selectors.container` set, this function tries to extract units using those selectors before falling back to the generic Tier 3 templates.

```python
async def extract_with_profile_selectors(
    page: Page,
    selectors: FieldSelectorMap,
    base_url: str,
) -> list[dict]:
    """
    Extract units using profile-stored CSS selectors.
    Returns empty list if selectors don't match or produce no results.
    """
    if not selectors.container:
        return []
    
    try:
        containers = await page.query_selector_all(selectors.container)
        if not containers:
            return []
        
        units = []
        for container in containers:
            unit = {}
            for field, selector in [
                ("rent_range", selectors.rent),
                ("sqft", selectors.sqft),
                ("unit_number", selectors.unit_id),
                ("bedrooms", selectors.bedrooms),
                ("bathrooms", selectors.bathrooms),
                ("availability_date", selectors.availability_date),
                ("floor_plan_name", selectors.floor_plan_name),
            ]:
                if not selector:
                    continue
                try:
                    el = await container.query_selector(selector)
                    if el:
                        text = await el.inner_text()
                        unit[field] = text.strip()
                except Exception:
                    continue
            
            if unit.get("rent_range") or unit.get("unit_number") or unit.get("floor_plan_name"):
                # Normalize through existing parse functions
                from templates._common import parse_rent, parse_sqft
                if unit.get("rent_range"):
                    unit["rent_range"] = parse_rent(unit["rent_range"]) or unit["rent_range"]
                units.append(unit)
        
        return units
    except Exception:
        return []
```

Insert this into `scrape_property()` as a pre-check before the existing Tier 3 DOM parsing. If profile selectors produce results, use them and skip generic DOM.

---

### Phase 6: Unit tests

**Step 6.1 — Create `tests/test_scrape_profile.py`**

```
Test ScrapeProfile model:
- test_default_profile_is_cold
- test_profile_serialization_roundtrip (dump to JSON, load back, assert equal)
- test_field_selector_map_optional_fields
- test_api_endpoint_with_json_paths
```

**Step 6.2 — Create `tests/test_profile_store.py`**

```
Test ProfileStore (use tmp_path fixture):
- test_save_and_load_roundtrip
- test_load_nonexistent_returns_none
- test_save_increments_version
- test_save_creates_audit_copy
- test_bootstrap_from_meta_detects_rentcafe
- test_bootstrap_from_meta_detects_entrata
- test_bootstrap_from_meta_unknown_platform
- test_list_by_maturity
```

**Step 6.3 — Create `tests/test_llm_extractor.py`**

```
Test prepare_llm_input:
- test_html_trimming_removes_scripts_styles (provide sample HTML, assert <script> and <style> removed)
- test_html_trimming_preserves_content (assert main content div kept)
- test_api_response_ranking (provide 5 API responses, 2 with unit-like keys, assert top 2 selected)

Test extract_with_llm (mock the Azure OpenAI call):
- test_successful_extraction_returns_units_and_hints
- test_llm_returns_invalid_json_handled_gracefully
- test_llm_returns_empty_units_array
- test_units_normalized_through_parse_functions
```

**Step 6.4 — Create `tests/test_profile_updater.py`**

```
- test_update_after_tier1_success_records_api_urls
- test_update_after_llm_success_writes_css_selectors
- test_update_after_llm_success_writes_json_paths
- test_maturity_promotion_cold_to_warm_after_1_success
- test_maturity_promotion_warm_to_hot_after_3_successes
- test_consecutive_failures_resets_on_success
- test_navigation_hints_recorded_from_crawled_urls
```

**Step 6.5 — Create `tests/test_drift_detector.py`**

```
- test_no_drift_on_cold_profile (always returns False for COLD)
- test_unit_count_drop_30pct_detected
- test_all_rents_null_detected
- test_timeout_pattern_detected_after_3_failures
- test_severe_drift_demotes_to_cold
- test_mild_drift_demotes_hot_to_warm
- test_no_drift_no_demotion
```

**Step 6.6 — Create `tests/test_profile_router.py`**

```
- test_hot_profile_skips_to_preferred_tier
- test_warm_profile_tries_preferred_then_cascade
- test_cold_profile_runs_full_cascade
- test_custom_timeout_from_profile
- test_block_domains_from_profile
- test_entry_url_override
```

**Step 6.7 — Update `tests/test_smoke.py`**

Add smoke test cases:
```
- test_profile_store_creates_and_loads_profile
- test_llm_extractor_prepare_input_does_not_crash (provide minimal HTML)
- test_entrata_scrape_property_accepts_profile_param (import check — no actual scrape)
- test_profile_updater_handles_empty_scrape_result
- test_full_profile_lifecycle: create COLD → simulate 3 successes → assert HOT
```

---

## Conventions — follow these strictly

- **Python 3.11+**, Pydantic v2 (`model_dump(mode="json")`, not `.dict()`)
- **pytest-asyncio** with `asyncio_mode = "auto"` in `pyproject.toml`
- **Type hints** on all function signatures
- **No `browser.close()`** — always `context.close()` (existing convention in entrata.py)
- **`asyncio.Semaphore`** for any concurrent browser operations
- **`hashlib.sha256`** for deterministic hashing (API schema signatures, DOM structure hashes)
- **Imports:** Use relative imports within the `ma_poc` package. Use lazy imports for heavy modules (the `from services.llm_extractor import ...` inside the function body pattern shown above is intentional — it avoids importing Azure OpenAI SDK at module level for runs that don't need Tier 4).
- **Logging:** Use the existing `log = logging.getLogger(__name__)` pattern from `daily_runner.py`. Print statements in `entrata.py` are acceptable (matches existing convention there).
- **Error handling:** Every LLM call and external API call must be wrapped in try/except. Failures in profile operations must never crash the pipeline — log a warning and continue with the default cascade.

## File creation order

1. `models/scrape_profile.py`
2. `services/__init__.py` (empty)
3. `services/profile_store.py`
4. `services/llm_extractor.py`
5. `services/vision_extractor.py`
6. `services/profile_updater.py`
7. `services/drift_detector.py`
8. `services/profile_router.py`
9. `services/profile_dom_extractor.py`
10. `config/profiles/.gitkeep`, `config/profiles/_audit/.gitkeep`
11. `config/prompts/tier4_extraction.txt`
12. Modify `scripts/entrata.py` (add Tier 4-5, accept profile param)
13. Modify `scripts/daily_runner.py` (add profile loading, routing, updating)
14. Modify `scripts/retry_runner.py` (same profile integration)
15. Modify `scripts/validate_outputs.py` (add new issue codes)
16. `tests/test_scrape_profile.py`
17. `tests/test_profile_store.py`
18. `tests/test_llm_extractor.py`
19. `tests/test_profile_updater.py`
20. `tests/test_drift_detector.py`
21. `tests/test_profile_router.py`
22. Update `tests/test_smoke.py`

## Mandatory workflow

For each file: implement fully → write tests immediately → run tests → fix failures → run `python -m py_compile <file>` → move to next file. Do NOT implement all files first and test later.

## Validation gate

After all files are implemented and tests pass:

```bash
# All unit tests
pytest tests/ -v --tb=short

# Smoke test
pytest tests/test_smoke.py -v

# Type check (if mypy is available)
mypy models/ services/ --ignore-missing-imports

# Verify profile store works end-to-end
python -c "
from models.scrape_profile import ScrapeProfile, ProfileMaturity
from services.profile_store import ProfileStore
from pathlib import Path
store = ProfileStore(Path('config/profiles'))
p = store.bootstrap_from_meta('test_123', {'Website': 'https://example.com'}, 'https://example.com')
assert p.confidence.maturity == ProfileMaturity.COLD
store.save(p)
loaded = store.load('test_123')
assert loaded is not None
assert loaded.canonical_id == 'test_123'
print('✅ Profile store end-to-end OK')
"
```