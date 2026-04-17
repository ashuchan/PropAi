# CLAUDE_REFACTOR.md — PMS-First Scraping Pipeline Refactor

> **Read this whole document before writing any code.** This refactor has hard ordering constraints across phases and cross-file invariants that will break if you skip ahead.

## What this document is

A full refactor plan for the MA Rent Intelligence Platform scraping pipeline. The current pipeline (`scripts/daily_runner.py` + `scripts/entrata.py`) is PMS-agnostic by default and handles PMS-specific logic reactively, scattered across a 2,610-line file misleadingly named `entrata.py`. This refactor makes PMS detection the **first** operation per scrape, then routes to a dedicated per-PMS adapter, collapsing the cascade where possible and falling through to the existing generic extractor when needed.

This document is written for execution by Claude Code. You (Claude Code) are expected to:
- Read requirements fully before writing code
- Write tests **immediately** after each file, not at the end
- Run tests, static analysis, and gate validation after each phase
- Research PMS response structures by inspecting real captured data in `data/runs/*/raw_api/` and by searching the web — **do not guess the JSON shape of any PMS API**

## Non-negotiable principles

1. **Never-fail contract is preserved.** Every change must keep `daily_runner.py`'s guarantee that a single property's failure cannot crash the run. All exception handling must remain.
2. **Backward compatibility on state files.** `data/state/property_index.json`, `data/state/unit_index.json`, and `config/profiles/*.json` written by the old pipeline must continue to load without error after the refactor. New fields get defaults. No schema-breaking renames.
3. **The 46-key output schema is locked.** `data/runs/{date}/properties.json` shape is fixed by downstream consumers. Do not add or rename top-level keys without updating `scrape_properties.py` and flagging it in the report.
4. **LLM is the teacher, not the worker.** After this refactor the median property should make **zero** LLM calls per scrape once its profile is warm. LLM calls at runtime signal either a new property, a site change, or a bug — never a routine state.
5. **PMS adapters own their quirks.** Once a PMS has an adapter, no PMS-specific code (host blocklists, widget filters, ID-extraction regexes, expand-button patterns) should remain in the orchestrator or the generic extractor. Find every such scattered branch and move it.
6. **Research before code.** For every PMS adapter, before writing the parser, you must: (a) search `data/runs/*/raw_api/` for at least 3 real payloads from that PMS, (b) web-search the PMS's developer docs or public API references, (c) document findings in a comment block at the top of the adapter file. Adapters written from assumption get rejected at the gate.

## The problem this refactor solves (what you'll see if you run the current pipeline)

Real failure example from `data/runs/2026-04-15/reports/5317.md` (Tides on Southern, Mesa AZ):
- Homepage load failed with `ERR_SSL_PROTOCOL_ERROR`
- Pipeline still ran Tier 6 (LLM, $0.00505) and Tier 7 (Vision, $0.00870) on empty content
- Both LLMs reported "no content provided" — total waste: $0.01375
- Profile has `consecutive_failures: 8` but routing still runs full cascade
- `api_provider` has been `null` for 8 consecutive failed runs — nothing updates it, even though the URL alone tells us nothing about PMS

This refactor eliminates all four of those failures.

## Phase index

| Phase | Name | Deliverable | Depends on |
|---|---|---|---|
| 0 | Repo survey + baseline metrics | `docs/REFACTOR_BASELINE.md` | — |
| 1 | PMS detection module (offline) | `ma_poc/pms/detector.py` + tests | Phase 0 |
| 2 | Adapter interface + registry | `ma_poc/pms/adapters/base.py`, `registry.py` | Phase 1 |
| 3 | Adapter implementations (RentCafe, Entrata, AppFolio, OneSite, SightMap, Avalon, Generic) | `ma_poc/pms/adapters/*.py` | Phase 2 |
| 4 | CTA-hop + leasing-portal resolver | `ma_poc/pms/resolver.py` | Phase 3 |
| 5 | Scraper orchestrator rewrite | new `ma_poc/pms/scraper.py`, deprecate `scripts/entrata.py` | Phase 3, 4 |
| 6 | Profile v2 schema + migration | `ma_poc/models/scrape_profile.py`, migration script | Phase 5 |
| 7 | Property report v2 | `ma_poc/reporting/property_report.py` | Phase 6 |
| 8 | daily_runner.py integration | modified `scripts/daily_runner.py` | Phase 7 |
| 9 | Bug hunt + gate validation | `scripts/gate_refactor.py` | Phase 8 |

**Hard ordering rule**: do not start Phase N+1 until Phase N's gate script passes. Each phase has a gate in section [Gates](#gates) at the end.

---

## Phase 0 — Repo survey + baseline metrics

**Goal**: Produce a written baseline before touching any code, so later phases can be measured.

### Files to create

1. `docs/REFACTOR_BASELINE.md` — the survey document (described below)
2. `scripts/refactor_baseline.py` — a one-shot script that produces metrics from the most recent `data/runs/*/` directory

### `scripts/refactor_baseline.py` requirements

Reads the most recent run under `data/runs/` (by directory name sort) and produces:

- **Tier distribution**: count of properties by `extraction_tier_used`. Table with rows = tier, columns = count, % of total
- **LLM cost**: total $ spent, properties that used LLM, properties that used Vision, avg cost per LLM-using property
- **Failure breakdown**: properties with `extraction_tier_used == "FAILED"`, grouped by the first error string (first 80 chars)
- **Profile maturity distribution**: from `config/profiles/*.json`, count by `confidence.maturity`
- **Profiles with `api_provider == null`**: absolute count and % of total profiles
- **Time breakdown**: if `scraped_at` and the report markdown has timing, report avg and p95 scrape duration
- **Redundant LLM calls**: properties where `UNITS_EMPTY` issue coexists with `LLM Calls Made > 0` (wasted calls on content-less scrapes — the bug we saw at property 5317)

Output: writes a markdown table to stdout AND appends to `docs/REFACTOR_BASELINE.md` with timestamp.

### `docs/REFACTOR_BASELINE.md` structure

```markdown
# Refactor Baseline — captured <ISO date>

## Current-pipeline metrics
<output of scripts/refactor_baseline.py>

## Known PMS distribution in current property set
<fill manually from handoff doc + CSV inspection>

## Target metrics after refactor (hypothesis)
- Tier-1 success rate: current X% → target Y%
- LLM calls per property (median): current 0-3 → target 0
- LLM $ per daily run: current $A → target $B
- Redundant-call count: current N → target 0
- `api_provider == null` profiles: current M% → target <10%
```

You fill in current X/A/N/M from the script; leave target numbers for the human to approve.

### Named tests (write these first, before the script)

| Test | File | What it checks |
|---|---|---|
| `test_baseline_finds_latest_run` | `tests/refactor/test_baseline.py` | Given a tree with `data/runs/2026-04-13` and `data/runs/2026-04-15`, picks 04-15 |
| `test_baseline_tier_distribution` | same | On a fixture with 3 SUCCESS and 2 FAILED properties, produces correct table |
| `test_baseline_llm_wasted_calls` | same | Property 5317-like fixture (UNITS_EMPTY + 2 LLM calls + SSL error) is counted as a wasted-call case |
| `test_baseline_handles_missing_profile_dir` | same | Script runs even if `config/profiles/` doesn't exist, reporting 0 profiles |

### Gate

`scripts/gate_refactor.py phase 0` passes iff:
- `docs/REFACTOR_BASELINE.md` exists and its `## Current-pipeline metrics` section is non-empty
- All 4 tests pass
- `scripts/refactor_baseline.py` runs to completion on `data/runs/*` without raising

**Do not proceed to Phase 1 until this gate is green.**

---

## Phase 1 — PMS detection module (offline, no network)

**Goal**: A single function that returns a `DetectedPMS` from cheap inputs (URL, CSV row, optional captured-homepage HTML). No network calls at this stage. Live HTTP probing is Phase 4.

### The module

Create `ma_poc/pms/__init__.py` (empty) and `ma_poc/pms/detector.py`.

### Public API

```python
class DetectedPMS:
    pms: Literal["rentcafe", "entrata", "appfolio", "onesite",
                 "sightmap", "realpage_oll", "avalonbay",
                 "squarespace_nopms", "wix_nopms", "custom", "unknown"]
    confidence: float                    # 0.0–1.0
    evidence: list[str]                  # human-readable reasons
    pms_client_account_id: str | None    # the cluster key — see Phase 6
    recommended_strategy: Literal[
        "api_first", "jsonld_first", "dom_first",
        "portal_hop", "syndication_only", "cascade"
    ]

def detect_pms(
    url: str,
    csv_row: dict | None = None,
    page_html: str | None = None,
) -> DetectedPMS: ...
```

### Detection signals (in priority order — first confident hit wins)

You (Claude Code) need to research these. The handoff document in this project has confirmed fingerprints; use those as fixtures. For each PMS below, **web-search the PMS's developer docs** or public rent-site listings to confirm the URL patterns are current.

1. **URL host fingerprints** — `{id}.onlineleasing.realpage.com` → OneSite; `*.rentcafe.com` → RentCafe; `*.sightmap.com` → SightMap; `avaloncommunities.com` → Avalon
2. **URL extension fingerprints** — `.aspx` path on a non-Microsoft vanity domain → RentCafe/Yardi (strong signal; Entrata and AppFolio never emit `.aspx`)
3. **CSV management-company priors** — build a dict `MGMT_TO_PMS_PRIOR` mapping known management companies to their typical PMS. Start with: Mark-Taylor → entrata, Lindsey Management → rentcafe, AvalonBay Communities → avalonbay, Greystar → unknown (too diverse to prior). Document the source of each entry in a comment.
4. **HTML markers** (only if `page_html` is passed) — look for `entrata.com` string, `/Apartments/module/`, `data-property-id`, `rentcafe`, `RENTCafe`, RealPage OneSite markers. These are the markers already present in `scripts/entrata.py` lines 1441–1464 — extract them rather than re-researching

5. **Platform giveaway scripts** — `<script src="...squarespace.com/...">` → `squarespace_nopms`; `<script src="...wix.com/...">` or `static.parastorage.com` → `wix_nopms`

### Recommended-strategy mapping

| PMS | strategy |
|---|---|
| rentcafe | `jsonld_first` |
| entrata | `api_first` (with widget + POST probe) |
| appfolio | `api_first` |
| onesite | `api_first` (split endpoints) |
| sightmap | `api_first` |
| realpage_oll | `portal_hop` |
| avalonbay | `api_first` |
| squarespace_nopms, wix_nopms | `syndication_only` |
| custom, unknown | `cascade` |

### `pms_client_account_id` extraction

For each PMS where extractable from URL alone, write a rule. Examples you must research and confirm:
- OneSite: numeric prefix of subdomain — `8756399.onlineleasing.realpage.com` → `"8756399"`
- RentCafe: you will need to inspect captured HTML to find the client ID pattern. Search `data/runs/*/raw_api/` for rentcafe.com payloads and look for a consistent property identifier
- Entrata: the regex at `scripts/entrata.py` line 1480 captures `\d{3,8}` from path — refine it using 5+ real Entrata URLs from the captured data

**Do not hard-code a pattern you haven't seen in at least 3 real URLs.** Document the URLs you used as fixtures in a comment at the top of each extraction rule.

### Named tests

All tests go in `tests/pms/test_detector.py`. Use the 9 confirmed fingerprints from the handoff document as fixtures; add more as you find them in `data/runs/*/raw_api/`.

| Test | Input | Expected |
|---|---|---|
| `test_detect_onesite_from_subdomain` | `https://8756399.onlineleasing.realpage.com/#k=44781` | pms=onesite, conf≥0.95, client_id="8756399" |
| `test_detect_rentcafe_from_host` | `https://www.rentcafe.com/apartments/mi/ann-arbor/woodview-commons0/` | pms=rentcafe, conf≥0.95 |
| `test_detect_rentcafe_from_aspx_vanity` | `https://fairwaysatfayetteville.apartments/floorplans.aspx`, csv=None | pms=rentcafe, conf≥0.70 (via .aspx heuristic) |
| `test_detect_entrata_from_mgmt_prior` | `https://sanartesapartmentsscottsdale.com`, csv={"Management Company": "Mark-Taylor"} | pms=entrata, conf≥0.70 |
| `test_detect_avalonbay_from_host` | `https://www.avaloncommunities.com/new-jersey/west-windsor-apartments/avalon-w-squared/` | pms=avalonbay, conf≥0.95 |
| `test_detect_sightmap_from_host` | `https://tour.sightmap.com/embed/abc123` | pms=sightmap, conf≥0.95 |
| `test_detect_squarespace_nopms_from_html` | url=`https://83freight.com`, page_html=snippet with squarespace script | pms=squarespace_nopms |
| `test_detect_unknown_returns_cascade_strategy` | a domain with no signals | pms=unknown, strategy="cascade" |
| `test_detect_evidence_populated_for_every_result` | any input | `evidence` list is non-empty for all confidence>0 results |
| `test_detect_never_raises` | malformed URL `"not a url"`, `None` csv, `None` html | returns `DetectedPMS(pms="unknown", confidence=0.0, ...)` — no exception |
| `test_mgmt_prior_case_insensitive` | csv `{"Management Company": "MARK-TAYLOR"}` | same result as `"Mark-Taylor"` |
| `test_client_id_extraction_onesite_matches_3_real_urls` | 3 real OneSite URLs from the handoff/raw_api | all extract a numeric string |
| `test_html_none_path_doesnt_break_detection` | csv with signals, `page_html=None` | URL+CSV detection still works |

### Gate

`scripts/gate_refactor.py phase 1` passes iff:
- All 13 tests pass
- `mypy ma_poc/pms/detector.py` returns no errors (strict mode)
- `ruff check ma_poc/pms/detector.py` returns no issues
- Each PMS in the `DetectedPMS.pms` literal has at least one passing test that returns it
- The adapter file's top-of-file comment block lists the data sources consulted (at least: which raw_api fixtures, which web searches — Claude Code fills this in as it researches)

---

## Phase 2 — Adapter interface + registry

**Goal**: A protocol that every PMS adapter implements, and a registry that maps detection results to adapters.

### Files to create

1. `ma_poc/pms/adapters/__init__.py` — registers all adapters
2. `ma_poc/pms/adapters/base.py` — the protocol + shared types
3. `ma_poc/pms/adapters/registry.py` — the lookup

### `base.py` — the protocol

```python
from typing import Protocol, runtime_checkable
from playwright.async_api import Page

@dataclass
class AdapterContext:
    base_url: str
    detected: DetectedPMS                 # from Phase 1
    profile: ScrapeProfile | None         # may be None for COLD
    expected_total_units: int | None
    property_id: str                      # canonical_id, for LLM cost accounting

@dataclass
class AdapterResult:
    units: list[dict]                     # empty list = no units found, not an error
    tier_used: str                        # e.g. "TIER_1_API_ENTRATA_WIDGET"
    winning_url: str | None               # URL/endpoint that produced data
    api_responses: list[dict]             # raw captures, for profile learning
    blocked_endpoints: list[tuple[str, str]]   # (url_pattern, reason)
    llm_field_mappings: list[dict]        # if Phase-5-equivalent ran
    errors: list[str]
    confidence: float                     # 0–1; adapter's own confidence in the result

@runtime_checkable
class PmsAdapter(Protocol):
    pms_name: str                         # must match DetectedPMS.pms literals

    async def extract(self, page: Page, ctx: AdapterContext) -> AdapterResult: ...

    def static_fingerprints(self) -> list[str]:
        """Host/path substrings that uniquely identify this PMS.
        Used by the orchestrator's post-run PMS inference and by the
        detector's URL-match step. No network access."""
        ...
```

### `registry.py` — the lookup

```python
def get_adapter(pms: str) -> PmsAdapter: ...
def all_adapters() -> list[PmsAdapter]: ...
def register(adapter: PmsAdapter) -> None: ...
```

Registry must be populated at import time from `ma_poc/pms/adapters/__init__.py`. When an unknown PMS is requested, return the Generic adapter (Phase 3).

### Named tests — `tests/pms/test_registry.py`

| Test | Check |
|---|---|
| `test_registry_has_adapter_for_each_pms_literal` | For every string in `DetectedPMS.pms` except "unknown" and "custom", `get_adapter(pms)` returns a real adapter |
| `test_registry_returns_generic_for_unknown` | `get_adapter("unknown")` returns adapter with `pms_name == "generic"` |
| `test_registry_returns_generic_for_custom` | Same for "custom" |
| `test_adapter_names_match_pms_literals` | Every registered adapter's `pms_name` is one of the valid literals |
| `test_protocol_structural_match` | Each registered adapter passes `isinstance(a, PmsAdapter)` (runtime_checkable) |
| `test_register_prevents_duplicate_names` | Registering two adapters with same name raises |

### Gate

`scripts/gate_refactor.py phase 2` passes iff:
- All 6 tests pass
- `mypy ma_poc/pms/adapters/` clean
- Every literal in `DetectedPMS.pms` (except "unknown", "custom") maps to an adapter module file in `ma_poc/pms/adapters/` — even if the adapter is a stub that raises `NotImplementedError` until Phase 3
- Stubs count as passing Phase 2 gate; they are required to be real in Phase 3 gate

---

## Phase 3 — Adapter implementations

**Goal**: Implement each adapter as a standalone module. **No shared state, no cross-adapter imports.** Every adapter is self-contained and can be tested in isolation.

For each adapter, follow this sub-phase pattern:

1. **Research first** (mandatory — gate blocks if skipped)
   - Find ≥3 real payloads from this PMS in `data/runs/*/raw_api/`. If fewer than 3 exist, document this and note the gap — do not fabricate
   - Web-search the PMS's API documentation or public reverse-engineering write-ups. Record sources as bullet points in the adapter's top-of-file comment
   - Identify: the entry URL pattern, the API endpoint(s) that return unit data, the JSON envelope shape, the unit-level field names
2. **Write the adapter** — `ma_poc/pms/adapters/<pms>.py`
3. **Write the tests** — `tests/pms/adapters/test_<pms>.py`
4. **Run** — all tests pass
5. **Gate** — per-adapter mini-gate before moving to next adapter

### Adapter-file top-of-file template

Every adapter file starts with this comment block:

```python
"""
<PMS name> adapter.

Research log
------------
Web sources consulted:
  - <URL 1> (accessed <date>)
  - <URL 2>
Real payloads inspected (from data/runs/*/raw_api/):
  - <cid1> — <one-line summary of API shape>
  - <cid2>
  - <cid3>
Key findings:
  - API endpoint: <pattern>
  - Response envelope: <path to units list>
  - Unit ID field: <field name>
  - Rent field(s): <field names, noting range vs flat>
  - Known gotchas: <e.g. "null /units response when no availability">
"""
```

The gate script parses this block and fails if any section is empty.

### Order of implementation

Do adapters in this order. Earlier ones have the most real captures and will be easiest; later ones will teach you what's missing from the base protocol, so catching that early is good.

1. `entrata.py` — already partially implemented in `scripts/entrata.py`; port the widget filter, POST probe, and property-ID extraction
2. `sightmap.py` — `_parse_sightmap_payload` at `scripts/entrata.py:433` is the reference implementation; port it directly
3. `rentcafe.py` — research needed (Yardi-hosted, JSON-LD heavy)
4. `appfolio.py` — research needed
5. `onesite.py` — research needed; split-endpoint pattern (`/floorplans` + `/units`, `/units` can be null)
6. `avalonbay.py` — research needed; single-REIT custom stack on `avaloncommunities.com`
7. `generic.py` — the fallback; contains the current 7-phase cascade from `scripts/entrata.py` **with** PMS-specific branches removed (widget filter, SightMap parser, Entrata probe all moved to their adapters)

### Per-adapter tests — minimum set

Every adapter must have these tests. Name them `test_<pms>_<aspect>`:

| Aspect | What it checks |
|---|---|
| `extract_happy_path` | Given a real captured payload, `extract()` returns ≥1 unit with non-null `floor_plan_name` and either rent or unit_id |
| `extract_from_stored_fixture` | Uses a saved fixture under `tests/pms/adapters/fixtures/<pms>/<cid>.json`. Fixture is a real captured payload, checked into repo |
| `extract_returns_empty_list_on_no_data` | Given a payload with no unit list (e.g. chatbot config), returns `AdapterResult(units=[], ...)` — **not** raises |
| `extract_handles_null_units_response` | OneSite-specific; RentCafe may also have this. Empty `/units` response → floor-plan-level records with rent=null |
| `static_fingerprints_nonempty` | `static_fingerprints()` returns ≥1 host/path string |
| `tier_used_label_is_pms_specific` | `tier_used` contains the PMS name (e.g. `TIER_1_API_RENTCAFE`) |
| `unit_id_format_valid` | If adapter emits unit_id, it matches expected regex for that PMS (document regex in adapter's research log) |
| `rent_within_sanity_range` | All emitted rents are in `[200, 50000]` or null |

**Fixtures requirement**: every adapter's `tests/pms/adapters/fixtures/<pms>/` directory must contain ≥2 real captured JSON payloads from `data/runs/*/raw_api/`, renamed to `<canonical_id>.json`. If fewer than 2 exist, the adapter is marked `research-blocked` and cannot pass its gate until the human provides more data.

### Generic adapter — special requirements

`generic.py` is the fallback. It contains what remains of the current cascade after PMS-specific code is extracted. Its responsibilities:
- Tier 1: generic `parse_api_responses` (from `scripts/entrata.py:503` minus the SightMap branch, minus the Entrata widget branch)
- Tier 1.5: `extract_embedded_json`
- Tier 2: JSON-LD
- Tier 3: DOM parsing with rent-signal heuristic
- Tier 4: legacy LLM (Phase 6b of current code) — **but only if `ctx.detected.pms == "unknown"`**. Never run for detected PMSs; an adapter failing means something specific is wrong, and LLM fallback would mask it
- Tier 5: Vision — same rule as Tier 4

When `ctx.detected.pms != "unknown"` and generic is invoked as a fallback from an adapter, the LLM/Vision tiers are skipped. This is enforced by a check at the top of `extract()`.

### Per-adapter gates

For each adapter, a mini-gate must pass before moving to the next:
- All named tests pass
- Top-of-file research log has all four sections (Web sources, Real payloads, Key findings — ≥1 bullet each)
- `mypy` strict on the adapter file
- At least 2 fixture files exist under `tests/pms/adapters/fixtures/<pms>/`

### Phase 3 overall gate

`scripts/gate_refactor.py phase 3` passes iff:
- Every per-adapter gate passes
- `tests/pms/adapters/` tests all pass
- No adapter imports another adapter (checked by static scan: `grep "from ma_poc.pms.adapters" ma_poc/pms/adapters/*.py` returns only `from ma_poc.pms.adapters.base import ...`)
- No PMS-specific host string (e.g. `"sightmap.com"`, `"rentcafe.com"`) remains in `generic.py` — checked by grep; list of banned strings: `sightmap`, `rentcafe`, `appfolio`, `entrata`, `avaloncommunities`, `onlineleasing`
- The only place each banned string appears, outside its own adapter, is `ma_poc/pms/detector.py`

---

## Phase 4 — CTA-hop + leasing-portal resolver

**Goal**: Turn the observation from the handoff document (100% of properties in the 77-sample are vanity domains, not direct PMS-hosted URLs) into a Phase-0-time resolver. Before any adapter runs, if we've landed on a marketing site, follow the CTA to the PMS subdomain and re-detect.

### Files to create

`ma_poc/pms/resolver.py`

### Public API

```python
@dataclass
class ResolvedTarget:
    original_url: str
    resolved_url: str                     # may equal original_url if no hop needed
    hop_path: list[str]                   # URLs traversed, in order
    final_detection: DetectedPMS
    method: Literal["no_hop", "cta_link", "iframe", "redirect", "failed"]

async def resolve_target(
    page: Page,
    original_url: str,
    initial_detection: DetectedPMS,
) -> ResolvedTarget: ...
```

### Algorithm

1. If `initial_detection.pms` is a known PMS with confidence ≥ 0.85 and the URL host already matches the PMS's `static_fingerprints()`, return `no_hop`. Examples: URL is already `*.rentcafe.com`, URL is already `{id}.onlineleasing.realpage.com`.
2. Else: load homepage, extract hrefs of anchors matching `/apply|availab|floor\s*plan|lease|resident.*portal/i` (use the existing `_AVAILABILITY_ANCHOR_RE` from `scripts/entrata.py:165` — move it here)
3. For each candidate anchor target (cap at 5, ordered by priority: `availab` > `floor plan` > `apply` > rest):
   - Run `detect_pms(target_url, csv_row)` on the target
   - If any adapter's `static_fingerprints()` matches the target host, return a `ResolvedTarget` with `method="cta_link"`, `resolved_url=target_url`, and the new detection
4. Else: look for iframes pointing to `_LEASING_PORTAL_DOMAINS` (reuse existing set from `scripts/entrata.py:1216`). First match wins, `method="iframe"`
5. Else: check for meta-refresh or JS redirect chains Playwright captured during load. If final page URL differs from `original_url` and matches a PMS fingerprint, `method="redirect"`
6. Else: return `ResolvedTarget(..., final_detection=initial_detection, method="failed")`

### Named tests — `tests/pms/test_resolver.py`

Fixtures: use Playwright `page.route` to mock the homepage HTML. Do not hit the live internet in tests.

| Test | Fixture | Expected |
|---|---|---|
| `test_resolver_skips_hop_when_already_on_pms` | URL `https://8756399.onlineleasing.realpage.com/...`, detection=onesite/0.95 | method="no_hop", resolved_url == original_url |
| `test_resolver_finds_rentcafe_via_apply_button` | Vanity HTML with `<a href="https://www.rentcafe.com/.../foo/">Apply</a>` | method="cta_link", final_detection.pms=="rentcafe" |
| `test_resolver_finds_sightmap_iframe` | HTML with `<iframe src="https://tour.sightmap.com/embed/X">` | method="iframe", final_detection.pms=="sightmap" |
| `test_resolver_prioritizes_availability_over_apply` | HTML with both `<a>View Availability</a>` → rentcafe and `<a>Apply Now</a>` → onesite | resolves to rentcafe (availability wins by priority) |
| `test_resolver_returns_failed_when_nothing_found` | Vanity HTML with no PMS links, no iframes | method="failed", `final_detection` unchanged |
| `test_resolver_caps_candidates_at_5` | HTML with 20 Apply buttons → mock would be hit 20 times if uncapped; assert only 5 fetches |
| `test_resolver_handles_playwright_timeout` | Mock `page.goto` raising TimeoutError | returns method="failed", does not propagate exception |
| `test_resolver_records_hop_path` | 3-hop chain (vanity → subdomain → portal) | `hop_path` list has 3 entries in order |

### Gate

`scripts/gate_refactor.py phase 4` passes iff:
- All 8 tests pass
- `_AVAILABILITY_ANCHOR_RE` and `_LEASING_PORTAL_DOMAINS` are **removed** from `scripts/entrata.py` (checked by grep)
- `mypy` strict passes on `resolver.py`

---

## Phase 5 — Scraper orchestrator rewrite

**Goal**: Replace the 2,610-line `scripts/entrata.py` with a thin orchestrator that calls Phase 1 (detect) → Phase 4 (resolve) → Phase 3 (adapter.extract) → fallback to generic only if adapter returns zero units.

### Files to create / modify

- Create `ma_poc/pms/scraper.py` — the new orchestrator
- Modify `scripts/entrata.py` → reduce to a thin shim that imports and calls `ma_poc.pms.scraper.scrape`. Keep the old function name `scrape` so `daily_runner.py` doesn't break mid-refactor (Phase 8 is where daily_runner gets updated)
- Keep `_parse_sightmap_payload`, `parse_api_responses`, `extract_embedded_json`, `parse_jsonld`, `parse_dom`, `probe_entrata_api` working as module-level functions, but move each to its proper location: parsers go into their adapters, utility parsers stay in `generic.py`

### New `scrape` signature (kept compatible with the current one)

```python
async def scrape(
    base_url: str,
    proxy: str | None = None,
    profile: ScrapeProfile | None = None,
    expected_total_units: int | None = None,
) -> dict: ...
```

Return dict must have **all** the keys the current `scrape()` returns (list them from `scripts/entrata.py:1956-1976`), plus new ones:
- `_detected_pms` — the `DetectedPMS` serialized to dict
- `_resolved_target` — the `ResolvedTarget` serialized to dict
- `_adapter_used` — the adapter's `pms_name`
- `_fallback_chain` — list of adapters attempted, in order, with their confidence/unit counts

### Orchestration logic

```
1. Normalize http→https (existing logic)
2. initial_detection = detect_pms(base_url, profile.meta, page_html=None)
3. Launch browser, register response handler (existing)
4. Load homepage with _goto_robust
5. Capture page_html, re-run detect_pms with html for stronger detection
6. resolved = await resolve_target(page, base_url, strong_detection)
7. if resolved.resolved_url != base_url:
       navigate to resolved.resolved_url
       capture new page_html, re-detect
8. adapter = get_adapter(final_detection.pms)
9. result = await adapter.extract(page, ctx)
10. if result.units: return
11. if final_detection.pms != "unknown" and result.units == []:
        generic = get_adapter("generic")
        result = await generic.extract(page, ctx_with_skip_llm=True)
12. if still no units and final_detection.pms == "unknown":
        # full cascade including LLM, but only for truly unknown sites
        generic.extract(page, ctx_with_skip_llm=False)
13. Serialize result to the legacy dict shape + new _detected_pms keys
```

### Key simplification: kill the redundant invocations

The current code calls `probe_entrata_api()` on every Phase-4 link (`scripts/entrata.py:2272`) and `detect_leasing_portals()` on every Phase-4 link (`scripts/entrata.py:2285`). In the new orchestrator, these are:
- `probe_entrata_api` → moved into `adapters/entrata.py`, called once by that adapter
- `detect_leasing_portals` → moved into `resolver.py`, called once by the resolver in Phase 4

Confirm they are no longer called inside a per-link loop after refactor.

### The broken-page early exit (fixes the property 5317 bug)

At step 4, after `_goto_robust`, if `results["errors"]` contains any of:
- `ERR_SSL_PROTOCOL_ERROR`, `ERR_CONNECTION_TIMED_OUT`, `ERR_TOO_MANY_REDIRECTS`, `ERR_NAME_NOT_RESOLVED`, `net::ERR_ABORTED`

...set `_detected_pms.pms = "unreachable"` and **return immediately** with `units=[]`, `extraction_tier_used="FAILED_UNREACHABLE"`. Do not run any adapter, do not run LLM, do not run Vision. This alone eliminates the $0.01375 wasted per scrape on sites like 5317.

### Named tests — `tests/pms/test_scraper.py`

Use Playwright's `page.route` and an in-memory fake `PmsAdapter` that records calls.

| Test | Check |
|---|---|
| `test_orchestrator_detects_then_calls_correct_adapter` | Mock rentcafe homepage → rentcafe adapter's `extract` is called once, generic never |
| `test_orchestrator_falls_through_to_generic_when_adapter_empty` | Mock rentcafe adapter returns `units=[]` → generic adapter is called with `skip_llm=True` |
| `test_orchestrator_runs_llm_only_for_unknown_pms` | Unknown-PMS site with adapter returning empty → generic with `skip_llm=False` |
| `test_orchestrator_never_runs_llm_for_detected_pms_failure` | Known-PMS site where everything fails → no LLM call recorded |
| `test_orchestrator_skips_everything_on_ssl_error` | Mock SSL error on homepage → returns `FAILED_UNREACHABLE`, 0 LLM calls, 0 adapter calls |
| `test_orchestrator_skips_everything_on_dns_error` | `ERR_NAME_NOT_RESOLVED` → same |
| `test_orchestrator_hop_to_pms_subdomain` | Vanity URL with rentcafe CTA → final adapter call uses resolved URL |
| `test_orchestrator_preserves_legacy_result_keys` | Returned dict contains all keys from a snapshot of the current `scrape()` return |
| `test_orchestrator_adds_new_detection_keys` | Returned dict contains `_detected_pms`, `_resolved_target`, `_adapter_used`, `_fallback_chain` |
| `test_orchestrator_hot_profile_skips_detection` | If `profile.confidence.maturity == "HOT"` and `profile.api_hints.api_provider` is set, use that PMS without re-detecting |
| `test_probe_entrata_not_called_for_rentcafe` | RentCafe site → Entrata API probe is never invoked (statically checked: `probe_entrata_api` not called) |

### Gate

`scripts/gate_refactor.py phase 5` passes iff:
- All 11 tests pass
- `scripts/entrata.py` has been reduced to <100 lines (a shim) — line-count check in gate script
- No adapter-specific host string appears in `ma_poc/pms/scraper.py` (grep check, same banned list as Phase 3)
- All legacy return keys preserved — checked against a JSON snapshot at `tests/pms/snapshots/legacy_scrape_keys.json`

---

## Phase 6 — Profile v2 schema + migration

**Goal**: The current profile is well-modelled but has pieces that are not being used (`cluster_id`) and pieces that grow without bound (`blocked_endpoints`, `llm_field_mappings`). This phase makes the profile leaner, forces `api_provider` to be populated, and adds a migration that walks every existing profile and upgrades it in place.

### Schema changes

Edit `ma_poc/models/scrape_profile.py`. All changes must preserve backward-compat: old JSON files must deserialize (use `model_config = ConfigDict(extra="ignore")` on all models, which may already be set).

| Field path | Change | Reason |
|---|---|---|
| `api_hints.api_provider` | Now **required**, default `"unknown"` (was `None`) | Enforce that detection always ran |
| `api_hints.client_account_id` | **NEW**, `str | None` | The cluster key from Phase 1 |
| `dom_hints.platform_detected` | Remove (duplicates `api_provider`) | Deduplication |
| `navigation.explored_links` | Cap at 50 entries, LRU | Current impl is unbounded |
| `api_hints.blocked_endpoints` | Cap at 50 (already documented as such, verify) | Unbounded-growth risk |
| `api_hints.llm_field_mappings` | Cap at 20 (documented, verify) | Same |
| `cluster_id` | Remove from schema | Never implemented, dead field |
| `confidence.last_success_detection` | **NEW**, `DetectedPMS | None` | Stores the PMS used on the last success, for HOT-path routing |
| `confidence.consecutive_unreachable` | **NEW**, `int`, default 0 | Counts ERR_SSL/DNS failures distinct from `consecutive_failures` |
| `stats` | **NEW** section | See below |

### New `stats` section

```python
class ProfileStats(BaseModel):
    total_scrapes: int = 0
    total_successes: int = 0
    total_failures: int = 0
    total_llm_calls: int = 0
    total_llm_cost_usd: float = 0.0
    last_tier_used: str | None = None
    last_unit_count: int = 0
    p50_scrape_duration_ms: int | None = None
    p95_scrape_duration_ms: int | None = None
```

These are cheap to maintain and let you answer "is this property expensive" without reading run reports.

### Migration script

Create `scripts/migrate_profiles_v1_to_v2.py`.

- Reads every file under `config/profiles/*.json`
- For each: loads as v1, transforms, writes as v2, keeps v1 copy under `config/profiles/_audit/<cid>_v1.json`
- Transformations:
  - If `api_hints.api_provider` is null: **run `detect_pms(entry_url, csv_row_if_available, None)` to populate it**. Evidence goes into a new `api_hints.api_provider_source` field with value `"migration_detect"`
  - If both `api_hints.api_provider` and `dom_hints.platform_detected` are set and different: log a warning, keep the `api_hints` value
  - Drop `cluster_id`
  - Initialize `stats` to zeros (we can't reconstruct historical stats; fine to start at 0)
- Summary report at the end: counts by (was_unknown, now_detected_pms)

### Named tests — `tests/profile/test_migration.py`

| Test | Check |
|---|---|
| `test_migration_populates_api_provider_from_url` | v1 profile with `entry_url=<onesite URL>` and null provider → v2 has `api_provider="onesite"` |
| `test_migration_preserves_llm_field_mappings` | v1 with 3 mappings → v2 has same 3 mappings |
| `test_migration_drops_cluster_id` | v1 with `cluster_id="X"` → v2 deserializes without cluster_id |
| `test_migration_caps_explored_links_at_50` | v1 with 200 explored links → v2 has 50 most recent |
| `test_migration_audit_copy_written` | After migration, `config/profiles/_audit/<cid>_v1.json` exists with original content |
| `test_migration_is_idempotent` | Running twice produces same result as once |
| `test_v2_profile_loads_old_v1_json` | Directly load an untouched v1 JSON with `ScrapeProfile.model_validate` → succeeds with defaults |
| `test_stats_zero_initialized` | Fresh v2 profile has `stats.total_scrapes == 0` |
| `test_consecutive_unreachable_increments_on_ssl` | Feed profile an SSL error outcome → counter increments, `consecutive_failures` does not |
| `test_hot_profile_must_have_api_provider` | A profile with `maturity="HOT"` and `api_provider="unknown"` raises a validation warning |

### Gate

`scripts/gate_refactor.py phase 6` passes iff:
- All 10 tests pass
- `scripts/migrate_profiles_v1_to_v2.py` runs on the full `config/profiles/` without raising, and produces a summary with: (a) count of profiles processed, (b) count where `api_provider` moved from unknown to known
- After migration, count of profiles with `api_provider == "unknown"` is <10% of total (the 90%+ target from the handoff's "detection works" hypothesis)
- Every v1 profile has a corresponding `_audit/<cid>_v1.json` file

---

## Phase 7 — Property report v2

**Goal**: Make the per-property markdown report a useful debugging artifact, not a data dump. Current report (see `5317.md` for reference) has two problems: (1) it buries the signal in LLM prompt/response transcripts that are rarely what you need, and (2) it doesn't surface the detection decision at all — so when a scrape fails, the first thing you want to know ("what PMS did we think this was and why") is missing.

### Files to modify / create

- `ma_poc/reporting/property_report.py` — new module (currently embedded in `scripts/scrape_report.py`; extract it)
- `scripts/scrape_report.py` — reduce to thin shim calling the new module

### New report structure

```markdown
# <property name> — <run_date>

## Status
| | |
|---|---|
| **Verdict** | SUCCESS | FAILED_UNREACHABLE | FAILED_NO_DATA | CARRY_FORWARD |
| Canonical ID | 5317 |
| Units extracted | 46 |
| Scrape duration | 8.2s |
| LLM cost | $0.00 |

## Detection
| | |
|---|---|
| Detected PMS | entrata (confidence 0.82) |
| Evidence | mgmt=Mark-Taylor → entrata prior; /Apartments/module/ marker in HTML |
| Client account ID | 12345 |
| Resolved URL | https://sanartes.entrata.com/ (hopped from vanity) |
| Adapter used | entrata |
| Profile maturity | WARM (2 successes, 0 failures) |

## Pipeline
| Step | Outcome | Duration | Notes |
|---|---|---|---|
| URL normalize | https → https | 0ms | |
| Offline detection | entrata/0.70 | 2ms | mgmt prior |
| Homepage load | OK | 1.8s | |
| Online detection | entrata/0.82 | 5ms | + /Apartments/ marker |
| CTA hop | no_hop | 0ms | already on entrata subdomain |
| Adapter.extract | 46 units (TIER_1_API_ENTRATA_WIDGET) | 6.2s | |

## Changes since last run
- +2 new units (1204, 1307)
- 1 unit rent changed (1101: $2800 → $2850)
- 1 unit disappeared (0805)

## Issues
(only shown if any)

## LLM calls
(only shown if any — one line per call: tier, tokens, cost, outcome)

<details>
<summary>Raw LLM transcripts (<N> calls)</summary>
<!-- Existing detailed transcripts go here, collapsed by default -->
</details>
```

### Key principles for the report

- **Inverted pyramid.** Verdict at the top, detection next (this is the most common debugging question), pipeline steps third, LLM only if used.
- **LLM transcripts collapsed.** The current 5317.md report puts the full LLM prompts inline. After refactor, they go inside `<details>` blocks. This makes the report 4x shorter for the common case (no LLM) and keeps full fidelity when debugging.
- **No empty sections.** Current report has "Phase 3: No units found" etc. even when nothing interesting happened. Drop sections with no signal.
- **Verdict is one of 4 discrete values**, not a free-text tier label. `SUCCESS` / `FAILED_UNREACHABLE` / `FAILED_NO_DATA` / `CARRY_FORWARD`. Downstream consumers can filter reliably.
- **`Changes since last run` is a first-class section.** Right now diffs are buried in the run-level report.json. Surface them per property.

### Named tests — `tests/reporting/test_property_report.py`

| Test | Check |
|---|---|
| `test_report_verdict_success` | SUCCESS scrape with units → report starts with `Verdict: SUCCESS` |
| `test_report_verdict_unreachable` | SSL error → `Verdict: FAILED_UNREACHABLE` |
| `test_report_verdict_no_data` | Scrape succeeded but 0 units → `FAILED_NO_DATA` |
| `test_report_verdict_carry_forward` | Failed scrape + prior state used → `CARRY_FORWARD` |
| `test_report_omits_llm_section_when_no_calls` | Zero LLM calls → no `## LLM calls` section |
| `test_report_collapses_transcripts` | Report with LLM calls wraps prompts in `<details>` |
| `test_report_shows_detection_evidence` | Report's Detection table has `Evidence` column populated |
| `test_report_shows_changes_since_last_run` | Given a diff with +2 new, -1 gone → all shown in `## Changes since last run` |
| `test_report_omits_changes_for_new_property` | First-time property → no `## Changes since last run` section |
| `test_report_shows_fallback_chain_when_adapter_failed` | Adapter→Generic fallback → Pipeline table has both rows |
| `test_report_renders_without_detection_for_legacy_input` | Input dict with no `_detected_pms` key → report still renders (shows "Detection: unavailable") |

### Gate

`scripts/gate_refactor.py phase 7` passes iff:
- All 11 tests pass
- On the latest run directory, regenerating all reports does not error for any property
- Report for property 5317 (the SSL-error case) now shows `Verdict: FAILED_UNREACHABLE` and contains no LLM transcripts

---

## Phase 8 — daily_runner.py integration

**Goal**: Wire the new pipeline into `scripts/daily_runner.py` without breaking the never-fail contract or changing the 46-key output.

### Changes to `scripts/daily_runner.py`

1. Replace `from entrata import scrape` with `from ma_poc.pms.scraper import scrape`. Because Phase 5 kept the `scrape()` signature compatible, nothing else changes at the call site.
2. After scrape, update profile with the new fields from Phase 6. Specifically: if `scrape_result["_detected_pms"]["pms"] != "unknown"` and `scrape_result["_detected_pms"]["confidence"] >= 0.80`, set `profile.api_hints.api_provider` to that PMS value (overwriting any prior value) and set `profile.api_hints.client_account_id` from the detection.
3. Add a new top-level metric to the run-level report: "Properties by detected PMS". Counts by `_detected_pms.pms`. Surfaces coverage at a glance.
4. Add a new top-level metric: "LLM spend by PMS". For each PMS, total LLM cost across all its properties. After this refactor, every PMS except "unknown" should have ~$0. If any does not, that's a bug.
5. The `_expected_units_for` CSV helper (line 646) stays as is.

### Tests to add — `tests/integration/test_daily_runner_refactor.py`

Use a fake scraper via monkeypatch to avoid launching Playwright.

| Test | Check |
|---|---|
| `test_daily_runner_populates_api_provider_on_success` | Fake scrape returns detected_pms=rentcafe/0.95 → after run, profile on disk has `api_hints.api_provider == "rentcafe"` |
| `test_daily_runner_does_not_overwrite_provider_on_low_confidence` | Existing profile has `api_provider="entrata"`; new scrape detects unknown/0.3 → profile keeps "entrata" |
| `test_daily_runner_unreachable_does_not_cost_money` | Fake scrape returns FAILED_UNREACHABLE → run report shows 0 LLM calls for that property |
| `test_daily_runner_46_key_output_preserved` | Run against 1 fake property → `properties.json` record has all 46 keys from `TARGET_PROPERTY_FIELDS` |
| `test_daily_runner_handles_missing_detected_pms_key` | Fake scrape omits `_detected_pms` (simulating mid-migration) → run does not crash, profile update skipped cleanly |
| `test_daily_runner_report_has_pms_breakdown` | Run report.json contains `"properties_by_pms": {...}` and `"llm_cost_by_pms": {...}` |

### Gate

`scripts/gate_refactor.py phase 8` passes iff:
- All 6 integration tests pass
- `scripts/daily_runner.py` can be run end-to-end with `--limit 3` on the current property CSV without raising (smoke test in the gate script)
- Output `properties.json` has schema-identical records to a captured baseline from Phase 0

---

## Phase 9 — Bug hunt + final gate

**Goal**: A systematic pass over the refactored code looking specifically for the bug classes that bit the original implementation. This is the "bug hunt" step from your mandatory 7-step workflow.

### Bug hunt checklist

Work through each item. For each, either confirm the bug is absent or open a fix commit and re-test.

#### Detection & routing
- [ ] `detect_pms` never raises on any input. Fuzz with: `None`, `""`, `"not-a-url"`, `"javascript:alert(1)"`, binary bytes decoded as latin-1
- [ ] `detect_pms` is deterministic — same inputs produce same `DetectedPMS` including same `evidence` ordering
- [ ] `get_adapter` never returns `None` — unknown PMS returns generic
- [ ] Resolver does not navigate more than 5 hops on any input (check with a cycle: A→B→A)
- [ ] Resolver cancels in-flight navigation on exception (no zombie browser tabs)

#### Orchestrator
- [ ] On SSL error, no adapter is called. Grep for any adapter `extract` call in a code path reachable after the error check
- [ ] Legacy return keys enumerated in `tests/pms/snapshots/legacy_scrape_keys.json` are all present on every scrape result
- [ ] `_property_id` is stamped on every result dict, even when scrape fails early
- [ ] Context/browser are closed on every exit path (wrap the whole scrape body in `try/finally` around context.close + browser.close)

#### Adapters
- [ ] No adapter directly mutates `profile` — all profile updates go through `profile_updater` after scrape
- [ ] No adapter has a broader blocklist entry than the URL pattern it matches (e.g., an adapter-specific `_FALSE_POSITIVE_HOSTS` must not include a domain owned by another adapter)
- [ ] Every adapter's `extract` returns an `AdapterResult` — not `None`, not a bare list, not a dict
- [ ] Adapter `confidence` field in `AdapterResult` is 0.0 on empty units, not an arbitrary default

#### Profile
- [ ] Every v1 profile in `config/profiles/` loads as v2 without warnings (run a smoke script: load every file, assert no ValidationError)
- [ ] `stats.total_llm_cost_usd` monotonically increases — never decreases across runs
- [ ] `consecutive_unreachable` and `consecutive_failures` are mutually exclusive — an SSL error increments only `consecutive_unreachable`, a parse failure increments only `consecutive_failures`
- [ ] LRU cap on `explored_links` evicts oldest, not newest (order-preserving test)

#### Report
- [ ] Report markdown renders valid markdown (no broken tables, no unclosed `<details>`) — validate with `mistune` or `markdown-it-py`
- [ ] No report has a section that's present-but-empty (e.g., a `## LLM calls` header with no content)
- [ ] Verdict is always one of the 4 literal values — check with `in {"SUCCESS", "FAILED_UNREACHABLE", "FAILED_NO_DATA", "CARRY_FORWARD"}`

#### Integration
- [ ] `scripts/daily_runner.py --limit 10` produces a report whose "LLM spend by PMS" has `$0` for every PMS except possibly "unknown"
- [ ] A property that was `consecutive_failures: 8` in the old system and has a real PMS signal detectable from its URL now succeeds on the next run (manually pick one — property 5317 likely does not qualify because the SSL error is genuine; pick one where the old failure was downstream of bad routing)
- [ ] The 9 confirmed fingerprints from the handoff document all route to their correct adapter (run `test_orchestrator_detects_then_calls_correct_adapter` variants for all 9)

### Static analysis sweep

Run these in order. All must pass:

```bash
ruff check ma_poc/pms/ ma_poc/reporting/ ma_poc/models/ scripts/
mypy --strict ma_poc/pms/ ma_poc/reporting/
pytest tests/ --cov=ma_poc.pms --cov=ma_poc.reporting --cov-report=term-missing
```

Coverage threshold for the gate: **85% line coverage on `ma_poc/pms/`**, **80% on `ma_poc/reporting/`**. Below threshold = gate fails.

### Observable-behavior checks (run against real data)

Run `scripts/daily_runner.py --limit 20` on a subset of properties. Check the resulting run directory:

1. `report.json` → `"properties_by_pms"` present; no single "unknown" > 20% of total
2. `report.json` → `"llm_cost_by_pms"` present; sum ≤ 10% of the Phase 0 baseline cost
3. Every `properties.json` record has `_meta._detected_pms` present
4. `config/profiles/_audit/` contains one v1 file per migrated profile

### Gate (final)

`scripts/gate_refactor.py phase 9` — the refactor is complete when:
- Every phase gate (0–8) passes individually
- The full bug-hunt checklist above is checked off (script reads a `docs/BUG_HUNT_CHECKLIST.md` file with `[x]` marks and verifies all items are checked)
- Static analysis sweep clean
- Coverage thresholds met
- Observable checks pass on a real 20-property run

---

## Gates — the gate script (`scripts/gate_refactor.py`)

A single script that runs in CI and locally. Invocation:

```bash
python scripts/gate_refactor.py phase <N>        # run one phase gate
python scripts/gate_refactor.py all              # run all phases in order, stop on first fail
python scripts/gate_refactor.py final            # only the cross-cutting checks from phase 9
```

### Gate script structure

- One function per phase: `check_phase_0()`, `check_phase_1()`, ...
- Each function returns `GateResult(phase: int, passed: bool, reasons: list[str])`
- Main writes a table to stdout, writes JSON to `data/gates/<timestamp>.json`
- Non-zero exit code on any failure

### Per-phase checks (summary — full spec in each phase above)

| Phase | What the gate checks |
|---|---|
| 0 | `docs/REFACTOR_BASELINE.md` exists and non-empty; baseline script runs; 4 tests pass |
| 1 | 13 detector tests pass; mypy + ruff clean; every PMS literal has a test |
| 2 | 6 registry tests pass; every PMS literal has an adapter file (stubs OK) |
| 3 | All adapter tests pass; research-log top-comment complete; no cross-adapter imports; no PMS strings in generic.py |
| 4 | 8 resolver tests pass; `_AVAILABILITY_ANCHOR_RE` and `_LEASING_PORTAL_DOMAINS` gone from `scripts/entrata.py` |
| 5 | 11 orchestrator tests pass; `scripts/entrata.py` <100 lines; legacy return keys preserved |
| 6 | 10 migration tests pass; migration script runs; <10% of profiles still have `api_provider=="unknown"` |
| 7 | 11 report tests pass; 5317.md regeneration shows FAILED_UNREACHABLE, no LLM transcripts |
| 8 | 6 integration tests pass; end-to-end `--limit 3` runs clean; 46-key schema preserved |
| 9 | Bug-hunt checklist marked complete; static analysis clean; coverage met; observable checks pass |

### Critical behaviors the gate script enforces

These are easy to violate by accident in a large refactor:

- **No PMS name hard-coded outside its own adapter or the detector.** The gate greps for banned strings across `ma_poc/pms/scraper.py`, `ma_poc/pms/resolver.py`, `ma_poc/pms/adapters/generic.py`. Banned list: `sightmap`, `rentcafe`, `appfolio`, `entrata`, `avaloncommunities`, `onlineleasing`, `realpage`, `yardi`
- **No LLM import in an adapter.** Adapters are deterministic by design. If an adapter needs LLM help, that's the generic adapter's job. Grep check: no `from services.llm_extractor` or `from services.vision_extractor` in `ma_poc/pms/adapters/*.py` except `generic.py`
- **No Playwright import in `detector.py`.** Detection is offline. Grep check
- **No direct `os.path` / `open()` in adapters.** Adapters take inputs, return outputs. Filesystem access is the orchestrator's job. This prevents an adapter from accidentally reading/writing profile files directly
- **`scripts/entrata.py` shim is <100 lines.** Line-count check

---

## Cross-cutting: what to leave alone

Resist the urge to refactor these during this work:

- `ma_poc/templates/` and `ma_poc/extraction/` — the Phase A BRD-spec pipeline. CLAUDE.md notes this is a parallel implementation. Collapsing it into the new adapter pattern is worth doing, but not as part of this refactor. Flag it as a follow-up
- `state_store.py` — its not-concurrent limitation is real but orthogonal. Don't touch
- `concurrency.py` — auto-sizing works; new adapters inherit it for free
- `identity.py` — the 5-tier cascade is orthogonal to PMS detection. Don't conflate
- `scrape_properties.py::transform_units_from_scrape` — the 46-key schema projection. Adapters produce the same unit shape as before, so this doesn't need changes

---

## Cross-cutting: what to watch for while researching PMS structures

You'll be searching the web and inspecting real captured payloads. Heads-up on bear traps:

1. **Documentation drift.** PMS docs are often months behind actual API behavior. Trust captured payloads over docs when they conflict
2. **Anonymization in public write-ups.** Some blog posts reverse-engineering these APIs redact property IDs. Don't copy redacted URLs into tests; use real ones from `raw_api/`
3. **Widget vs. direct API.** Entrata has both. The widget wrapper shape is already handled at `scripts/entrata.py:314`. Port that logic; don't re-derive it
4. **Null vs. empty.** OneSite's `/units` can be `null`, `[]`, or `{response: null}`. Three different shapes for "no availability." Test all three
5. **Rate limiting / bot detection.** When researching, don't hammer a PMS's API. Use captures from `raw_api/`. If you must fetch live, space requests ≥2s apart and use the existing proxy config
6. **Do not fetch a captured endpoint with real tenant data and store the response.** If a payload in `raw_api/` has PII (names, emails, phone numbers), redact it before checking into `tests/pms/adapters/fixtures/`. Use a helper to redact emails/phones regex-style

---

## Execution notes for Claude Code

- Read requirements fully before writing code (per your CLAUDE.md workflow step 1)
- Write tests immediately after each file (step 3 — not at the end)
- Run tests after each phase (step 4)
- Static analysis at each phase gate (step 5)
- Gate validation before moving on (step 6)
- Bug hunt at Phase 9 (step 7)
- If a phase gate fails, **do not paper over it by loosening the gate** — fix the code. If the gate is genuinely wrong, flag it back to the human before changing it
- If a research step turns up that a PMS's response structure is substantially different from what any adapter-pattern assumes (e.g. a PMS that exposes data only via GraphQL subscriptions), **stop and flag to the human** rather than inventing a workaround
- Commit at phase boundaries. Never commit across phases. One phase = one commit (or one PR), with the gate output in the commit message

## Out of scope for this refactor

Call these out in PR description so reviewers don't expect them:
- Tier-6 syndication fallback (handoff Gap #4) — separate project
- REIT custom stack family beyond AvalonBay (Gap #3 partial) — AvalonBay alone is in scope; Equity/UDR/Essex/Camden/Mid-America are not
- Profile cross-property clustering (Gap #5) — `client_account_id` is captured but no cross-property learning uses it yet
- `LEASE_UP_VOLATILE` outcome (Gap #6)
- Vintage-template test cohort (Gap #7)
- Non-garden unit-ID validator (Gap #8)
- `parent_property_id` for multi-phase (Gap #9)
- Collapsing `ma_poc/templates/` and `ma_poc/extraction/` into adapters

---

*End of refactor instructions.*