# PropAi × Jugnu — Integration Architecture v1

**Author:** ashuchan
**Date:** 2026-04-25
**Scope:** Replace PropAi's in-tree scraper (`ma_poc/scripts/jugnu_runner.py` + `entrata.py` + `extraction/` + `templates/` + `pms/adapters/`) with a thin shell around the standalone Jugnu library, while preserving PropAi's V2 output contract, identity resolution, state diff, carry-forward, and reporting.

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

These are gaps the library would normally close with new fields, but where threading more context into the existing prompts achieves the same outcome at zero cost to Jugnu's surface area.

| # | PropAi need | Prompt-enrichment fix | Library change still needed? |
|---|---|---|---|
| 4 | **Per-property context in LLM prompts** (city, state, pmc, property_name, expected_total_units) | Inject `CrawlInput.metadata` as a dedicated `{input_metadata}` block in **every** Spark prompt. The prompt sees: "You are extracting from a property the caller already knows is named 'San Artes Apartments' in Scottsdale AZ 85255, managed by Mark-Taylor, with ~46 units expected." This anchors `proj_name`, suppresses LLM hallucination of address fields, and lets the model sanity-check unit counts against the expected total. | YES — small: thread `CrawlInput.metadata` through `discovery_llm`, `extraction_llm`, `external_ranker_llm` as a template variable. ~30 LOC. |
| 6 | **Blocked-endpoint learning** ("this API is noise, don't reanalyze") | Prompt-1 (discovery) already accepts `{known_noise_patterns}` from SkillMemory. Per-URL noise can ride in the per-URL ScrapeProfile and be passed into Prompt-1 as `{url_specific_noise}`. The LLM downranks them deterministically. Smart-trigger consolidation already promotes confirmed noise to skill-wide. | NO library schema change — just include the per-URL profile's `api_hints.confirmed_endpoints` (negated set) as another prompt fragment. |
| 9 | **Junk filters** (`MODULE_*`, "Lease Magnet", `[Riedman]`, stop-word unit numbers) | `Skill.negative_keywords` is plumbed into all extraction prompts as: "REJECT any record whose floor_plan_name or unit_id matches these substrings: {negative_keywords}". Already drafted into the `custom_instructions` above. The LLM filters at extraction time; deterministic adapters (`schema_normalizer`) also apply the list as a final safety net. | YES — small: wire `Skill.negative_keywords` into both Spark prompt templates AND `glow/schema_normalizer.py`. The wiring exists in the schema; it just isn't consumed yet. |
| 10 | **Per-field validators** (beds [0,7], baths step 0.5, area [150,10000], rent>1, zip 5 digits) | Embed bounds + reject rules directly in `custom_instructions` (already drafted above) AND in the `OutputSchema.json_schema`. Prompt-2 already receives `{output_schema_json}` — passing the json_schema with `minimum`/`maximum`/`pattern` constraints lets the LLM self-correct. | NO library change required if we treat `custom_instructions` as the contract. **Optional improvement**: have `validation/schema_gate.py` actually run `jsonschema` against `OutputSchema.json_schema` (it's currently unused). One PR, ~15 LOC. |
| 5 | **Deterministic LLM-mapping replay** (`json_paths`, `response_envelope`, `success_count`) — saves dollars per run after warm-up | Cannot be solved by prompt enrichment alone — replay is by definition zero-LLM. **However**, prompt enrichment makes Prompt-2 EMIT richer mappings: the prompt template already requires `field_mappings.api.json_paths` + `response_envelope` in the response. We just need the post-Prompt-2 crystallizer to write those onto the per-URL `ScrapeProfile`, and a Tier-1 replay adapter to consume them next run. | YES — this is the single highest-cost gap. Extend `LlmFieldMapping` with `api_url_pattern`/`json_paths`/`response_envelope`/`success_count`, and wire `glow/tiers/tier1_profile.py` to apply them. Already in Jugnu's P4/P5 plan per `context.md` §6.1 — **confirm it's actually wired** during M1. |
| 7 | **Multi-page CTA-hop + iframe-resolve + leasing-portal redirect** (50%+ of PropAi success comes from here) | **Partially** by prompt enrichment: Prompt-1's response includes `navigation_hint`. We can have the runner act on `navigation_hint` between Jugnu calls — issue a second `Jugnu.crawl()` for the hinted URL with the parent's context preserved. This gets us the right behavior without a Jugnu library change, at the cost of two crawl roundtrips per CTA-hop site. | EVENTUALLY YES — Jugnu's `GlowResolver` is documented in §6.1 to do CTA-hop/iframe/redirect, but the current `crawler.py:107-113` only calls it once on a single fetch. Long-term fix: wire `GlowResolver` to BFS over `SkillMemory.high_confidence_link_keywords` with per-page network re-observation, capped by a new `max_internal_pages` setting. **Workable without this for M1-M2 via the navigation_hint loop.** |

### Net library changes proposed to Jugnu

Down from 6 in the original plan to **4 small + 1 large**:

1. **(small)** Thread `CrawlInput.metadata` into every Spark prompt as `{input_metadata}` — gap 4
2. **(small)** Consume `Skill.negative_keywords` in Spark prompts AND `schema_normalizer` — gap 9
3. **(small)** Run `OutputSchema.json_schema` in `validation/schema_gate.py` — gap 10 (optional)
4. **(small)** Pass per-URL `ScrapeProfile.api_hints.blocked_endpoints` into Prompt-1 as `{url_specific_noise}` — gap 6
5. **(large)** Confirm/wire `LlmFieldMapping` with `json_paths`/`response_envelope`/`success_count` + Tier-1 replay adapter — gap 5

Items 1, 2, 3, 4 are all "thread one more variable into a prompt template." The only design discussion is item 5, and that's already in Jugnu's own plan.

The previously-proposed `Blink.parent_record` is **dropped** per direction.
The previously-proposed multi-page BFS in `GlowResolver` is **deferred** — handled in the runner via the `navigation_hint` loop until Jugnu's resolver is wired.

---

## 5. Prompt enrichment — concrete additions per prompt

Where each enrichment lands, so Jugnu maintainers see exactly what to thread through.

### Prompt-1 (Discovery)
- `{input_metadata}` — property name, city, state, pmc, expected_total_units (NEW)
- `{url_specific_noise}` — per-URL ScrapeProfile blocked_endpoints (NEW)
- `{known_noise_patterns}` — already from SkillMemory
- `{skill_memory_prompt1_context}` — already from SkillMemory
- `{negative_keywords}` — Skill.negative_keywords (NEW, downrank links/APIs matching these)

### Prompt-2 (Extraction)
- `{input_metadata}` — same property context (NEW). Anchors proj_name, suppresses address hallucination.
- `{output_schema_json}` — already passed; ensure `json_schema` constraints (min/max/pattern) come through (NEW: enforce schema is the rich one)
- `{negative_keywords}` — REJECT records matching these substrings (NEW)
- `{custom_instructions}` — Skill.custom_instructions (already passed but verify it's the full text, not truncated)
- `{confirmed_field_synonyms_json}` — already from SkillMemory
- `{skill_memory_prompt2_context}` — already from SkillMemory

### Prompt-3 (Merge)
- `{input_metadata}` — same property context (NEW). Helps disambiguate "is unit 101 in this run the same unit 101 from last run, or did we crawl a different building."
- `{merging_keys}` — already passed

### Prompt-4 (External)
- `{input_metadata}` (NEW) — same as above
- `{negative_keywords}` (NEW)
- Already gets `{known_noise_patterns}` and `{skill_memory_prompt4_context}`

### Prompt-5 (Warmup)
- No per-URL context (it's pre-URL by design)
- `{negative_keywords}` (NEW) so Prompt-5 can seed `known_noise_patterns` from the skill's own blocklist

### Prompt-6 (Consolidation)
- No per-URL context (it's a batch summarizer)
- Already aware of negative_keywords via SkillMemory

---

## 6. Suggested milestones

- **M1 — wire dependency, run hello-world** (1-2 days)
  Add Jugnu as path dep; define `PROPAI_SKILL`; run `jugnu_poc_runner.py --limit 1` against one known-good RentCafe property; verify `Blink.records[0]` contains property + nested units; document gaps observed in practice; verify `LlmFieldMapping` replay actually works (gap 5).

- **M2 — close caller-side gaps + ship prompt enrichments upstream** (3-5 days)
  Build `property_transform`, `memory_store`, `profile_store_adapter`; port V2 normalization; open Jugnu PRs for prompt-enrichment items 1, 2, 4 (small ones); land them; rerun `--limit 5` and compare output to `jugnu_runner.py --limit 5` byte-for-byte (or document the diffs).

- **M3 — multi-page navigation via navigation_hint loop** (2-3 days)
  Implement the runner-side hop: when `Blink.tier_used == "llm"` and `llm_interactions` shows a `navigation_hint`, issue a second `Jugnu.crawl()` for the hinted URL with the parent's metadata. This gives us multi-page support without waiting for Jugnu's `GlowResolver` BFS work.

- **M4 — soak test on full 500** (1 week)
  Run both pipelines on the full CSV. Compare success rate, LLM cost, unit counts. Switch over only when Jugnu is at parity or better.

- **M5 — propose Jugnu PR for `LlmFieldMapping` replay** (gap 5) if not already wired
  This is the single biggest cost lever. After M4 we'll have data on how often we'd hit this fast path.

- **M6 — eventual switch-off**
  Once Jugnu is at parity, delete `ma_poc/scripts/jugnu_runner.py`, `entrata.py`, `extraction/`, `templates/`, `pms/adapters/`. Keep `identity.py`, `state_store.py`, `schema_v2.py`, `validation.py`, `scripts/daily_runner.py` only if anything still depends on them — otherwise also retire.

---

## 7. Open questions for the user

1. **State directory layout during the POC** — share `data/state/` with the existing `jugnu_runner.py` (so they're directly comparable run-over-run) or use `data/jugnu_poc/state/` so they can run in parallel without colliding?
2. **Jugnu dependency form** — local path install (`file:///c:/Users/.../Jugnu`) for the POC, or do you want to push Jugnu to a private git remote first and pin to a tag?
3. **Where prompt enrichment PRs land** — do you own the Jugnu repo and merge directly, or do these go in as PRs that need review by someone else?

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
