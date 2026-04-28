# PropAi × Jugnu — Integration Architecture v1

**Author:** ashuchan
**Date:** 2026-04-25 (rev 2026-04-26 — all jugnu-side gaps shipped)
**Scope:** Replace PropAi's in-tree scraper (`ma_poc/scripts/jugnu_runner.py` + `entrata.py` + `extraction/` + `templates/` + `pms/adapters/`) with a thin shell around the standalone Jugnu library, while preserving PropAi's V2 output contract, identity resolution, state diff, carry-forward, and reporting.

> **Status (rev 2026-04-26):** all five jugnu library changes proposed in §4 have landed. PropAi-side scaffolding (`ma_poc/jugnu_poc/`) is the next step.

---

## 1. Repo layout — parallel POC, not in-place replacement

`ma_poc/scripts/jugnu_runner.py` is 1,454 LOC of orchestration that wires CSV loading, identity, state diff, V2 output, and reports. Don't rip it out yet — add a parallel POC alongside it that proves parity, then switch over.

```
ma_poc/
├── jugnu_poc/                                    # NEW — thin shell around jugnu lib
│   ├── __init__.py
│   ├── skill.py                                  # PROPAI_SKILL: Skill object (real-estate)
│   ├── runner.py                                 # orchestrator
│   ├── property_transform.py                     # Blink.records[0] → 46-key V2 property record
│   ├── memory_store.py                           # persist/load SkillMemory between runs
│   ├── profile_store_adapter.py                  # jugnu ScrapeProfile ↔ per-canonical_id store
│   └── README.md
└── scripts/
    └── jugnu_poc_runner.py                       # CLI entrypoint mirroring jugnu_runner.py UX
```

Add `jugnu` as a path dependency in `requirements.txt`:
```
jugnu @ file:///c:/Users/ashus/OneDrive/Documents/Code/Jugnu
```
Pin to `git+https://github.com/<org>/jugnu@v0.1.0` once Jugnu publishes.

---

## 2. The Skill — single nested property record, no parent/child split inside Jugnu

**Design decision:** Jugnu's `OutputSchema` describes ONE record shape — a property with a nested `units` array. Each `Blink` returns at most one record per URL (`Blink.records[0]`). PropAi's V2 transform happens in `property_transform.py` after Jugnu returns, never inside Jugnu.

This keeps Jugnu's contract simple — `Blink.records: list[dict]` matching `OutputSchema` — and avoids introducing a `parent_record` concept that would force every other Jugnu use-case (finance, sports, news) to think about container hierarchies they don't have.

### `jugnu_poc/skill.py`

```python
PROPAI_SKILL = Skill(
    name="propai_multifamily",
    version="2.0.0",
    description=(
        "Extract one multifamily apartment property per URL. "
        "Each property has a top-level metadata block AND a nested units array, "
        "one entry per individually-listed apartment with rent and availability."
    ),
    output_schema=OutputSchema(
        # Property + nested units, returned as ONE record per URL
        fields=[
            # Property-level
            "proj_name", "address", "city", "state", "zip_code", "country",
            "phone", "email_address", "website", "pmc", "website_design",
            "concessions",
            # Nested array
            "units",                         # list[dict] — see units schema below
        ],
        primary_key="website",               # URL is the natural key per crawl
        merging_keys=["address", "website"],
        minimum_fields=["proj_name", "units"],
        json_schema={
            "type": "object",
            "properties": {
                "proj_name":       {"type": ["string", "null"]},
                "address":         {"type": ["string", "null"]},
                "city":            {"type": ["string", "null"]},
                "state":           {"type": ["string", "null"]},
                "zip_code":        {"type": ["string", "null"], "pattern": "^\\d{5}$"},
                "country":         {"type": ["string", "null"]},
                "phone":           {"type": ["string", "null"]},
                "email_address":   {"type": ["string", "null"]},
                "website":         {"type": ["string", "null"]},
                "pmc":             {"type": ["string", "null"]},
                "website_design":  {"type": ["string", "null"]},
                "concessions":     {"type": ["string", "null"]},
                "units": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "unit_id":         {"type": ["string", "null"]},
                            "floor_plan_name": {"type": ["string", "null"]},
                            "beds":            {"type": ["integer","null"], "minimum": 0, "maximum": 7},
                            "baths":           {"type": ["number", "null"], "minimum": 0, "maximum": 10},
                            "area":            {"type": ["integer","null"], "minimum": 150, "maximum": 10000},
                            "rent_low":        {"type": ["number", "null"], "exclusiveMinimum": 1},
                            "rent_high":       {"type": ["number", "null"], "exclusiveMinimum": 1},
                            "available_date":  {"type": ["string", "null"], "pattern": "^\\d{4}-\\d{2}-\\d{2}$"},
                            "lease_term":      {"type": ["integer","null"], "exclusiveMinimum": 1},
                            "move_in_date":    {"type": ["string", "null"], "pattern": "^\\d{4}-\\d{2}-\\d{2}$"},
                            "concessions":     {"type": ["string", "null"]},
                            "amenities":       {"type": "array", "items": {"type": "string"}},
                        },
                    },
                },
            },
        },
    ),
    source_hints=[
        SourceHint(platform="rentcafe",
                   api_patterns=["/api/getApartments","default.aspx","/GetApartmentsByFilter"],
                   link_keywords=["floor-plans","availability","apartments"]),
        SourceHint(platform="entrata",
                   api_patterns=["/api/v1/properties","/module/widgets/","/api/v1/floorplans","/api/v1/propertyunits"],
                   link_keywords=["floor-plans","availability"]),
        SourceHint(platform="appfolio",
                   api_patterns=["/api/available_rentals"],
                   link_keywords=["listings"]),
        SourceHint(platform="sightmap",
                   api_patterns=["sightmap.com/app/api"]),
        SourceHint(platform="realpage",
                   api_patterns=["api.ws.realpage.com/floorplans","api.ws.realpage.com/units"]),
        SourceHint(platform="onesite",
                   api_patterns=["/onesite/api","/availabilityandpricing"]),
        SourceHint(platform="avalonbay",
                   api_patterns=["/api/communities","/availableapartments"]),
    ],
    custom_instructions=(
        # Field semantics
        "monthlyRent and rentAmount are PRE-concession asking rent. "
        "effectiveRent INCLUDES concessions — prefer monthlyRent/rentAmount for rent_low/rent_high. "
        "If rent appears as a range string ($1,200 - $1,500), parse low and high separately. "
        "If only one rent is present, set both rent_low and rent_high to it. "
        # Unit identity
        "unit_id is the building-unit number tenants see (e.g. '101', 'A204', '1004'). "
        "It is NOT the floor plan name. If only floor plan rows are listed (no per-unit detail), "
        "emit one record per floor plan and leave unit_id null. "
        # Bedroom/bathroom normalization
        "Studio = 0 bedrooms. Beds must be integer 0-7. Baths must be multiple of 0.5, 0-10. "
        # Junk filtering
        "REJECT any unit whose floor_plan_name starts with MODULE_, contains 'Lease Magnet', "
        "'Pop-Up', or vendor prefixes like '[Riedman]'. These are CMS module names, not units. "
        "REJECT unit_id values that are stop-words ('Left', 's', 'View', 'Apply'). "
        # Sanity bounds
        "REJECT rent < $200 or > $50,000/month — these are misidentified fields. "
        "REJECT area outside 150-10000 sqft — these are bedroom counts or floor numbers leaking. "
        # Availability defaults
        "If a unit has no explicit availability date but is listed on the site, "
        "treat it as AVAILABLE today. "
        # Property metadata
        "proj_name comes from og:site_name, og:title, JSON-LD ApartmentComplex.name, or <title>. "
        "Address fields come from JSON-LD PostalAddress. Phone from JSON-LD telephone or footer regex. "
        # Concessions
        "concessions is a free-text banner like '6 weeks free!' or '$500 off first month'. "
        "Look for promotional banners, hero images, and announcement bars."
    ),
    negative_keywords=[
        "MODULE_", "Lease Magnet", "Pop-Up", "[Riedman]",
        "EliseAI", "Sierra", "ConversionCloud", "UserWay",
        "/tag-manager/", "/analytics/", "/beacon", "/gen_204",
    ],
    jugnu_settings=JugnuSettings(
        link_confidence_threshold=0.4,
        max_external_depth=0,                     # PropAi never follows external
        max_llm_calls_per_url=4,                  # 3 API + 1 DOM (matches PropAi budget)
        max_concurrent_crawls=0,                  # auto-detect
        carry_forward_on_failure=True,
        memory_consolidation_batch_size=50,
        memory_consolidation_smart_trigger_count=3,
    ),
)
```

PropAi's `_format_v2` already knows how to flatten this into the 46-key schema with CSV-priority for address/city/state/zip/name. That logic stays in `property_transform.py` — Jugnu doesn't need to know.

---

## 3. Runner control flow

`jugnu_poc/runner.py` keeps PropAi's outer loop and replaces only the per-property scrape:

```
load CSV (config/properties.csv)
   |
identity.resolve_identity(row)                    # KEEP (PropAi script)
identity.detect_duplicates(...)                   # KEEP
   |
load PROPAI_SKILL + memory_store.load(skill.name) # NEW
   |
jugnu = Jugnu(PROPAI_SKILL, skill_memory=cached, profile_store=profile_store_adapter)
await jugnu.warm_up()                             # one-shot Prompt-5
   |
inputs = { row.url: CrawlInput(
              url=row.url,
              carry_forward_records=state_store.last_property_record(canonical_id),
              metadata={                          # threaded into ALL prompts
                  "canonical_id":   ...,
                  "property_name":  csv.name,
                  "address":        csv.address,
                  "city":           csv.city,
                  "state":          csv.state,
                  "zip_code":       csv.zip,
                  "pmc":            csv.management_company,
                  "expected_total_units": csv.total_units_est,
                  "schema_version": "v2",
              }
          ) for row in csv }
   |
results: dict[url, Blink] = await jugnu.crawl(inputs)
   |
for url, blink in results:
    canonical_id = inputs[url].metadata["canonical_id"]
    property_record = blink.records[0] if blink.records else None     # nested property+units
    units_v2     = property_transform.normalize_units(property_record["units"])
    property_v2  = property_transform.build_v2_property(csv_row, blink, property_record, units_v2)
    state_store.upsert_units(canonical_id, units_v2)                  # KEEP
    diff         = state_store.compute_diff(canonical_id)             # KEEP
    if blink.status == CARRY_FORWARD:
        units_v2 = state_store.carry_forward_units(canonical_id)      # KEEP
    write properties.json + report.{json,md} + issues.jsonl           # KEEP
   |
memory_store.save(skill.name, blink.llm_profile)  # SkillMemory survives the run
```

**Kept from PropAi** (domain-specific, stays outside the library): identity resolution, state store + daily diff + disappeared, carry-forward unit snapshotting, V2 normalization (`schema_v2.py`), validation issue codes, run report assembly.

**Replaced by Jugnu**: fetch (Ember), proxy + rate limiter, extraction cascade (Glow tiers), LLM extraction (Spark), per-URL profile, conditional change detection, DLQ, **and the actual win** — self-learning via SkillMemory + Prompt-6 consolidation across all 500 properties in one batch.

---

## 4. Gap analysis — push everything possible into prompt enrichment

Per direction: no `parent_record` concept inside Jugnu. The schema IS a property with nested units. For gaps 4–13, prefer prompt enrichment over library schema changes wherever possible.

### Closed by the nested-schema decision (no library change)

| # | PropAi need | How it's handled |
|---|---|---|
| 1 | Per-unit records | Returned as `record["units"]` array inside the single property record. PropAi's `property_transform.normalize_units()` flattens to V2. |
| 2 | Property-level container (proj_name, address, phone, pmc, website_design, concessions banner) | Top-level fields on the same record alongside `units`. No special Jugnu concept. |
| 3 | CSV-priority over scraped values | Caller-side merge in `property_transform.build_v2_property()`. Skill prompts also tell the LLM that CSV-provided values are authoritative — see (4). |
| 8 | Per-record carry-forward with `carryforward_days` counter | Caller-side in `state_store.upsert_units` / `carry_forward_units`. Jugnu's Blink-level carry-forward (one bool flag) is enough — PropAi's per-unit diff is domain-specific. |
| 11 | Cost accounting per canonical_id | Caller groups by `inputs[url].metadata["canonical_id"]`. Jugnu's `cost_ledger.py` already keys per-URL per-stage. |
| 12 | 28 PropAi event types | Both event ledgers coexist — PropAi keeps emitting fetch/scheduling events from outside, Jugnu emits LLM/cost events. |
| 13 | Schema versioning (V1 vs V2) | Caller-side switch in `property_transform`. Jugnu doesn't see schema versions. |

### Solvable via prompt enrichment

These are gaps the library would normally close with new fields, but where threading more context into the existing prompts achieves the same outcome at zero cost to Jugnu's surface area. **All entries shipped 2026-04-26.**

| # | PropAi need | What landed in jugnu | Status |
|---|---|---|---|
| 4 | **Per-property context in LLM prompts** (city, state, pmc, property_name, expected_total_units) | New `jugnu/spark/input_metadata.py` formatter. `CrawlInput.metadata` is read by `crawler._crawl_url` and threaded into Prompt-1/2/3/4 as `{input_metadata}`. Templates updated. Coverage in `tests/test_spark/test_prompt_enrichment.py`. | ✅ shipped |
| 6 | **Blocked-endpoint learning** ("this API is noise, don't reanalyze") | New `ApiHints.blocked_endpoints` list. Prompt-1's `improvement_signal.misleading_patterns` are promoted into the per-URL profile by `crawler._record_blocked_endpoints`. Discovery prompt receives them as `{url_specific_noise}`. `KnownApiAdapter` gains a `blocked_endpoints` short-circuit and `GenericAdapter` plumbs the per-URL list through. Coverage in `tests/test_glow/test_replay_and_filters.py`. | ✅ shipped |
| 9 | **Junk filters** (`MODULE_*`, "Lease Magnet", `[Riedman]`, stop-word unit numbers) | `Skill.negative_keywords` rendered into all six prompt templates. New `filter_records_by_negative_keywords` in `glow/schema_normalizer.py` runs as a final safety net after extraction AND after merge in `crawler._crawl_url`. `WarmupOrchestrator` and `MemoryConsolidator` also re-add caller-supplied keywords as a noise floor so an LLM can't silently drop them. | ✅ shipped |
| 10 | **Per-field validators** (beds [0,7], baths step 0.5, area [150,10000], rent>1, zip 5 digits) | `passes_schema_gate` now also runs `jsonschema` (Draft 2020-12) per-record against `OutputSchema.json_schema`. Soft-imports `jsonschema` so the gate degrades to the prior minimum_fields check if the dep is missing. `jsonschema>=4.0.0` added to `pyproject.toml`. | ✅ shipped |
| 5 | **Deterministic LLM-mapping replay** (`json_paths`, `response_envelope`, `success_count`) — saves dollars per run after warm-up | `LlmFieldMapping` extended with `api_url_pattern`, `json_paths`, `response_envelope`, `dom_selector`, `success_count`. `crystallizer` now writes per-mapping payloads (no longer collapses everything into `extraction_hint`). `ProfileReplayAdapter` gains an active `_replay_api_active` step that re-issues the stored `api_url_pattern` via httpx, walks `response_envelope`, and projects each record through the per-field `json_paths`. `success_count`/`confidence` increment on every Tier-1a hit. Coverage in `test_replay_and_filters.py`. | ✅ shipped |
| 7 | **Multi-page CTA-hop + iframe-resolve + leasing-portal redirect** (50%+ of PropAi success comes from here) | DiscoveryLLM (Prompt-1) is now wired into `_crawl_url` between fetch and Prompt-2 — `Lantern.discover` runs once and feeds Prompt-1 with split api/link candidates. Discovery's `navigation_hint`, `ranked_apis`, `ranked_links`, `platform_guess` are exposed on `Blink.llm_interactions["discovery"]`. **Multi-page BFS itself stays runner-side via the navigation_hint loop in PropAi's `jugnu_poc/runner.py` (M3) — Jugnu still issues one fetch per `Jugnu.crawl()` call.** | ✅ Prompt-1 wired; runner-side hop loop is M3 |

### Net library changes shipped in jugnu

All five proposed changes landed. Code locations for traceability:

1. ✅ Thread `CrawlInput.metadata` into every Spark prompt — gap 4
   `jugnu/spark/input_metadata.py`, `crawler.py`, all four Spark `*_llm.py` modules, all four prompt templates
2. ✅ Consume `Skill.negative_keywords` in Spark prompts + `schema_normalizer` — gap 9
   All six prompt templates, `crawler.py` (post-extract + post-merge filter), `glow/schema_normalizer.py`, `spark/warmup.py`, `spark/consolidator.py`
3. ✅ Run `OutputSchema.json_schema` in `validation/schema_gate.py` — gap 10
   Per-record validation; `jsonschema>=4.0.0` added to `pyproject.toml`
4. ✅ `ApiHints.blocked_endpoints` + `{url_specific_noise}` in Prompt-1 + `KnownApiAdapter` short-circuit — gap 6
   `profile.py`, `crawler._record_blocked_endpoints`, `glow/tiers/tier1_api.py`, `glow/generic_adapter.py`, `discovery.txt`
5. ✅ `LlmFieldMapping` extension + active API replay in `tier1_profile` — gap 5
   `profile.py`, `spark/crystallizer.py`, `glow/tiers/tier1_profile.py`

**Side fixes that landed with the gap work:**
- DiscoveryLLM (Prompt-1) was previously dead code — now invoked between fetch and Prompt-2 whenever deterministic extraction came back empty. `navigation_hint` lands on `Blink.llm_interactions["discovery"]` so the runner-side hop loop has signal to act on.
- `Blink.llm_interactions` now reliably carries a structured `discovery` block (with `ranked_apis`, `ranked_links`, `navigation_hint`, `platform_guess`) plus `external_candidates` and `merge_decisions` when those tiers fire.

**Test coverage delta:** 109 → 129 tests (10 new replay/filter tests, 10 new prompt-enrichment tests). All green.

The previously-proposed `Blink.parent_record` is **dropped** per direction.
The previously-proposed multi-page BFS in `GlowResolver` is **deferred** — handled in the runner via the `navigation_hint` loop until Jugnu's resolver is wired.

---

## 5. Prompt enrichment — what each prompt now receives

All enrichments below shipped 2026-04-26. Variable names are the literal `{placeholder}` keys in the templates under `jugnu/spark/prompts/`.

### Prompt-1 (Discovery) — `discovery.txt` ✅ wired into crawler
- `{input_metadata}` — caller-supplied per-URL JSON (property name, city, state, pmc, expected_total_units)
- `{url_specific_noise}` — per-URL `ScrapeProfile.api_hints.blocked_endpoints` (learned from prior runs' `improvement_signal.misleading_patterns`)
- `{known_noise_patterns}` — skill-wide noise from SkillMemory
- `{prompt1_context}` — dense paragraph from SkillMemory.prompt1_context
- `{negative_keywords}` — `Skill.negative_keywords` (downrank links/APIs matching these substrings)
- `{api_candidates_json}` / `{link_candidates_json}` — populated from `Lantern.discover()` (api_endpoints + ranked_links) so Prompt-1 has real candidates to score

### Prompt-2 (Extraction) — `extraction.txt`
- `{input_metadata}` — anchors proj_name and address fields against caller-known truth
- `{output_schema_json}` — full `OutputSchema.model_dump()` including the rich `json_schema` constraints (min/max/pattern) so the LLM self-corrects
- `{negative_keywords}` — explicit REJECT directive
- `{custom_instructions}` — full `Skill.custom_instructions` text
- `{confirmed_field_synonyms_json}` / `{field_extraction_hints_json}` — from SkillMemory
- `{prompt2_context}` — dense paragraph from SkillMemory.prompt2_context

### Prompt-3 (Merge) — `merge.txt`
- `{input_metadata}` — disambiguates "same property as last run vs different building"
- `{negative_keywords}` — REJECT during merge
- `{primary_key}`, `{merging_keys}`, `{confirmed_field_synonyms_json}`, `{field_extraction_hints_json}` — already in template

### Prompt-4 (External) — `external_rank.txt`
- `{input_metadata}` — context for "is this external link plausibly about *this* property"
- `{negative_keywords}` — never recommend a link matching these
- `{known_noise_patterns}`, `{prompt4_context}` — already in template

### Prompt-5 (Warmup) — `warmup.txt`
- No per-URL context (pre-URL by design)
- `{negative_keywords}` — seeds the skill's noise floor; `WarmupOrchestrator` re-adds them post-LLM so a forgetful model can't drop them

### Prompt-6 (Consolidation) — `consolidation.txt`
- No per-URL context (batch summarizer)
- `{negative_keywords}` — kept in `known_noise_patterns` as a floor; `MemoryConsolidator` re-adds them after applying the LLM's response, same defensive pattern as Prompt-5

---

## 6. Suggested milestones

- **M1 — scaffold `ma_poc/jugnu_poc/` and run hello-world** (1-2 days) — *next*
  Add Jugnu as path dep; create `jugnu_poc/{skill,runner,property_transform,memory_store,profile_store_adapter}.py`; define `PROPAI_SKILL`; run `jugnu_poc_runner.py --limit 1` against one known-good RentCafe property; verify `Blink.records[0]` contains property + nested units; verify `Blink.llm_interactions["discovery"]` exposes `navigation_hint` when extraction comes back empty.

- **M2 — V2 normalization + parity comparison** (3-5 days)
  Port V2 normalization into `property_transform.build_v2_property()`; rerun `--limit 5` and compare output to `jugnu_runner.py --limit 5` byte-for-byte (or document the diffs). Confirm the negative-keyword filter and json_schema gate actually drop the junk PropAi was previously dropping at write time.

- **M3 — multi-page navigation via navigation_hint loop** (2-3 days)
  Implement the runner-side hop: when `Blink.tier_used == "llm_extraction"` (or earlier tiers) returns empty AND `Blink.llm_interactions[0]["discovery"]["navigation_hint"]` is non-empty, issue a second `Jugnu.crawl()` for the hinted URL with the parent's metadata. The discovery payload also includes `ranked_apis` and `ranked_links` — runner can hop on the highest-confidence link/API instead of (or in addition to) the free-text hint. This gives us multi-page support without waiting for Jugnu's `GlowResolver` BFS work.

- **M4 — soak test on full 500** (1 week)
  Run both pipelines on the full CSV. Compare success rate, LLM cost, unit counts. Watch the new `LlmFieldMapping.success_count` field — by run 3 it should be incrementing on the same per-URL mappings, proving Tier-1a replay is the bypass path for repeat crawls. Switch over only when Jugnu is at parity or better.

- **M5 — internal multi-page BFS in jugnu** (deferred — runner-side hop is enough until we measure cost)
  Optional follow-up: move the navigation_hint loop from PropAi's runner into jugnu's `GlowResolver` itself, capped by a new `JugnuSettings.max_internal_pages`. Only worth doing if multiple skills end up needing the same loop.

- **M6 — eventual switch-off**
  Once Jugnu is at parity, delete `ma_poc/scripts/jugnu_runner.py`, `entrata.py`, `extraction/`, `templates/`, `pms/adapters/`. Keep `identity.py`, `state_store.py`, `schema_v2.py`, `validation.py`, `scripts/daily_runner.py` only if anything still depends on them — otherwise also retire.

---

## 7. Open questions for the user

1. **State directory layout during the POC** — share `data/state/` with the existing `jugnu_runner.py` (so they're directly comparable run-over-run) or use `data/jugnu_poc/state/` so they can run in parallel without colliding?
2. **Jugnu dependency form** — local path install (`file:///c:/Users/.../Jugnu`) for the POC, or do you want to push Jugnu to a private git remote first and pin to a tag?
3. **Vision (banner capture, accuracy sample, Tier-5 fallback)** — `Skill.vision_settings` and `Skill.screenshot_settings` exist on the schema but **no jugnu code path consumes them today**. `LLMProvider.from_settings(skill.llm_settings)` only constructs the text provider. Defer vision entirely for M1‑M4 (text-only pipeline). When PropAi needs PR-04 parity, we'll either (a) wire a `VisionLLMProvider` into a new Tier‑5 adapter inside jugnu, or (b) keep vision PropAi-side and call jugnu only for text extraction — TBD when we have soak data on text-only success rate.

---

## 8. LLM provider configuration

PropAi today drives Anthropic and OpenRouter via dedicated provider classes (`ma_poc/llm/anthropic.py`, `ma_poc/llm/openrouter.py`). Jugnu wraps `litellm.acompletion()` once at `Jugnu.__init__` time via `LLMProvider.from_settings(skill.llm_settings)`. The two reconcile cleanly because litellm dispatches on the model-prefix in the `model` string.

### Provider selection — via `Skill.llm_settings.model`

| Provider | `model` string (litellm format) | Notes |
|---|---|---|
| Anthropic | `claude-haiku-4-5-20251001`, `claude-sonnet-4-6`, `claude-opus-4-7` | Native Anthropic API. No prefix required. |
| OpenRouter | `openrouter/google/gemini-2.5-flash`, `openrouter/anthropic/claude-haiku-4-5` | `openrouter/` prefix triggers OpenRouter dispatch. |
| Azure OpenAI | `azure/<deployment-name>` | Needs `AZURE_API_KEY` + `AZURE_API_BASE` env vars. |
| Local Ollama | `ollama/llama3.1:70b` | Needs `OLLAMA_API_BASE`. |

`PROPAI_SKILL` ships with Anthropic Haiku 4.5 as default. To switch a single run to OpenRouter without editing the skill, override via env at runner startup (in `jugnu_poc/skill.py` module scope):

```python
def _resolve_models() -> tuple[str, str]:
    provider = os.getenv("LLM_PROVIDER", "anthropic").lower()
    if provider == "openrouter":
        return (
            os.getenv("OPENROUTER_MODEL", "openrouter/google/gemini-2.5-flash"),
            os.getenv("OPENROUTER_VISION_MODEL", "openrouter/google/gemini-2.5-flash"),
        )
    if provider == "azure":
        return (
            f"azure/{os.getenv('AZURE_OPENAI_DEPLOYMENT_GPT4O_MINI', 'gpt-4o-mini')}",
            f"azure/{os.getenv('AZURE_OPENAI_DEPLOYMENT_GPT4O_VISION', 'gpt-4o')}",
        )
    return (
        os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001"),
        os.getenv("ANTHROPIC_VISION_MODEL", "claude-haiku-4-5-20251001"),
    )
```

### Env-key bridging for PropAi infra-style names (Option B)

litellm only auto-discovers canonical names: `OPENROUTER_API_KEY`, `ANTHROPIC_API_KEY`, `AZURE_API_KEY`, `AZURE_API_BASE`. PropAi infra deploys hyphenated names like `OPENROUTER-api-key-production`. PropAi's existing in-tree code already uses canonical names (`OPENROUTER_API_KEY`, `ANTHROPIC_API_KEY`), so if your local dev env exports those, nothing to do. For deployed environments where only the hyphenated name is set, add a one-shot bridge at `jugnu_poc/skill.py` module load:

```python
def _bridge_keys() -> None:
    aliases = {
        "OPENROUTER_API_KEY":  ["OPENROUTER_API_KEY_PRODUCTION", "OPENROUTER-api-key-production"],
        "ANTHROPIC_API_KEY":   ["ANTHROPIC_API_KEY_PRODUCTION",  "ANTHROPIC-api-key-production"],
        "AZURE_API_KEY":       ["AZURE_OPENAI_API_KEY"],
        "AZURE_API_BASE":      ["AZURE_OPENAI_ENDPOINT"],
    }
    for canonical, candidates in aliases.items():
        if os.getenv(canonical):
            continue
        for alt in candidates:
            val = os.getenv(alt)
            if val:
                os.environ[canonical] = val
                break

_bridge_keys()  # at import time, before Skill is constructed
```

This stays caller-side — no jugnu library change. Option A (rename at deploy-time injection) and Option C (pass `api_key` via `LiteLLMSettings.extra_params`) are also valid; Option B is preferred because it's the smallest behavioural surface and survives env-var renames in either direction.

### Cost accounting

Per the accepted decision, we leverage litellm's `completion_cost(completion_response=...)` even when it returns `0.0` (unknown OpenRouter pricing, off-catalog Azure deployments). `LLMProvider.complete()` already records this via `CostLedger.record(stage=..., cost_usd=...)`, keyed per-URL per-stage. PropAi-side `runner.py` aggregates by `inputs[url].metadata["canonical_id"]` for per-property cost reporting. No additional jugnu-side work needed; the inaccuracy is acceptable given the alternative (per-provider price tables maintained inside jugnu) is high-effort low-value.

### Token management — chunking is jugnu's responsibility

`jugnu/spark/content_cleaner.py:html_to_fit_markdown` is the contract: Spark prompts never see raw HTML, only fit-markdown that the cleaner sized to fit a target token budget. PropAi's `extraction/tier4_llm.py` 60K truncation is **obsolete** in the new pipeline — delete it during the M6 switch-off. Per the accepted decision, no jugnu changes for chunking.

### Image size limits — applies only when vision is wired

Anthropic image API limit: 5 MB base64. OpenRouter vision: 20 MB. Today neither matters (vision deferred per §7). If/when we wire jugnu vision in M5+, the caller (or a new `VisionLLMProvider` inside jugnu) must downsample/crop before the API call — same logic PropAi already has in `ma_poc/llm/images.py:check_size`.

---

## 9. Proxy configuration

PropAi's existing pipeline depends on Bright Data residential proxies (per `.env.example`: `PROXY_PROVIDER=brightdata`, `PROXY_HOST/PORT/USERNAME/PASSWORD`). Jugnu now ships a matching abstraction so the same env vars (or a `Skill.proxy_settings` block) can drive proxy behaviour without any caller-side glue.

### Architecture

`jugnu/ember/proxy.py` defines a `ProxyConfig` value object plus a `ProxyProvider` ABC. Three implementations cover the common cases, and any custom selection / sessioning strategy can be plugged in by subclassing `ProxyProvider`:

| Provider | When to use |
|---|---|
| `StaticProxyProvider(config)` | One proxy used for every fetch. The classic single-proxy case. |
| `RotatingProxyProvider([cfg, ...])` | Health-scored pool. Picks the healthiest non-quarantined proxy per fetch; `severity="hard"` failures (403/captcha/auth) penalise faster than soft failures (timeouts, 5xx). Wraps the existing `ProxyPool`. |
| `BrightDataProvider(customer_id, zone, password, ...)` | Bright Data residential / datacenter. Assembles their `brd-customer-X-zone-Y[-country-Z][-session-S]` username pattern. Optional `country` filter. With `sticky_session_per_url=True` (default) the session ID is `sha256(url)[:16]` so multi-page crawls of the same property reuse the same exit IP, but two different properties land on different IPs. |

Ember calls `provider.select(url)` per fetch (Playwright via `new_context(proxy=...)` for per-context rotation, httpx via `AsyncClient(proxy=...)` with auth baked into the URL via `to_httpx_url()`), and reports outcomes back via `report_success` / `report_failure`. Failure severity is classified by `classify_failure_severity(status_code, error)` — 403/407/captcha → hard, everything else → soft.

### How a caller wires it

**Option A — declarative via Skill** (simplest; matches the Skill pattern for everything else):

```python
from jugnu.skill import ProxySettings, Skill

PROPAI_SKILL = Skill(
    name="propai_multifamily",
    ...,
    proxy_settings=ProxySettings(
        brightdata_customer_id="hl_xxxxxxx",
        brightdata_zone="residential1",
        brightdata_password="...",
        brightdata_country="us",
        brightdata_sticky_per_url=True,   # default
    ),
)

jugnu = Jugnu(PROPAI_SKILL, ...)         # provider built from settings automatically
```

`ProxySettings` selection precedence: `enabled=False` → no proxy; `brightdata_*` populated → BrightData; `rotating_servers` populated → RotatingProxyProvider; `server` populated → StaticProxyProvider; otherwise no proxy.

**Option B — explicit provider injection** (use when you want a custom selection strategy or want to share one provider across multiple Skills/Jugnus):

```python
from jugnu.ember.proxy import BrightDataProvider, build_proxy_provider_from_env

# Option B1: PropAi already configures Bright Data via env vars matching its
# .env.example — pick them up directly, no Skill changes needed.
provider = build_proxy_provider_from_env()

# Option B2: explicit construction
provider = BrightDataProvider(
    customer_id=os.environ["PROXY_BRIGHTDATA_CUSTOMER_ID"],
    zone=os.environ["PROXY_BRIGHTDATA_ZONE"],
    password=os.environ["PROXY_PASSWORD"],
    country="us",
)

jugnu = Jugnu(PROPAI_SKILL, proxy_provider=provider)   # explicit kwarg wins over Skill.proxy_settings
```

### Environment variables recognised by `build_proxy_provider_from_env`

| Var | Purpose |
|---|---|
| `PROXY_PROVIDER` | `brightdata` \| `rotating` \| `static` (default) — selects the dispatch arm |
| `PROXY_SERVER` | Static-mode server URL (e.g. `http://prox.example:8080`) |
| `PROXY_USERNAME` / `PROXY_PASSWORD` | Static-mode credentials (also used as Bright Data password fallback) |
| `PROXY_POOL_URLS` | Comma-separated list for rotating mode; entries may include `user:pass@host:port` |
| `PROXY_BRIGHTDATA_CUSTOMER_ID` | Bright Data customer ID (or fall back to `PROXY_USERNAME`) |
| `PROXY_BRIGHTDATA_ZONE` | Bright Data zone name (residential / datacenter / etc.) |
| `PROXY_BRIGHTDATA_COUNTRY` | Optional country filter (e.g. `us`) |
| `PROXY_BRIGHTDATA_STICKY_PER_URL` | `0` to disable URL-stickiness (per-request rotation) |
| `PROXY_HOST` / `PROXY_PORT` | Override Bright Data endpoint (defaults `brd.superproxy.io:22225`) |

PropAi's existing `.env.example` already defines `PROXY_PROVIDER=brightdata`, `PROXY_HOST`, `PROXY_PORT`, `PROXY_USERNAME`, `PROXY_PASSWORD`. To activate Bright Data through jugnu without changing PropAi infra, set additionally `PROXY_BRIGHTDATA_CUSTOMER_ID` (or rely on it falling back to `PROXY_USERNAME`) and `PROXY_BRIGHTDATA_ZONE`.

### GCP Secret Manager — secrets to create

Create one secret per credential in the project's Secret Manager and mount each as the matching env var on the worker (Cloud Run job / GKE pod / Cloud Build step) that runs the daily scrape. Existing PropAi infra-style hyphenated names are listed where they apply; map them to the canonical jugnu env var via the `_bridge_keys()` helper from §8 if your deployment requires keeping the hyphenated form.

| Secret name (suggested) | Mounted as env var | Required for | Notes |
|---|---|---|---|
| `proxy-provider` | `PROXY_PROVIDER` | all proxy modes | Constant `"brightdata"` for the PropAi prod path. |
| `proxy-brightdata-customer-id` | `PROXY_BRIGHTDATA_CUSTOMER_ID` | Bright Data | Bright Data dashboard → customer page. |
| `proxy-brightdata-zone` | `PROXY_BRIGHTDATA_ZONE` | Bright Data | Bright Data dashboard → zone name (e.g. `residential1`). |
| `proxy-brightdata-password` (or reuse `proxy-password`) | `PROXY_PASSWORD` | Bright Data | Zone password from Bright Data. Falls back to `PROXY_PASSWORD` if `PROXY_BRIGHTDATA_PASSWORD` is unset. |
| `proxy-brightdata-country` | `PROXY_BRIGHTDATA_COUNTRY` | Bright Data (optional) | Country filter, e.g. `us`. Omit for any country. |
| `proxy-brightdata-sticky-per-url` | `PROXY_BRIGHTDATA_STICKY_PER_URL` | Bright Data (optional) | `"0"` to disable URL-stickiness. Default on. |
| `proxy-host` | `PROXY_HOST` | Bright Data (optional) | Defaults to `brd.superproxy.io`. |
| `proxy-port` | `PROXY_PORT` | Bright Data (optional) | Defaults to `22225`. |
| `proxy-server` | `PROXY_SERVER` | static mode | Single-proxy URL when not using Bright Data. |
| `proxy-username` | `PROXY_USERNAME` | static mode (or Bright Data fallback for customer ID) | Static-mode auth. |
| `proxy-pool-urls` | `PROXY_POOL_URLS` | rotating mode | Comma-separated `user:pass@host:port` list of pool members. Use this when running a multi-vendor pool instead of Bright Data. |

Minimum set for the PropAi production path (Bright Data with sticky session per property): `proxy-provider`, `proxy-brightdata-customer-id`, `proxy-brightdata-zone`, `proxy-brightdata-password`. Everything else is optional with safe defaults.

The worker's service account needs `roles/secretmanager.secretAccessor` on each secret (or on the parent project). For Cloud Run, mount each secret with `--set-secrets PROXY_BRIGHTDATA_ZONE=proxy-brightdata-zone:latest,...`; for GKE, use a `secretKeyRef` in the pod spec; for Cloud Build, use the `availableSecrets.secretManager` block. Never bake credentials into the container image or `properties.csv`.

### Failure handling and quarantine

The rotating pool penalises consistently-failing proxies and quarantines them when health drops below 0.25. Hard failures (403, 407, captcha, auth errors) drop health by 0.25 each; soft failures (timeouts, 5xx, connection errors) drop by 0.05. Recoveries above 0.5 lift the quarantine. PropAi's daily-runner can therefore start the day with a fresh pool and rely on jugnu to pull noisy IPs out of rotation automatically — same behaviour as `ma_poc/scraper/proxy_manager.py` but living one layer down.

### What does NOT change in PropAi

- `ma_poc/scraper/proxy_manager.py` and the existing PropAi proxy code stay in place during the POC; once `jugnu_poc/runner.py` proves parity it'll be retired in M6.
- No PropAi-side Bright Data username assembly — that's now jugnu's responsibility.
- `daily_runner.py --proxy http://user:pass@host:port` still works after M6: pass that string into `build_proxy_provider_from_env()` (set `PROXY_SERVER=...`) or wrap it in `StaticProxyProvider(ProxyConfig.parse(s))` and inject into `Jugnu(...)`.

---

## Appendix A — Field-by-field mapping (Jugnu output → V2 schema)

PropAi V2 = 46-key property record + nested units. Jugnu returns the same shape (per the schema in §2). `property_transform` does the rename + normalize + CSV-priority merge.

| V2 key | Jugnu source | Transform |
|---|---|---|
| `apartment_id` | CSV `apartmentid` | `_safe_int()` |
| `proj_name` | CSV `name` → fallback `record["proj_name"]` | CSV-priority |
| `address` | CSV → fallback `record["address"]` | CSV-priority |
| `city` / `state` / `zip_code` | CSV → fallback `record[*]` | CSV-priority + `_format_zip_5()` |
| `phone` | CSV → fallback `record["phone"]` | CSV-priority |
| `email_address` | `record["email_address"]` | as-is |
| `website` | CSV `website` → fallback `Blink.url` | CSV-priority |
| `pmc` | CSV → fallback `record["pmc"]` | CSV-priority |
| `website_design` | derived from `Blink.tier_used` platform_guess | `_PLATFORM_LABELS` map |
| `concessions` | `record["concessions"]` | as-is |
| `units[].beds` | `record["units"][].beds` | `_normalize_beds()` |
| `units[].baths` | `record["units"][].baths` | `_normalize_baths()` |
| `units[].floor_plan_name` | `record["units"][].floor_plan_name` | junk filter |
| `units[].area` | `record["units"][].area` | `_format_area()` |
| `units[].unit_id` | `record["units"][].unit_id` | junk filter, str cast |
| `units[].rent_low/high` | `record["units"][].rent_low/high` | `_format_rent()`, parse `rent_range` if needed |
| `units[].date_captured` | `Blink` timestamp | format `%Y-%m-%d %H:%M:%S` |
| `units[].available_date` | `record["units"][].available_date` | `_format_date()` |
| `units[].lease_term` | `record["units"][].lease_term` | `_safe_lease_term()` |
| `units[].move_in_date` | `record["units"][].move_in_date` | `_format_date()` |
| (14 other null-only fields) | external sources, not Jugnu | `null` |

The aggregates (`Average Unit Size`, `Total Units`, `Unit Mix`, `First Move-In Date`) are computed by `property_transform` from the normalized units list — same as today's `_format_v2`.
