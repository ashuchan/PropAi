# Integration Test Plan — Scraper Architecture

**Author:** Senior QA review
**Date:** 2026-05-10
**Scope:** Build a layer-aligned integration test suite for the scraper pipeline:
**extract → propagate → persist → consume**. Establish reusable fakes, factories,
and a corpus so future tests are cheap to write and obvious to read.

This plan does **not** replace existing unit tests in
[`tests/`](../tests/) or the PR/phase-named files in
[`tests/integration/`](../tests/integration/). It adds a parallel,
better-organised suite. Legacy files migrate opportunistically as they break.

---

## 0. Why this is needed

Inventory of the current state:

| Area | State |
|---|---|
| [`tests/integration/`](../tests/integration/) (~25 files) | Named after PRs/phases (`test_pr24_h13_units_preservation`, `test_phase_z_wiring_audit`). Opaque from the filename. Coverage skews to merge logic; thin elsewhere. |
| [`tests/conftest.py`](../tests/conftest.py) | Loads 5 HTML fixtures and patches `sys.path`. **No** layer-level fixtures (no fake fetcher, no fake LLM, no temp data-provider, no run-directory factory). |
| [`tests/fixtures/`](../tests/fixtures/) | 5 HTML samples + 1 API JSON. Tiny and stale. |
| Mocking style | Inconsistent. [`test_pr25_e2e_cross_source.py:11-17`](../tests/integration/test_pr25_e2e_cross_source.py#L11-L17) injects `sys.modules["playwright"]` ad-hoc; other tests stub adapters directly. No shared protocol. |
| Markers | Only `llm`. Nothing for `slow`, `db`, `playwright`, `e2e`, `ts`. |
| Layer seam coverage | Extract→Propagate is over-tested in places. **Persist (DataProvider / dual-write / Postgres) and Consume (reporting, `validate_outputs`, TS API) are barely exercised in integration.** |

Goal: make it impossible to ship a regression at a layer seam without a
red test, and trivial for a future contributor to find or add the right
test.

---

## 1. Mocking philosophy

The rule: **mock at process / network boundaries; keep all in-process logic
real.** Anything else makes integration tests degenerate into
mock-verifying tautologies.

| Component | Strategy | Rationale |
|---|---|---|
| [`fetch.fetcher.Fetcher`](../fetch/fetcher.py) | **Fake** via protocol-conforming class returning canned `FetchResult`s | Real Playwright/proxy is non-deterministic, slow, network-dependent |
| LLM clients ([`llm_extractor`](../services/llm_extractor.py), [`llm_api_rescue`](../services/llm_api_rescue.py), [`vision_extractor`](../services/vision_extractor.py)) | **Fake** at the SDK boundary (Azure / Anthropic clients). Keep [`llm_prompt`](../services/llm_prompt.py) and [`llm_gate`](../services/llm_gate.py) real. | Costs money, non-deterministic. Prompt-shaping logic is in-process and must stay real. |
| Browser (`playwright`, `patchright`) | **Fake** with `sys.modules` injection consolidated in one fixture | We don't run real Chromium in CI |
| `DataProvider` (filesystem) | **Real** [`FileSystemDataProvider`](../data_provider/filesystem.py) against `tmp_path` | Pure file logic; faking would defeat the test |
| `DataProvider` (sqlite) | **Real** [`SqliteDataProvider`](../data_provider/sqlite.py) against `:memory:` or `tmp_path` | Cheap real-DB stand-in for SQL paths |
| `DataProvider` (postgres) | **Real** under `@pytest.mark.db`, opt-in via `POSTGRES_TEST_DSN` env var | We must verify the actual driver / dialect at least once |
| `DualWriteDataProvider` | **Real** wrapping two real providers (FS + SQLite) | This is exactly what the dual-write contract needs to verify |
| [`ProfileStore`](../services/profile_store.py), [`scripts/state_store.py`](../scripts/state_store.py) | **Real** against `tmp_path` | Pure file logic, fast |
| [`observability.events.emit`](../observability/events.py) | **Real**, captured via spy fixture | Events are an observable contract |
| [`pms.detector`](../pms/detector.py), [`pms.resolver`](../pms/resolver.py), `pms.adapters.*` | **Real** | This *is* the system under test |
| [`source_planner`](../services/source_planner.py), [`source_merger`](../services/source_merger.py), [`profile_router`](../services/profile_router.py), [`profile_updater`](../services/profile_updater.py) | **Real** | This *is* the system under test |
| [`reporting/*`](../reporting/) | **Real** | This *is* the system under test |

---

## 2. Package layout

```
ma_poc/tests/integration/
├── __init__.py
├── conftest.py                          # Layer-level fixtures (see §3)
├── fakes/
│   ├── __init__.py
│   ├── fake_fetcher.py                  # Fetcher protocol stub: scripted FetchResult sequences
│   ├── fake_llm.py                      # Azure / Anthropic SDK doubles; canned JSON responses
│   ├── fake_vision.py                   # VisionProvider impl returning fixed unit lists
│   ├── fake_browser.py                  # Single-source playwright sys.modules patch
│   └── event_spy.py                     # Captures observability.events.emit calls
├── factories/
│   ├── __init__.py
│   ├── fetch_result.py                  # build_fetch_result(html=..., api=..., outcome=...)
│   ├── scrape_profile.py                # build_profile(maturity=..., hints=...)
│   ├── unit_record.py                   # build_unit(...)
│   └── run_directory.py                 # build_run_dir(tmp_path, properties=[...])
├── corpus/                              # Realistic, versioned response bodies
│   ├── rentcafe/{listing.html, api_units.json, sitemap.xml}
│   ├── entrata/...
│   ├── appfolio/...
│   ├── onesite/...
│   ├── sightmap/...
│   └── README.md                        # How to add a new corpus entry
│
├── extract/                             # Layer 1: extract integration
│   ├── test_extract_detect_then_adapter_dispatch.py
│   ├── test_extract_generic_5tier_cascade.py
│   ├── test_extract_api_interception_then_jsonld_fallback.py
│   ├── test_extract_template_fail_triggers_llm.py
│   ├── test_extract_llm_low_confidence_triggers_vision.py
│   ├── test_extract_per_pms_happy_path_rentcafe.py
│   ├── test_extract_per_pms_happy_path_entrata.py
│   ├── test_extract_per_pms_happy_path_appfolio.py
│   └── test_extract_link_hop_navigation_memory.py
│
├── propagate/                           # Layer 2: extract → propagate seam
│   ├── test_propagate_profile_learns_preferred_tier.py
│   ├── test_propagate_cold_to_warm_to_hot_3run_loop.py
│   ├── test_propagate_field_patches_persist_across_runs.py
│   ├── test_propagate_blocked_endpoint_demotes_tier.py
│   ├── test_propagate_cross_source_merge_by_unit_identity.py
│   ├── test_propagate_diff_calculator_rent_change_detected.py
│   └── test_propagate_diff_calculator_unit_disappeared_then_reappeared.py
│
├── persist/                             # Layer 3: propagate → persist seam
│   ├── test_persist_filesystem_run_artifact_roundtrip.py
│   ├── test_persist_sqlite_run_artifact_roundtrip.py
│   ├── test_persist_dual_write_primary_secondary_consistency.py
│   ├── test_persist_dual_write_secondary_failure_does_not_break_reads.py
│   ├── test_persist_retention_3day_rolling_window.py
│   ├── test_persist_upsert_idempotency_property_and_unit.py
│   ├── test_persist_transaction_atomicity_across_stores.py
│   ├── test_persist_schema_v2_column_translation_at_provider_boundary.py
│   └── test_persist_postgres_smoke.py        # @pytest.mark.db, opt-in
│
├── consume/                             # Layer 4: persist → consume seam
│   ├── test_consume_run_report_summarises_events_jsonl.py
│   ├── test_consume_validate_outputs_metrics_against_known_run.py
│   ├── test_consume_property_report_renders_from_provider.py
│   ├── test_consume_observation_report_history_query.py
│   └── test_consume_ts_data_provider_parity.py   # @pytest.mark.ts
│
└── e2e/                                 # End-to-end through all 4 layers
    ├── test_e2e_5_property_smoke_filesystem.py
    ├── test_e2e_5_property_smoke_sqlite.py
    └── test_e2e_skipped_property_carryforward.py
```

**Naming convention:** `test_{layer}_{behavior_in_imperative}.py`. Anyone
scanning `ls` should know what each file proves without opening it. Old
PR-named files stay where they are; new work follows this scheme.

---

## 3. Shared fixtures (`tests/integration/conftest.py`)

Sketch — exact signatures finalised in P1:

```python
@pytest.fixture
def fake_fetcher() -> FakeFetcher: ...
    # Usage:  fake_fetcher.responses_for(url, [FetchResult(...), ...])
    # Records every call so tests can assert on attempt order, retry, etc.

@pytest.fixture
def fake_llm() -> FakeLLM: ...
    # Usage:  fake_llm.returns(json_blob); fake_llm.raises(429); fake_llm.recorded_prompts

@pytest.fixture
def fake_vision() -> FakeVisionProvider: ...

@pytest.fixture(autouse=True)
def patch_playwright_modules(): ...
    # One place owns the sys.modules["playwright"] stub so individual tests don't reinvent it.

@pytest.fixture
def fs_provider(tmp_path) -> FileSystemDataProvider: ...

@pytest.fixture
def sqlite_provider(tmp_path) -> SqliteDataProvider: ...

@pytest.fixture
def dual_provider(fs_provider, sqlite_provider) -> DualWriteDataProvider: ...

@pytest.fixture
def event_spy() -> EventSpy: ...
    # monkeypatches observability.events.emit; .calls is a list of (kind, payload).

@pytest.fixture
def run_dir(tmp_path) -> RunDirectoryFactory: ...
    # run_dir.build(date="2026-05-10", properties=[...]) -> Path

@pytest.fixture
def profile_factory(tmp_path) -> ScrapeProfileFactory: ...

@pytest.fixture
def corpus() -> CorpusLoader: ...
    # corpus.load("rentcafe/listing.html") -> str
    # corpus.load_json("rentcafe/api_units.json") -> dict
```

### Markers — add to `pyproject.toml`

```toml
markers = [
    "llm: requires a live LLM provider (skip with -m 'not llm')",
    "db: requires a real Postgres (skipped without POSTGRES_TEST_DSN)",
    "ts: requires a Node toolchain",
    "slow: > 2s wall-clock — excluded from default CI run",
    "e2e: full pipeline through all 4 layers",
]
```

CI matrix: default run uses `-m "not llm and not db and not ts and not slow"`.
Nightly job runs the lot.

---

## 4. Test-by-test coverage targets

The point of each test, one line each. This list *is* the acceptance
criteria — every box ticked when its file exists, passes locally, and
fails when the contract it asserts is broken.

### Extract layer

| File | Asserts |
|---|---|
| `test_extract_detect_then_adapter_dispatch.py` | URL/HTML signals → correct PMS adapter chosen via `pms.detector` + `pms.adapters.registry` |
| `test_extract_generic_5tier_cascade.py` | API → JSON-LD → DOM → LLM → Vision order; first ≥0.7 confidence wins |
| `test_extract_api_interception_then_jsonld_fallback.py` | When intercepted API yields no usable units, JSON-LD picks up the property |
| `test_extract_template_fail_triggers_llm.py` | Tier-3 selectors all fail → Tier-4 LLM is invoked exactly once |
| `test_extract_llm_low_confidence_triggers_vision.py` | LLM confidence < 0.6 → Vision fallback runs |
| `test_extract_per_pms_happy_path_rentcafe.py` | RentCafe corpus → ≥1 `UnitRecord` with required fields |
| `test_extract_per_pms_happy_path_entrata.py` | Entrata corpus, ditto |
| `test_extract_per_pms_happy_path_appfolio.py` | AppFolio corpus, ditto |
| `test_extract_link_hop_navigation_memory.py` | BFS link-hop visits floorplan sub-pages; no infinite loop; navigation memory persisted on profile |

### Propagate layer

| File | Asserts |
|---|---|
| `test_propagate_profile_learns_preferred_tier.py` | After a tier-3 success, `profile.preferred_tier == 3` and is consulted on the next run |
| `test_propagate_cold_to_warm_to_hot_3run_loop.py` | 3 runs flip maturity COLD → WARM → HOT; HOT skips the cascade |
| `test_propagate_field_patches_persist_across_runs.py` | LLM-discovered field mapping is reused on run 2 without re-calling the LLM |
| `test_propagate_blocked_endpoint_demotes_tier.py` | A 403/CAPTCHA on the preferred endpoint demotes preferred_tier and records `blocked_endpoints` |
| `test_propagate_cross_source_merge_by_unit_identity.py` | Replay (physical fields) + cascade narrow parser (transactional fields) merge into one `UnitRecord` per identity |
| `test_propagate_diff_calculator_rent_change_detected.py` | Rent delta vs prior run produces a `DailyDiff` row tagged "updated" |
| `test_propagate_diff_calculator_unit_disappeared_then_reappeared.py` | Disappearance + reappearance within window → `availability_periods` updated, no spurious "new" |

### Persist layer

| File | Asserts |
|---|---|
| `test_persist_filesystem_run_artifact_roundtrip.py` | `properties.json` / `units.json` / `issues.jsonl` written by `daily_runner` and re-read identically |
| `test_persist_sqlite_run_artifact_roundtrip.py` | Same artefacts via SQLite provider; row counts match |
| `test_persist_dual_write_primary_secondary_consistency.py` | Both providers see the same data after writes; reads come from primary |
| `test_persist_dual_write_secondary_failure_does_not_break_reads.py` | Inject `IOError` on secondary writes; reads from primary still succeed; failure is logged |
| `test_persist_retention_3day_rolling_window.py` | `_apply_retention()` deletes rows older than 3 days for `scrape_events`, `extraction_results`, etc. (per memory `project_postgres_retention_policy.md`) |
| `test_persist_upsert_idempotency_property_and_unit.py` | Re-running the same run is a no-op; no duplicates, no broken FKs |
| `test_persist_transaction_atomicity_across_stores.py` | Failure mid-transaction rolls back property + unit + event writes together |
| `test_persist_schema_v2_column_translation_at_provider_boundary.py` | V1 file-key inputs are translated to V2 column names at the FS provider boundary (per memory `project_db_v2_schema.md`) |
| `test_persist_postgres_smoke.py` | `@pytest.mark.db` — same roundtrip against a real Postgres |

### Consume layer

| File | Asserts |
|---|---|
| `test_consume_run_report_summarises_events_jsonl.py` | `reporting/run_report.py` produces correct bot-block / CAPTCHA tallies from a known events.jsonl |
| `test_consume_validate_outputs_metrics_against_known_run.py` | All 10 `validate_outputs` metrics computed correctly on a fixture run dir |
| `test_consume_property_report_renders_from_provider.py` | `reporting/property_report.py` reads via `DataProvider` and renders without I/O surprises |
| `test_consume_observation_report_history_query.py` | Per-field observation history query returns rows in the expected schema |
| `test_consume_ts_data_provider_parity.py` | TS `services/src/data-provider/` reads the same FS run dir as Python (smoke; `@pytest.mark.ts`) |

### E2E

| File | Asserts |
|---|---|
| `test_e2e_5_property_smoke_filesystem.py` | 5-property pipeline: fake_fetcher → adapters → profile → FS provider → reports. All 7 assertions from `scripts/smoke_test.py`. |
| `test_e2e_5_property_smoke_sqlite.py` | Same, via SQLite |
| `test_e2e_skipped_property_carryforward.py` | Change-detection SKIPPED → carryforward_days increments; report reflects it |

---

## 5. Phased rollout

Each phase is one PR. No phase touches more than one layer.

| Phase | Deliverable | Exit criterion |
|---|---|---|
| **P1 — Scaffolding** (no new tests) | `conftest.py`, `fakes/`, `factories/`, empty `corpus/` with README, marker definitions | All existing tests still pass; `pytest --collect-only -m "not llm and not db"` clean |
| **P2 — Extract** | 8–10 tests under `extract/` | Each PMS adapter has ≥1 happy-path + ≥1 cascade-fallback test, all using `fake_fetcher` + corpus |
| **P3 — Propagate** | 6–8 tests under `propagate/` | Profile maturity transitions covered; 3-run cold→hot loop is deterministic |
| **P4 — Persist** | 8 tests under `persist/` | FS, SQLite, DualWrite contracts covered; retention + transactions verified; one opt-in `@db` test against real PG |
| **P5 — Consume** | 4–5 tests under `consume/` | `run_report` + `validate_outputs` verified against a fixture run dir |
| **P6 — E2E smoke** | 3 tests under `e2e/`, plus rewire [`scripts/smoke_test.py`](../scripts/smoke_test.py) to use the same fixtures | 5/5 deterministic, runs in <30s |
| **P7 — Migrate legacy** | Move/rename historical `test_pr*` and `test_phase*` files into the new layer dirs as they're touched in unrelated work | No new code references old filenames |

### Risks and tradeoffs

- **Fakes are an investment.** They only pay off if multiple tests use
  them. P1 is therefore deliberately *infrastructure only*. If P2 reveals
  the fake API is wrong, we revise it before writing 50 tests against the
  wrong shape.
- **Corpus drift.** Real PMS HTML changes. Corpus is versioned in-repo
  and updated only when a real failure exposes a gap; tests should not
  silently pass against ancient HTML. Each corpus dir gets a `SOURCE.md`
  noting capture date and source URL pattern.
- **`@pytest.mark.db` is opt-in.** It will not run in default CI. That's
  deliberate — the smoke suite catches contract drift; the marked test
  catches dialect drift, and we run it nightly.
- **Migration debt.** Old `test_pr*` files are not deleted on day one.
  They keep their coverage; we move them only when touching them anyway.
  Net new tests are not allowed in the old naming scheme.

---

## 6. Recommended first PR

P1 only. Single PR contents:

- `tests/integration/conftest.py` (the fixtures sketched in §3)
- `tests/integration/fakes/{fake_fetcher,fake_llm,fake_vision,fake_browser,event_spy}.py`
- `tests/integration/factories/{fetch_result,scrape_profile,unit_record,run_directory}.py`
- `tests/integration/corpus/README.md`
- Marker additions in [`pyproject.toml`](../pyproject.toml)
- **No new test files.** Existing suite must remain green.

That keeps the review tractable and lets us walk back any abstraction
that turns out wrong before we've written 50 tests against it.
