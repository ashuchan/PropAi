# `ma_poc/` — SOLID Refactor Plan

**Author:** Senior architecture review
**Date:** 2026-05-07
**Scope:** Decompose the bulky orchestrators and god-modules under `ma_poc/`
into bounded, single-responsibility modules ready for distributed execution.
**Companion email sender:** [`ma_poc/scripts/email_refactor_plan.py`](../scripts/email_refactor_plan.py)

---

## Executive summary

`ma_poc/` carries roughly **12,240 lines across seven god-modules** that
mix orchestration, extraction, persistence, LLM dispatch, reporting, and
profile learning. Two parallel pipelines (legacy `daily_runner.py` /
`entrata.py` and the newer Jugnu `jugnu_runner.py`) duplicate host-specific
parsers, identity resolution, and rent normalization across three places
each.

This plan carves the codebase into **8 bounded contexts** behind stable
protocols, delivered in **8 incremental PRs** — no big-bang rewrite.
Targets: every refactored module ≤ 400 lines and ≤ 3 public methods,
top-7 line count down ~70 % via deduplication, and concrete unblockers
for stateless workers, shard-safe upserts, and adaptive backpressure.

**Headline KPIs**

| Metric | Today | Target |
|---|---:|---:|
| God-modules > 800 lines | 7 | 0 |
| Top-7 module total | ~12,240 | ~3,500 |
| Largest single file | 3,156 (`entrata.py`) | < 300 |
| `daily_runner.run_daily()` | 827 lines | < 30 |
| `GenericAdapter` LOC | 2,145 | < 400 (orchestrator only) |
| Bounded contexts | mixed | 8 with stable protocols |
| Parallel pipelines | 2 (sharing nothing) | 2 (sharing 5 contexts) |

---

## 1. Diagnosis — where the bulk lives

Top-7 by line count, with the SOLID violation that drives each split.
Line numbers are from the current `main` branch (2026-05-06).

| Module | Lines | SOLID violation | Smell |
|---|---:|---|---|
| `scripts/entrata.py` | 3,156 | **SRP** — god function | `scrape()` spans ~700 lines, 7 phases inlined, 100+ state vars, 4 nested LLM fallbacks. |
| `pms/adapters/generic.py` | 2,145 | **SRP / DIP** — god class | `GenericAdapter._extract_inner()` has 7 sub-tiers (0–6c) as inline conditionals. LLM, prompt strings, and merge rules baked in. |
| `scripts/jugnu_runner.py` | 1,608 | **SRP** — monolithic orchestrator | `run_jugnu()` wires 5 layers + CSV + format routing + report. `_process_property()` alone is 270 lines. |
| `scripts/daily_runner.py` | 1,410 | **SRP** — pipeline-as-function | `run_daily()` is 827 lines: CSV → identity → dedup → state load → concurrent scrape → diff → carry-fwd → record build → report → state write. |
| `scripts/retry_runner.py` | 1,097 | **DIP** — coupling via re-exports | Imports 6 internals from `daily_runner` (`_scrape_in_thread`, `TARGET_PROPERTY_FIELDS`, …). 700-line `run_retry()` mirrors `run_daily()`. |
| `scripts/scrape_properties.py` | 959 | **OCP** — host parsers scattered | Four `_*_units_from_body()` functions (SightMap / RealPage / AvalonBay / generic) — no common interface. Same logic re-implemented in `entrata.py`. |
| `services/llm_extractor.py` | 865 | **ISP** — fat interface | Three modes (`extract_with_llm`, `analyze_api_with_llm`, `analyze_dom_with_llm`) on one module surface. Prompts hardcoded. Provider not injectable. |

### Cross-module duplication observed

| Logic | Duplicated in |
|---|---|
| Generic API key-name variants (50+ unit/rent/sqft keys) | `entrata.py::parse_api_responses`, `generic.py::parse_generic_api`, `scrape_properties.py::_generic_units_from_body` |
| Rent extraction from nested `{min, max}` / list `[{rent, term}]` | `entrata.py::_extract_rent`, `scrape_properties.py::_extract_rent`, `generic.py` (inline) |
| SightMap / RealPage / AvalonBay payload parsing | `scrape_properties.py`, `entrata.py` (host-specific paths inside `parse_api_responses`) |
| Identity resolution heuristics | `identity.py`, `jugnu_runner._SimpleProfileStore`, fallback shims in `generic.py` |
| Carry-forward decision | `daily_runner.run_daily`, `retry_runner.run_retry`, `discovery/carry_forward.py` |

---

## 2. Target architecture — 8 bounded contexts

Each context has **one reason to change** and a stable public interface.
Internal classes stay private. No context imports siblings' internals.

### 2.1 `orchestration/` — pipeline composition only

```
ma_poc/orchestration/
├── pipeline.py              # generic Pipeline[T] with Stage protocol
├── stages/
│   ├── csv_load.py          # Stage: CSV → list[PropertyRow]
│   ├── identity.py          # Stage: PropertyRow → ResolvedProperty
│   ├── dedup.py             # Stage: detects hard/soft/geo duplicates
│   ├── scrape.py            # Stage: dispatches concurrent scraping
│   ├── state_sync.py        # Stage: diff + carry-forward + persist
│   └── report.py            # Stage: write JSON + Markdown + SLO
├── runners/
│   ├── daily.py             # DailyRunner — wires DailyPipeline (~120 lines)
│   ├── jugnu.py             # JugnuRunner — wires JugnuPipeline (~150 lines)
│   └── retry.py             # RetryRunner — DailyPipeline + row-filter strategy
├── concurrency_manager.py   # owns AsyncPool / ThreadedPool from concurrency.py
└── cli.py                   # argparse, isolated from business logic
```

### 2.2 `extraction/engine/` — replaces `scripts/entrata.py`

```
ma_poc/extraction/engine/
├── browser_session.py       # Playwright lifecycle, context-per-property,
│                            # finally-close (closes Bug-Hunt #1)
├── network_capture.py       # XHR/fetch interception, instance-scoped
│                            # buffer (closes Bug-Hunt #2)
├── noise_filter.py          # _FALSE_POSITIVE_HOSTS + _PATH_FRAGMENTS +
│                            # profile blocklist composition
├── link_explorer.py         # prioritization + bounded BFS (MAX_CRAWL_PAGES)
├── metadata_extractor.py    # name/address/phone/geo from og:* + JSON-LD + footer
├── phases/
│   ├── _base.py             # PhaseStage protocol + PhaseContext dataclass
│   ├── phase1_homepage.py   # homepage load + full network capture
│   ├── phase2_filter.py     # noise filter + link prioritization
│   ├── phase3_known.py      # profile mappings → API → JSON-LD → DOM
│   ├── phase4_explore.py    # link-by-link with per-page network observation
│   ├── phase5_llm_api.py    # targeted LLM API analysis (max 3/property)
│   ├── phase6_dom.py        # targeted DOM LLM → legacy LLM → vision
│   └── phase7_finalize.py   # availability defaults + profile learning
└── scraper.py               # Scraper facade composes phases via PhaseRegistry
```

### 2.3 `pms/adapters/generic/` — split `generic.py`

```
ma_poc/pms/adapters/generic/
├── __init__.py              # exports GenericAdapter (thin facade)
├── tiers/
│   ├── _base.py             # Tier protocol + TierResult
│   ├── tier0_blocked.py     # blocked_filter + profile_replay
│   ├── tier1_api_narrow.py  # parse_generic_api on unit-signal responses
│   ├── tier2_api_broad.py   # broad parser + SightMap / RealPage hosts
│   ├── tier3_jsonld.py      # JSON-LD with plan-name-only rejection
│   ├── tier4_embedded.py    # __NEXT_DATA__ / __NUXT__ / SSR globals
│   ├── tier5_dom.py         # CSS-selector cascade with junk filters
│   ├── tier6a_llm_api.py    # LLM API analysis (3-call budget)
│   ├── tier6b_llm_dom.py    # LLM DOM section analysis (1-call budget)
│   └── tier6c_llm_mono.py   # monolithic fallback (1-call budget)
├── tier_orchestrator.py     # sequencer; injectable assess_and_decide strategy
├── merger.py                # the current _merge_field_values() as a rule chain
├── prompt_loader.py         # loads from config/prompts/ — no inline strings
└── gates.py                 # skip_llm / LLM_GATE_RELAXED policy
```

### 2.4 `extraction/parsers/` — consolidate host-specific parsers

```
ma_poc/extraction/parsers/
├── _base.py                 # UnitParser protocol
├── sightmap.py              # joins units to floor plans
├── realpage.py              # /floorplans + /units split-endpoint
├── avalon.py                # AvalonBay format
├── onesite.py
├── entrata_widget.py
├── generic_api.py           # 50+ key-name variants — single home for them
└── parser_registry.py       # host fingerprint → parser
```

**OCP win:** new PMS = drop a new file + register it. No edits to the
extraction engine.

### 2.5 `services/llm/` — explode `llm_extractor.py`

```
ma_poc/services/llm/
├── _protocols.py            # MonolithicExtractor / ApiAnalyzer / DomAnalyzer
├── prompts.py               # templates loaded from config/prompts/
├── monolithic_extractor.py  # extract_with_llm()
├── api_analyzer.py          # analyze_api_with_llm()
├── dom_analyzer.py          # analyze_dom_with_llm()
├── mapping_replay.py        # apply_saved_mapping() — zero-cost path
├── field_normalizer.py      # config-driven field-name mapping
├── retry_policy.py          # tenacity wrapper with random.uniform jitter
└── factory.py               # provider selection (Anthropic | Azure | …)
```

**ISP win:** `GenericAdapter` depends on `ApiAnalyzer` only; the resolver
depends on `MonolithicExtractor` only. No fat interface.

### 2.6 `state/` — pull state work out of `daily_runner.py`

```
ma_poc/state/
├── _protocols.py            # StateStore / ProfileStore protocols
├── csv_loader.py            # UTF-8-BOM read + flexible column inference
├── identity_resolver.py     # 5-tier cascade as chain of Resolver impls
├── dedup_detector.py        # hard / soft / geo dupes
├── record_builder.py        # the current build_property_record
├── state_diff.py            # new / updated / unchanged / disappeared
├── carry_forward.py         # failed-scrape replay rules (single source)
└── stores/
    ├── filesystem.py        # JSON file impl (current default)
    └── postgres.py          # SQL impl (kept thin; stays compatible with
                             # sync_run_to_pg.py 3-day retention contract)
```

### 2.7 `reporting/` — consolidate the `*_report.py` family

Move `scripts/scrape_report.py`, `scripts/generate_daily_report.py`,
`scripts/health_report.py` into `reporting/`.

```
ma_poc/reporting/
├── aggregator.py            # tracks stats during the main loop, not as
│                            # a second pass over properties
├── markdown_writer.py
├── json_writer.py
├── verdict.py               # already exists — keep
├── slo_section.py
└── run_report.py            # already exists — keep, slim down
```

### 2.8 `scripts/` — collapse to thin entrypoints

Each runner becomes < 150 lines: load env, parse args, build a
`Pipeline`, run, exit. `entrata.py` retired (deprecated wrapper for one
release calling new `Scraper`). `retry_runner.py` becomes a
`RetryPipeline` that extends `DailyPipeline` with a row-filter strategy
— eliminates the 6-symbol re-export coupling.

---

## 3. SOLID — what each principle buys us

| Principle | Today's pain | After refactor |
|---|---|---|
| **SRP** Single Responsibility | `scrape()` 700 lines / 7 phases. `run_daily()` 827 lines / 12 concerns. | Every module ≤ 400 lines, ≤ 3 public methods. Each phase, parser, and stage is independently testable. |
| **OCP** Open/Closed | New PMS = edit `CONTAINER_SELECTORS`, `_UNIT_ID_KEYS`, `ENTRATA_API_PATTERNS` inside `entrata.py`. | New PMS = drop `parsers/<host>.py`, register in `parser_registry.py`. No edits to extraction engine. |
| **LSP** Liskov Substitution | No interfaces today. Adapters carry adapter-specific debug fields in result dicts (`_winning_url`, `_llm_interactions`). | All `UnitParser`, `PhaseStage`, `Tier`, `LLMExtractor` impls substitutable behind their protocol. Debug data on `PhaseContext`, not the result. |
| **ISP** Interface Segregation | `llm_extractor.py` exposes 3 unrelated modes; callers depend on the whole module. | Three narrow protocols: `MonolithicExtractor`, `ApiAnalyzer`, `DomAnalyzer`. Callers depend only on what they use. |
| **DIP** Dependency Inversion | Profiles read from `config/profiles/` directly inside `scrape()`. LLM provider imported at module top. | Orchestrators depend on `ProfileStore`, `StateStore`, `LLMExtractor`, `ProxyPool` protocols. Concretes injected at the runner. Swap to Postgres / Redis / cloud providers without touching the engine. |

---

## 4. Migration plan — 8 incremental PRs, no big bang

Each PR is shippable in isolation. Each ends with the CLAUDE.md gates:
full `pytest .` green, `mypy --strict` + `ruff check` clean,
`scripts/smoke_test.py` passing 5/5, and a 50-property golden-run diff
against the prior baseline output.

| PR | Scope | Risk | Behavioural-parity check |
|---|---|:---:|---|
| **PR-1** | Extract `BrowserSession` + `NetworkCapture` + `NoiseFilter` from `entrata.py`. No behavioural change — only structural. | Low | Byte-equal `properties.json` on 50-row golden run. |
| **PR-2** | Extract `Phase1HomepageLoad` and `Phase2NoiseFilter` as classes. Wire through new `PhaseRegistry`. | Med | Per-property unit count parity; tier distribution parity (PR-03 AC in CLAUDE.md). |
| **PR-3** | Extract Phases 3–7. `scrape()` becomes a 15-line orchestration of phase classes. Retire `entrata.py` as a deprecated wrapper. | High | 50-property golden diff + Vision banner-capture rate ≥ 95 % (PR-04 AC). |
| **PR-4** | Split `generic.py` into `tiers/` + `tier_orchestrator.py`. Move prompt strings to `config/prompts/`. | Med | LLM-call count per property unchanged ± 5 %. `cost_ledger.db` spend within ± $0.05 per 50-property run. |
| **PR-5** | Consolidate host-specific parsers under `extraction/parsers/` behind `UnitParser` protocol. Remove duplicate copies in `scrape_properties.py` and `entrata.py`. | Med | SightMap + RealPage + AvalonBay unit counts per property unchanged. |
| **PR-6** | Refactor `llm_extractor.py` into `services/llm/`. Three narrow protocols, externalized prompts, retry-policy module. Provider injected via factory. | Med | Profile-replay determinism: pre/post-PR `apply_saved_mapping()` output byte-equal on 100 saved mappings. |
| **PR-7** | Decompose `daily_runner.py` into `state/` + `orchestration/` + `reporting/`. `run_daily()` becomes a 30-line `DailyPipeline.execute()`. | High | `property_index.json` + `unit_index.json` structural diff = empty after a full run. |
| **PR-8** | Collapse `retry_runner.py` into a `RetryPipeline` = `DailyPipeline` + row-filter strategy. Drop the 6-symbol re-export coupling. | Low | Resume + retry-errors modes produce identical ledger merges as today on a synthetic run with seeded failures. |

### Per-PR checklist (apply to every PR)

1. `pytest . -v --ignore=data --ignore=config` exits 0.
2. `ruff check ma_poc/` exits clean (no E/F findings).
3. `mypy ma_poc/ --strict` exits clean.
4. `python ma_poc/scripts/smoke_test.py` passes 5/5.
5. Golden-run diff: `python ma_poc/scripts/jugnu_runner.py --csv config/properties.csv --limit 50` produces `properties.json` byte-equal (or behaviourally-equal per the table) to the pre-PR baseline.
6. `python ma_poc/scripts/validate_outputs.py` shows no regression on the 10 metrics in CLAUDE.md.
7. Bug-Hunt items relevant to the touched code path re-checked.

---

## 5. Metrics — before and after

| Module | Before | After (target) | Strategy |
|---|---:|---:|---|
| `scripts/entrata.py` | 3,156 | retired | Split into ~12 modules < 300 each under `extraction/engine/`. |
| `pms/adapters/generic.py` | 2,145 | < 400 | Orchestrator only; 7 tier modules < 300 each. |
| `scripts/jugnu_runner.py` | 1,608 | < 200 | Entrypoint only; pipeline + stages < 250 each. |
| `scripts/daily_runner.py` | 1,410 | < 200 | Entrypoint only; 5 stage modules < 300 each in `state/`. |
| `scripts/retry_runner.py` | 1,097 | < 150 | Extends `DailyPipeline` with row-filter strategy. |
| `scripts/scrape_properties.py` | 959 | < 200 | Host parsers move to `extraction/parsers/`; this becomes a thin batch entrypoint. |
| `services/llm_extractor.py` | 865 | < 150 | Facade only; 4 sub-modules < 250 each in `services/llm/`. |
| **Top-7 total** | **~12,240** | **~3,500** | ~70 % reduction via deduplication (host parsers, rent extraction, identity resolution duplicated today across 3 modules). |

---

## 6. Distributed-systems concerns this refactor unblocks

Carving these seams is not just hygiene — it removes specific blockers
for running `ma_poc` on distributed workers.

1. **Stateless scrape units.** Today `entrata.py` reads/writes
   `config/profiles/{canonical_id}.json` directly. With `ProfileStore`
   behind a protocol, scrapes can run on Cloud Run / K8s jobs against a
   Postgres- or Redis-backed profile store — no shared filesystem
   required.
2. **Shard-safe upserts.** The memory note
   `project_postgres_retention_policy` records that today the same
   property scraped by two shards causes pass-2 to flip pass-1's units
   to `disappeared_since` immediately. Pulling state into `state/`
   behind a transactional `StateStore` interface makes
   `(canonical_id, run_date)` tracking a first-class concern.
3. **Adaptive backpressure.** `ConcurrencyManager` isolates pool sizing.
   Today `MAX_CONCURRENT_BROWSERS` is static; with the manager owning
   rate-limiter feedback, we can throttle on proxy 429s without
   touching `scrape()`.
4. **Replayable extraction.** Phase-as-class lets us run a single phase
   against cached `data/raw_html/` + `raw_api/` snapshots — closes the
   debugging loop for accuracy regressions without re-fetching.
5. **Multi-tenant safety.** Once `ProfileStore` and `StateStore` are
   protocols, per-tenant namespacing is a constructor argument, not a
   code change.
6. **Provider hot-swap.** `LLMExtractor` protocol decouples adapter
   logic from Anthropic vs. Azure vs. (future) on-prem; cost-routing
   decisions move to a single dispatch module.

---

## 7. Risk register + mitigations

### R1 — Behavioural drift in `scrape()`

The 7-phase function has accumulated edge cases (HTTP→HTTPS
normalization, `MAX_CRAWL_PAGES = 10`, expander click swallows
exceptions, redirect capture for portal iframes, `_FALSE_POSITIVE_HOSTS`
list of 30+ domains, `_FALSE_POSITIVE_PATH_FRAGMENTS` of 15+ patterns).

**Mitigation:** snapshot a 50-property golden run before PR-1. After
each of PR-1 through PR-3, assert byte-equal `properties.json` against
that baseline. Any drift blocks merge.

### R2 — Profile-format compatibility

Per-property profiles in `config/profiles/` must deserialize cleanly
against new code. `ScrapeProfile` has many forward-compatible fields
(see `models/scrape_profile.py`); the refactor must not change the
on-disk schema.

**Mitigation:** add a version shim in `state/stores/filesystem.py`. Do
not change the on-disk schema in this refactor. Add a regression test
that loads 50 representative profiles from a saved fixture set.

### R3 — Test-coverage gap on `pms/adapters/generic.py`

Exploration shows only ~4 tests under `tests/pms/`. Splitting a
2,145-line class behind that thin a coverage net is dangerous.

**Mitigation:** before PR-4 ships, add a baseline integration test
asserting unit counts on 10 representative URLs. Each new tier module
in PR-4 must add unit tests for its own contract.

### R4 — Two pipelines stay separate

Memory says Jugnu is the recommended path, but `daily_runner.py` is
still in production use.

**Mitigation:** do **not** attempt a unification in this refactor.
Both runners consume the same `extraction/`, `state/`, `reporting/`
modules but remain distinct entrypoints. A future plan can collapse
them once both are riding the new contexts.

### R5 — Postgres retention contract

Memory `project_postgres_retention_policy` pins a 3-day rolling cap and
the v2-strict schema.

**Mitigation:** `StateStore` protocol must keep `_apply_retention()`
invariants intact. `sync_run_to_pg.py` stays untouched in PR-1 through
PR-7. PR-8 may revisit only if the row-filter strategy demands it.

### R6 — `_meta` debug-field contracts

Several downstream consumers (`reporting/run_report.py`,
`observability/slo_watcher.py`) read internal fields like
`_meta.verdict`, `_extract_result.tier_used`, `_winning_page_url`.
These are documented in CLAUDE.md as the source of truth.

**Mitigation:** no PR is allowed to change `_meta.*` or
`_extract_result.*` key names. Add a contract test that asserts the
expected key set is present on every output record.

---

## 8. Explicitly out of scope

- Merging legacy + Jugnu pipelines (separate, larger initiative).
- Changing the on-disk profile or state schemas.
- Migrating the `ma_poc/extraction/` Phase A BeautifulSoup stack
  (already inactive — leave as-is or archive separately).
- Frontend / TS API changes (different bounded context).
- Postgres schema migrations beyond what each PR's tests need.
- LLM-provider replacement (the refactor enables it; the swap itself
  is a separate change).
- Cross-property cluster learning (the `cluster_id` field on
  `ScrapeProfile` stays unimplemented).

---

## 9. Sequencing notes

- **Do PR-1 first, alone.** It is purely structural and validates the
  golden-run baseline harness. Without that harness, PR-2 and PR-3 are
  unsafe.
- **PR-4 and PR-5 are independent** of PR-1 through PR-3 and can ship
  in parallel if reviewer bandwidth allows.
- **PR-6 should land before PR-4** if possible — `services/llm/`
  protocols make the tier classes in PR-4 simpler to write.
- **PR-7 is the largest blast-radius PR.** Schedule it after a quiet
  release window. Roll back is a single `git revert`; the on-disk
  state schema does not change.
- **PR-8 closes the loop.** Until it lands, `retry_runner.py` keeps
  the 6-symbol coupling — that is acceptable as long as PR-7 has
  shipped, because `daily_runner.py` is then the thin entrypoint.

---

## 10. Definition of done

The refactor is complete when **all** of the following hold:

- [ ] No file under `ma_poc/scripts/` exceeds 200 lines.
- [ ] No file anywhere under `ma_poc/` exceeds 400 lines.
- [ ] `scripts/entrata.py` is retired (file deleted or 50-line
      deprecation shim).
- [ ] `daily_runner.run_daily()` and `jugnu_runner.run_jugnu()` are
      ≤ 30 lines each.
- [ ] `tests/extraction/engine/` exists with phase-level tests; total
      `pytest .` count is ≥ 250.
- [ ] `mypy ma_poc/ --strict` clean.
- [ ] `ruff check ma_poc/` clean.
- [ ] `scripts/smoke_test.py` passes 5/5.
- [ ] 50-property golden-run diff vs. pre-PR-1 baseline is empty (or
      every difference is documented and approved).
- [ ] `scripts/validate_outputs.py` shows no regression on the 10
      CLAUDE.md metrics.
- [ ] All 6 risks above have a passing test guarding them.
