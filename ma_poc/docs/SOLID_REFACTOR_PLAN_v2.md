# `ma_poc/` — SOLID Refactor Plan v2

**Author:** Senior architecture review (replaces v1)
**Date:** 2026-05-07
**Supersedes:** [SOLID_REFACTOR_PLAN.md](SOLID_REFACTOR_PLAN.md) (v1)
**Scope:** Decompose god-modules, retire legacy parallel pipeline, and
migrate live behavior into the existing Jugnu 5-layer architecture.

---

## 0. Why v2 replaces v1

v1 is structurally sound but mis-scoped. Three problems force a rewrite:

1. **v1 invents bounded contexts that already exist.** The Jugnu pipeline
   already ships [`fetch/`](../fetch/), [`discovery/`](../discovery/),
   [`pms/`](../pms/), [`validation/`](../validation/),
   [`observability/`](../observability/), [`reporting/`](../reporting/)
   with frozen dataclass contracts and 161 layered tests
   ([`scripts/CLAUDE.md`](../scripts/CLAUDE.md) §"Cross-layer contracts").
   v1's proposed `extraction/engine/`, `state/`, `orchestration/`,
   `extraction/parsers/` would be a *third* parallel architecture next
   to the legacy `entrata.py` stack and Jugnu. The right move is
   **migrate into Jugnu**, not invent.
2. **v1 misses ~5,700 lines of god code.**
   [`scripts/health_report.py`](../scripts/health_report.py) (1,724),
   [`scripts/sync_run_to_pg.py`](../scripts/sync_run_to_pg.py) (1,700),
   [`scripts/generate_daily_report.py`](../scripts/generate_daily_report.py) (1,228),
   and [`pms/adapters/_html_extract.py`](../pms/adapters/_html_extract.py)
   (1,030) are not in v1's diagnosis but are larger than several modules
   v1 prioritises.
3. **v1 punts on "delete redundant code" and "two pipelines" — both of
   which the user explicitly asked to address.** The legacy
   `extraction/` BeautifulSoup stack (Phase A `tier1_api.py` …
   `tier5_vision.py`, [`pipeline.py`](../extraction/pipeline.py)) is
   dead per [`scripts/CLAUDE.md`](../scripts/CLAUDE.md). The legacy
   `daily_runner.py` + `entrata.py` pipeline duplicates host-parsing,
   identity, and rent normalisation that already exist (or should
   exist) in Jugnu adapters.

The full v1 critique is recorded in the chat transcript that produced
this document; the headlines above drive every change below.

---

## 1. Diagnosis — current ground truth (re-measured 2026-05-07)

### 1a. God modules

| Module | Lines | One reason this is too big |
|---|---:|---|
| [`scripts/entrata.py`](../scripts/entrata.py) | 3,156 | `scrape()` is a 700-line, 7-phase god function with ~100 local state vars. |
| [`pms/adapters/generic.py`](../pms/adapters/generic.py) | 2,145 | `_extract_inner()` runs sub-tiers 0–6c inline; LLM prompts and merge rules baked in. |
| [`scripts/health_report.py`](../scripts/health_report.py) | 1,724 | **(missed by v1)** Multiple report shapes + queries + formatters in one file. |
| [`scripts/sync_run_to_pg.py`](../scripts/sync_run_to_pg.py) | 1,700 | **(missed by v1)** Schema mapping + retention + 8 per-table syncs in one module. |
| [`scripts/jugnu_runner.py`](../scripts/jugnu_runner.py) | 1,641 | `run_jugnu()` (107–1598) wires 5 layers, CSV, format routing, report. `_process_property()` ≈270 lines. |
| [`scripts/daily_runner.py`](../scripts/daily_runner.py) | 1,427 | `run_daily()` is ≈846 lines: CSV → identity → dedup → state → scrape → diff → carry-fwd → record → report. |
| [`scripts/generate_daily_report.py`](../scripts/generate_daily_report.py) | 1,228 | **(missed by v1)** Mixed JSON/Markdown/CSV emitters + aggregations. |
| [`scripts/retry_runner.py`](../scripts/retry_runner.py) | 1,097 | Imports 6 internals from `daily_runner`; `run_retry()` ≈700 lines mirrors `run_daily()`. |
| [`pms/adapters/_html_extract.py`](../pms/adapters/_html_extract.py) | 1,030 | **(missed by v1)** Generic HTML utilities mixed with adapter-specific helpers. |
| [`scripts/scrape_properties.py`](../scripts/scrape_properties.py) | 969 | Four `_*_units_from_body()` parsers with no common interface; logic re-implemented in `entrata.py`. |
| [`services/llm_extractor.py`](../services/llm_extractor.py) | 865 | Three modes (`extract_with_llm`, `analyze_api_with_llm`, `analyze_dom_with_llm`) on one fat surface; prompts hard-coded; provider not injectable. |

**Top-11 total: ~16,982 lines.** Target: **≤ 4,500 lines** (~73 % reduction)
via consolidation into Jugnu layers + deletion of legacy duplicates.

### 1b. Cross-module duplication

| Logic | Duplicated in |
|---|---|
| 50+ generic API key-name variants | `entrata.py::parse_api_responses`, `generic.py::parse_generic_api`, `scrape_properties.py::_generic_units_from_body` |
| `_extract_rent` (nested `{min,max}` / list `[{rent,term}]`) | `entrata.py`, `scrape_properties.py`, `generic.py` (inline) |
| SightMap / RealPage / AvalonBay parsers | `scrape_properties.py`, `entrata.py` |
| Identity resolution heuristics | `identity.py`, `jugnu_runner._SimpleProfileStore`, fallbacks in `generic.py` |
| Carry-forward decision | `daily_runner.run_daily`, `retry_runner.run_retry`, `discovery/carry_forward.py` |
| Report aggregation | `scripts/scrape_report.py`, `scripts/generate_daily_report.py`, `reporting/run_report.py`, `scripts/health_report.py` |

### 1c. Dead / parallel code that v1 leaves alive

- [`extraction/pipeline.py`](../extraction/pipeline.py),
  [`tier1_api.py`](../extraction/tier1_api.py) …
  [`tier5_vision.py`](../extraction/tier5_vision.py),
  [`vision_banner.py`](../extraction/vision_banner.py),
  [`vision_sample.py`](../extraction/vision_sample.py),
  [`heuristics.py`](../extraction/heuristics.py),
  [`confidence.py`](../extraction/confidence.py) — the BRD-spec
  Phase A BeautifulSoup stack. Confirmed inactive by
  [`scripts/CLAUDE.md`](../scripts/CLAUDE.md) §"Relationship to
  ma_poc/templates/ and ma_poc/extraction/".
- [`templates/rentcafe.py`](../templates/rentcafe.py),
  [`entrata.py`](../templates/entrata.py),
  [`appfolio.py`](../templates/appfolio.py) — Phase A templates,
  inactive.
- [`scripts/run_phase_a.py`](../scripts/run_phase_a.py) — explicitly
  "not used in production" per CLAUDE.md.
- Empty placeholder dirs created earlier today suggest stalled work:
  [`extraction/engine/phases/`](../extraction/engine/phases/),
  [`extraction/parsers/`](../extraction/parsers/),
  [`pms/adapters/tiers/`](../pms/adapters/tiers/),
  [`services/llm/`](../services/llm/),
  [`scripts/state/`](../scripts/state/),
  [`scripts/reporting/`](../scripts/reporting/),
  [`scripts/orchestration/`](../scripts/orchestration/).

### 1d. Architectural reality v1 ignored

The Jugnu pipeline already enforces SRP at the layer boundary. Every
inter-layer flow is a frozen `@dataclass` (`FetchResult`, `CrawlTask`,
`ExtractResult`, `ValidatedRecords`, `Event`). Tests are organised by
layer (`tests/fetch/`, `tests/discovery/`, `tests/pms/`,
`tests/validation/`, `tests/observability/`, `tests/reporting/`). The
refactor's job is to **fill these layers with the legacy bulk and
delete the duplicates**, not to design a parallel set of contexts.

---

## 2. Strategy — migrate-and-retire, not carve-and-add

Three principles drive every PR:

1. **Migrate into existing Jugnu layers.** Where v1 proposes
   `extraction/engine/`, use `pms/adapters/` + `fetch/`. Where v1
   proposes `state/`, use the existing `discovery/`, `validation/`, and
   the not-yet-existent `state/` only as a thin home for what truly
   doesn't fit. Where v1 proposes `orchestration/`, use the empty
   `scripts/orchestration/` that's already on disk.
2. **Retire the legacy pipeline, don't preserve it.** `daily_runner.py`
   becomes a thin wrapper that delegates to Jugnu. `retry_runner.py`
   becomes a row-filter strategy on top of the same. `entrata.py` is
   deleted (its useful host-parsing knowledge migrates into
   `pms/adapters/<host>.py`). Phase A `extraction/` is deleted outright.
3. **Decompose only what stays.** Of the 11 god modules, the four that
   *belong* in a refactored ma_poc — `generic.py`, `llm_extractor.py`,
   `jugnu_runner.py`, and the report family — get internal SRP
   decomposition. The other seven get retired or migrated.

### 2a. Target layout (concrete)

```
ma_poc/
├── fetch/                       # L1 — already exists, untouched here
├── discovery/                   # L2 — already exists, untouched here
├── pms/
│   ├── adapters/
│   │   ├── generic/             # NEW — package replaces generic.py (PR-2)
│   │   │   ├── __init__.py      # exports GenericAdapter facade < 200 LOC
│   │   │   ├── tiers/           # 0_blocked, 0_replay, 1_api_narrow,
│   │   │   │                    # 2_api_broad, 3_jsonld, 4_embedded,
│   │   │   │                    # 5_dom, 6a_llm_api, 6b_llm_dom, 6c_llm_mono
│   │   │   ├── orchestrator.py  # tier sequencing (no Strategy pattern, just a list)
│   │   │   ├── merger.py        # the current _merge_field_values() rule chain
│   │   │   └── gates.py         # skip_llm / LLM_GATE_RELAXED policy
│   │   ├── parsers/             # NEW — was extraction/parsers/ in v1 (PR-3)
│   │   │   ├── _base.py         # UnitParser protocol
│   │   │   ├── sightmap.py      # joins units to floor plans
│   │   │   ├── realpage.py      # split-endpoint /floorplans + /units
│   │   │   ├── avalon.py
│   │   │   ├── onesite.py
│   │   │   ├── entrata_widget.py
│   │   │   ├── generic_api.py   # 50+ key-name variants — single home
│   │   │   └── registry.py      # host fingerprint → parser
│   │   ├── _html_extract/       # NEW package replacing _html_extract.py (PR-3)
│   │   │   ├── selectors.py
│   │   │   ├── text_normalise.py
│   │   │   └── jsonld.py
│   │   └── … (other adapters keep their current single-file form)
├── services/
│   └── llm/                     # NEW — package replaces llm_extractor.py (PR-4)
│       ├── __init__.py          # facade re-exports
│       ├── _protocols.py        # MonolithicExtractor / ApiAnalyzer / DomAnalyzer
│       ├── prompts.py           # loaded from config/prompts/ — no inline strings
│       ├── monolithic.py        # extract_with_llm()
│       ├── api_analyzer.py      # analyze_api_with_llm()
│       ├── dom_analyzer.py      # analyze_dom_with_llm()
│       ├── mapping_replay.py    # apply_saved_mapping()
│       ├── normaliser.py        # config-driven field-name mapping
│       ├── retry_policy.py      # tenacity wrapper with jitter
│       └── factory.py           # provider selection (Anthropic | Azure | …)
├── validation/                  # L4 — already exists, untouched here
├── observability/               # L5 — already exists, untouched here
├── reporting/                   # already exists, expanded by PR-6
│   ├── run_report.py            # current — slim down
│   ├── verdict.py               # current — keep
│   ├── property_report.py       # current — keep
│   ├── observation_reports.py   # current — keep
│   ├── aggregator.py            # NEW — stats tracked during loop, not 2nd pass
│   ├── markdown_writer.py       # NEW — was scattered in 3 files
│   ├── json_writer.py           # NEW
│   ├── slo_section.py           # NEW
│   └── health.py                # NEW — replaces scripts/health_report.py
├── state/                       # NEW (PR-5) — pulled from daily_runner.py
│   ├── _protocols.py            # StateStore / ProfileStore protocols
│   ├── csv_loader.py
│   ├── identity_resolver.py     # 5-tier cascade as resolver chain
│   ├── dedup_detector.py
│   ├── record_builder.py        # build_property_record
│   ├── state_diff.py            # new / updated / unchanged / disappeared
│   ├── carry_forward.py         # single source — replaces 3 copies
│   └── stores/
│       ├── filesystem.py        # current JSON impl
│       └── postgres.py          # respects sync_run_to_pg.py 3-day retention contract
├── scripts/
│   ├── orchestration/           # NEW — was empty (PR-1)
│   │   ├── pipeline.py          # generic Pipeline[T] + Stage protocol
│   │   ├── stages/              # csv_load / identity / dedup / scrape / state_sync / report
│   │   └── concurrency.py       # owns AsyncPool / ThreadedPool
│   ├── jugnu_runner.py          # SHRINK to ~150 lines (PR-1)
│   ├── daily_runner.py          # SHRINK to ~80-line shim that delegates to Jugnu (PR-7)
│   ├── retry_runner.py          # SHRINK to ~100-line RetryPipeline (PR-7)
│   ├── sync_run_to_pg/          # NEW package (PR-6)
│   │   ├── __init__.py          # CLI entrypoint
│   │   ├── retention.py         # 3-day rolling cap (memory-pinned contract)
│   │   ├── tables/              # one module per per-run table
│   │   └── upserts.py           # properties / units / scrape_profiles
│   ├── generate_daily_report.py # SHRINK to a thin caller of reporting/ (PR-6)
│   ├── health_report.py         # DELETE — moved to reporting/health.py (PR-6)
│   ├── scrape_properties.py     # SHRINK to ≤200 lines — host parsers move out (PR-3)
│   ├── entrata.py               # DELETE in PR-1 (knowledge migrates first)
│   └── … (other small scripts unchanged)
└── extraction/                  # DELETE the Phase A stack in PR-0 — see §4
```

### 2b. SOLID payoff (one line each)

- **SRP** — every module ≤ 400 lines, every public function ≤ 80 lines.
- **OCP** — new PMS = drop a `pms/adapters/parsers/<host>.py` + register.
  No edits to the orchestrator or other adapters.
- **LSP** — `UnitParser`, `Tier`, and `LLMExtractor` impls are
  substitutable behind their protocol; debug data lives on the
  `AdapterContext`/`PhaseContext`, never on result dicts.
- **ISP** — three narrow LLM protocols replace one fat module; callers
  depend only on what they use.
- **DIP** — adapters depend on `ProfileStore`, `StateStore`,
  `LLMExtractor`, `ProxyPool` protocols; concretes injected at the
  runner. Postgres / filesystem / Redis swappable without touching the
  engine.

---

## 3. Migration plan — 7 incremental PRs

Each PR is independently shippable. CLAUDE.md gates apply to every PR:
`pytest .` green, `mypy --strict` clean, `ruff check` clean,
`smoke_test.py` 5/5, and a 50-property semantic-diff against the
pre-PR baseline (see §5).

| PR | Scope | Risk | Behavioural-parity check |
|---|---|:---:|---|
| **PR-0** | **Delete dead code.** Remove `extraction/{pipeline,tier1_api…tier5_vision,vision_banner,vision_sample,heuristics,confidence}.py`, `templates/`, `scripts/run_phase_a.py`, and the four empty placeholder dirs. | Low | None — code is unreachable. Verify by running grep across imports + full test suite. |
| **PR-1** | **Retire `entrata.py`.** Migrate host-specific parsing into `pms/adapters/parsers/<host>.py`. Lift `Phase 1–7` orchestration into `pms/adapters/generic/orchestrator.py`. Delete `entrata.py`. `daily_runner.py` is rewritten in PR-7 — until then, daily_runner imports from the new locations. | **High** | 50-property semantic-diff vs pre-PR-0 baseline; tier-distribution within ±5 %; LLM-call count within ±5 %. |
| **PR-2** | **Decompose `pms/adapters/generic.py`** into the `generic/` package described in §2a. Plain ordered list of tier objects — no PhaseRegistry, no injectable strategy (YAGNI per §0/v1 critique). | Med | Profile-replay determinism: pre/post-PR `apply_saved_mapping()` byte-equal on 100 saved mappings. Cost-ledger spend within ±$0.05 per 50-row run. |
| **PR-3** | **Consolidate parsers + decompose `_html_extract.py`**. Move `_*_units_from_body` from `entrata.py` (post-PR-1) and `scrape_properties.py` into `pms/adapters/parsers/`. Split `_html_extract.py` into the 3-file package. | Med | SightMap + RealPage + AvalonBay unit counts per property unchanged. |
| **PR-4** | **Decompose `services/llm_extractor.py`** into `services/llm/`. Three protocols, externalised prompts, retry-policy module, provider factory. The empty `services/llm/` dir on disk is the home. | Med | LLM-call count and cost within ±5 %. Prompt-hash equality for the externalised prompts. |
| **PR-5** | **Extract `state/` from `daily_runner.py`.** Lift identity, dedup, record building, state diff, carry-forward into the new `state/` package. `daily_runner.run_daily()` shrinks to ≤300 lines (still legacy, but slim). | **High** | `property_index.json` + `unit_index.json` structural diff = empty after a full run. |
| **PR-6** | **Consolidate reporting + sync_run_to_pg.** Move `scripts/health_report.py` → `reporting/health.py`. Split `scripts/generate_daily_report.py` into thin caller + `reporting/` modules. Split `scripts/sync_run_to_pg.py` into a package, **preserving the 3-day retention contract** verbatim (memory-pinned). | Med | Postgres rows after sync byte-equal; retention sweep produces same row counts; report markdown diff = empty. |
| **PR-7** | **Retire `daily_runner.py` + collapse `retry_runner.py`.** `daily_runner.py` becomes a ≤80-line shim invoking Jugnu's pipeline; `retry_runner.py` becomes a ≤100-line `RetryPipeline` = Jugnu pipeline + row-filter strategy. Drop the 6-symbol re-export coupling. `jugnu_runner.run_jugnu()` shrinks to ≤30 lines using `scripts/orchestration/pipeline.py`. | **High** | Resume + retry-errors modes produce identical ledger merges as today on a synthetic run with seeded failures. |

### Per-PR checklist

1. `pytest . -v --ignore=data --ignore=config` exits 0.
2. `ruff check ma_poc/` clean.
3. `mypy ma_poc/ --strict` clean.
4. `python ma_poc/scripts/smoke_test.py` passes 5/5.
5. 50-property semantic-diff (§5) vs pre-PR baseline empty.
6. `python ma_poc/scripts/validate_outputs.py` shows no regression on
   the 10 CLAUDE.md metrics.
7. Bug-Hunt items relevant to the touched code path re-checked.
8. `lint-imports` (import-linter, added in PR-0) passes — see §6.

### Sequencing rationale

- **PR-0 is purely subtractive** and validates the test suite still
  passes without the dead code. Lowest-risk warmup.
- **PR-1 is the linchpin.** Until `entrata.py` knowledge is migrated,
  every other PR is fighting two parallel parsers. Schedule first
  among load-bearing PRs.
- **PR-2 and PR-4 can run in parallel** if reviewer bandwidth allows;
  they touch disjoint code.
- **PR-3 depends on PR-1** (parsers come out of `entrata.py`).
- **PR-5 depends on PR-1** (state code currently calls `entrata.scrape`
  through `daily_runner`).
- **PR-6 is independent of PR-1–5** and can ship any time after PR-0.
- **PR-7 is last.** Until it lands, `daily_runner.py` keeps calling
  Jugnu via the migrated paths but is still a fat orchestrator.

---

## 4. Explicit deletion list

User asked for redundant classes deleted. These come out across PR-0
and PR-1:

**PR-0 deletions (dead code):**
- `ma_poc/extraction/pipeline.py`
- `ma_poc/extraction/tier1_api.py`
- `ma_poc/extraction/tier2_jsonld.py`
- `ma_poc/extraction/tier3_templates.py`
- `ma_poc/extraction/tier4_llm.py`
- `ma_poc/extraction/tier5_vision.py`
- `ma_poc/extraction/vision_banner.py`
- `ma_poc/extraction/vision_sample.py`
- `ma_poc/extraction/heuristics.py`
- `ma_poc/extraction/confidence.py`
- `ma_poc/templates/rentcafe.py`, `entrata.py`, `appfolio.py`
- `ma_poc/scripts/run_phase_a.py`
- empty placeholder dirs: `extraction/engine/phases/`,
  `extraction/parsers/`, `pms/adapters/tiers/`

**PR-1 deletions (after migration):**
- `ma_poc/scripts/entrata.py` — knowledge moved to
  `pms/adapters/parsers/` and `pms/adapters/generic/`
- duplicated `_extract_rent`, `_UNIT_ID_KEYS`, `_RENT_KEYS`,
  `_FALSE_POSITIVE_HOSTS`, `_FALSE_POSITIVE_PATH_FRAGMENTS` in three
  modules collapse to one canonical home in `pms/adapters/parsers/`

**PR-7 deletions:**
- `ma_poc/scripts/retry_runner.py` god body — becomes a 100-line
  strategy file
- `ma_poc/scripts/daily_runner.py` god body — becomes an 80-line shim

Each deletion is gated on a passing semantic-diff (§5) and a grep
showing zero remaining imports.

---

## 5. Behavioural-parity strategy (replaces v1 "byte-equal")

v1 demanded byte-equal output. That breaks on dict-insertion-order
changes, datetime micro-jitter, and Python-version-dependent JSON
serialisation. v2 uses a **semantic-diff harness** + **shadow-mode**.

### 5a. Semantic-diff harness (new, lands in PR-0)

`ma_poc/scripts/refactor_diff.py` reads two `properties.json` files and
asserts:

- **Property set:** equal `canonical_id` set.
- **Unit set per property:** equal `unit_id` set (or fingerprint-set
  when `unit_id` is missing).
- **Numeric tolerance:** `market_rent_low/high` and `area` within ±$1
  / ±1 sqft; `lat/lng` within ±0.0001.
- **Categorical equality:** `availability_status`, `pms_platform`,
  `verdict`, `tier_used` exact.
- **Ignore by design:** ISO-timestamp microseconds, dict key order,
  `_meta.run_id`, `_meta.scrape_timestamp`.
- **Diff output:** structured JSON listing every divergence with
  property + field + before + after.

Every PR must produce an empty diff against the baseline captured at
the start of PR-0.

### 5b. Shadow mode (used during PR-1 and PR-7)

Both PR-1 and PR-7 are high-blast-radius. For each, ship the new path
behind an env flag (`USE_NEW_GENERIC_ADAPTER=1`,
`USE_NEW_DAILY_SHIM=1`). Run a 50-row scrape with the flag both ways
in CI; require the semantic-diff between flag-on and flag-off to be
empty before flipping the default. Flag is removed in the next PR.

Rollback for either PR is a single env-var flip — not `git revert`.

---

## 6. Architectural-boundary enforcement

Add `import-linter` config in PR-0:

```ini
[importlinter]
root_packages =
    ma_poc

[importlinter:contract:layers]
name = Jugnu layer dependencies
type = layers
layers =
    ma_poc.scripts
    ma_poc.reporting
    ma_poc.observability
    ma_poc.validation
    ma_poc.pms
    ma_poc.fetch | ma_poc.discovery
    ma_poc.state
    ma_poc.services.llm | ma_poc.services
    ma_poc.models
```

Each PR's CI runs `lint-imports` and fails on violation. This is the
single biggest insurance policy against the contexts drifting back
into mush.

---

## 7. Documentation updates (per user request)

Each PR ends with documentation updates committed alongside the code:

- **PR-0:** [`scripts/CLAUDE.md`](../scripts/CLAUDE.md) §"Relationship
  to ma_poc/templates/ and ma_poc/extraction/" deleted; root
  [`CLAUDE.md`](../../CLAUDE.md) repository-structure block updated.
- **PR-1:** [`scripts/CLAUDE.md`](../scripts/CLAUDE.md) §"7-Phase
  Extraction Pipeline (entrata.py)" replaced by a
  `pms/adapters/generic/` reference. Module-level docstring on every
  new file.
- **PR-2:** [`scripts/CLAUDE.md`](../scripts/CLAUDE.md) §"Extraction
  cascade inside GenericAdapter (2026-04-19 refactor)" rewritten in
  terms of the new tier modules; tier-key strings unchanged so log
  consumers don't break.
- **PR-3:** new [`pms/adapters/parsers/README.md`](../pms/adapters/)
  documenting how to add a new host parser.
- **PR-4:** new [`services/llm/README.md`](../services/) documenting
  the three protocols and provider factory; `config/prompts/` index
  added.
- **PR-5:** [`scripts/CLAUDE.md`](../scripts/CLAUDE.md) §"State
  tracking" / "Identity resolution" / "Validation" sections updated
  with new file paths.
- **PR-6:** [`scripts/CLAUDE.md`](../scripts/CLAUDE.md) §"Postgres
  retention (2026-05-02)" updated with the new package layout; memory
  note `project_postgres_retention_policy` left intact (pinned
  contract).
- **PR-7:** [`scripts/CLAUDE.md`](../scripts/CLAUDE.md) §"The three
  pipeline paths" replaced with a single Jugnu-as-canonical section;
  `daily_runner.py` and `retry_runner.py` documented as compatibility
  shims.

The root [`CLAUDE.md`](../../CLAUDE.md) Repository-structure block is
updated in **every** PR that adds or removes a directory.

---

## 8. Risks + mitigations

### R1 — Behavioural drift in `entrata.scrape()` migration (PR-1)

The 7-phase function has accumulated edge cases: HTTP→HTTPS
normalisation, `MAX_CRAWL_PAGES = 10`, expander-click swallows
exceptions, redirect capture for portal iframes,
`_FALSE_POSITIVE_HOSTS` (30+), `_FALSE_POSITIVE_PATH_FRAGMENTS` (15+),
SightMap/RealPage parsers, profile-blocklist composition.

**Mitigation:** semantic-diff harness (§5a) + shadow mode (§5b). Snapshot
50-row baseline before PR-0. Empty diff is a hard merge gate.

### R2 — Profile-format compatibility

`config/profiles/{canonical_id}.json` files are on-disk state with
many forward-compatible fields ([`models/scrape_profile.py`](../models/scrape_profile.py)).

**Mitigation:** no PR changes the on-disk schema. Add a regression
test in PR-0 that loads 50 representative profiles from
`tests/fixtures/profiles/` and asserts deserialisation.

### R3 — Test-coverage gap on `pms/adapters/generic.py`

Survey shows ~4 tests under `tests/pms/adapters/`. Splitting 2,145
lines behind that thin a net is dangerous.

**Mitigation:** PR-2 starts by adding a baseline integration test
asserting unit counts on 10 representative URLs (covering SightMap,
RealPage, RentCafe, Entrata, AppFolio, Squarespace, Wix, AvalonBay,
OneSite, generic). Each new tier module adds unit tests for its own
contract.

### R4 — Postgres retention contract (memory-pinned)

Memory `project_postgres_retention_policy` pins a 3-day rolling cap on
`scrape_events`, `dlq_entries`, `llm_*`, `property_snapshots`,
`run_issues`, `run_ledger`. `properties`, `units`, `scrape_profiles`
are upsert-only.

**Mitigation:** PR-6's split of `sync_run_to_pg.py` keeps
`_apply_retention()` byte-for-byte. A new test
`tests/scripts/test_sync_run_to_pg_retention.py` asserts the row counts
after a synthetic 7-day backfill match the pre-PR run exactly.

### R5 — `_meta` debug-field contracts

[`reporting/run_report.py`](../reporting/run_report.py) and
[`observability/slo_watcher.py`](../observability/slo_watcher.py) read
`_meta.verdict`, `_extract_result.tier_used`, `_winning_page_url`. The
2026-04-19 fix in [`scripts/CLAUDE.md`](../scripts/CLAUDE.md) §"Reporting
key source of truth" makes this a hard contract.

**Mitigation:** no PR changes `_meta.*` or `_extract_result.*` key
names. Contract test in PR-0 asserts the expected key set is present
on every output record. Tier-key strings (e.g. `generic:api_narrow`)
unchanged across PR-2 so log consumers don't break.

### R6 — Two-pipeline freeze period

PR-0 through PR-6 leave `daily_runner.py` partially refactored —
internally still a god function but calling new modules. Risk: drift
between Jugnu and legacy that nobody fixes because PR-7 is "coming."

**Mitigation:** PR-7 is timeboxed to within 4 weeks of PR-6 merging.
If it slips, post a CODEOWNERS rule that all `daily_runner.py` PRs
require sign-off from the refactor driver.

### R7 — `import-linter` adoption

New tooling in PR-0 may surface a flood of pre-existing violations.

**Mitigation:** PR-0 adds the contract in *report-only* mode. Each
subsequent PR removes one violation class until clean. PR-7 flips the
contract to *enforce*.

---

## 9. Out of scope

- Migrating to a different LLM provider (refactor enables it; the swap
  is a separate change).
- Postgres schema migrations beyond what each PR's tests need.
- Frontend / TS API changes (different bounded context entirely).
- Cross-property cluster learning (`cluster_id` field stays
  unimplemented).
- BRD-spec Phase A's vision-LLM 5–10 % accuracy sample
  (`vision_sample.py`) — deletion in PR-0 is final; if we want this
  back, it's a new, scoped feature against Jugnu's `pms/adapters/`.
- `concurrency.py` ↔ `AsyncPool`/`ThreadedPool` rewrite. Move the file
  to `scripts/orchestration/concurrency.py` in PR-7; do not refactor.

---

## 10. Definition of Done

The refactor is complete when **all** hold:

- [ ] No file under `ma_poc/scripts/` exceeds 200 lines.
- [ ] No file anywhere under `ma_poc/` exceeds 400 lines.
- [ ] `scripts/entrata.py` is deleted.
- [ ] `scripts/run_phase_a.py`, `extraction/{pipeline,tier1_api…tier5_vision,vision_banner,vision_sample,heuristics,confidence}.py`, and `templates/{rentcafe,entrata,appfolio}.py` are deleted.
- [ ] `daily_runner.run_daily()` and `jugnu_runner.run_jugnu()` are
      ≤ 30 lines each, and `retry_runner.run_retry()` ≤ 50 lines.
- [ ] `pms/adapters/generic.py` (file) replaced by `pms/adapters/generic/` (package); facade ≤ 200 lines.
- [ ] `services/llm_extractor.py` (file) replaced by `services/llm/` package; facade ≤ 150 lines.
- [ ] `scripts/sync_run_to_pg.py` (file) replaced by `scripts/sync_run_to_pg/` package; retention contract test green.
- [ ] `scripts/health_report.py` deleted; replaced by `reporting/health.py`.
- [ ] `tests/pms/adapters/` exists with per-tier tests; total
      `pytest .` count is ≥ 250.
- [ ] `mypy ma_poc/ --strict` clean.
- [ ] `ruff check ma_poc/` clean.
- [ ] `lint-imports` clean and contract is enforce-mode.
- [ ] `scripts/smoke_test.py` passes 5/5.
- [ ] 50-property semantic-diff vs pre-PR-0 baseline empty.
- [ ] `scripts/validate_outputs.py` shows no regression on the 10
      CLAUDE.md metrics.
- [ ] All 7 risks above have a passing test guarding them.
- [ ] Documentation updates in §7 are merged.
