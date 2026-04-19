# Jugnu — Master Implementation Guide for Claude Code

> Robust Crawler Architecture for MA Rent Intelligence — migration from the
> current 7-phase `entrata.py` pipeline to a clean 5-layer system.
>
> Reference: `Jugnu_Robust_Crawler_Architecture.docx` (v1.0, 2026-04-18).

This document is the **entry point**. Read it in full before opening any
phase-specific file. It defines what stays constant across all phases:
the target architecture, the non-negotiables, the shared contracts, the
workflow, and the ordering.

---

## 1. What Jugnu is

Jugnu reorganises the scraping pipeline into five horizontal layers with
hard contracts between them. The existing 7-phase extraction cascade
becomes **one concern inside one layer** (L3), not the whole system.

```
┌─────────────────────────────────────────────────────────────────┐
│  L5 — Observability & Control                                   │
│    event ledger · cost meter · replay · DLQ · SLO watchers      │
└─────────────────────────────────────────────────────────────────┘
                   ▲                     │
              events│                    │control
                   │                     ▼
┌─────────────────────────────────────────────────────────────────┐
│  L4 — Validation & Schema                                       │
│    Pydantic gate · identity fallback · cross-run sanity         │
└─────────────────────────────────────────────────────────────────┘
                            ▲
                            │ ExtractResult
┌─────────────────────────────────────────────────────────────────┐
│  L3 — Extraction  (the 7 phases, reorganised)                   │
│    PMS detector → adapter registry → generic cascade fallback   │
└─────────────────────────────────────────────────────────────────┘
                            ▲
                            │ FetchResult
┌─────────────────────────────────────────────────────────────────┐
│  L2 — Discovery & Scheduling                                    │
│    sitemap · frontier · priority queue · carry-forward · DLQ    │
└─────────────────────────────────────────────────────────────────┘
                            ▲
                            │ CrawlTask
┌─────────────────────────────────────────────────────────────────┐
│  L1 — Fetch & Fleet                                             │
│    retries · proxy pool · stealth · rate limit · conditional GET│
└─────────────────────────────────────────────────────────────────┘
                            ▲
                     [ public web ]
```

Each layer:

- owns its own retry / error semantics
- emits events to L5
- exposes exactly one contract object to the layer above it
- knows nothing about how layers above or below are implemented

---

## 2. Phase map and instruction files

There are **10 phases (J0–J9)**. Each phase has its own instruction file.
Do not start phase N+1 until phase N's gate is green.

| Phase | Instruction file | Deliverable | Dependencies |
|---|---|---|---|
| —  | `JUGNU_CONTRACTS.md` | Shared dataclasses (FetchResult, CrawlTask, ExtractResult, ValidatedRecords, Event) | None (read first) |
| J0 | `PHASE_J0_BASELINE.md` | `scripts/jugnu_baseline.py` + `docs/JUGNU_BASELINE.md` | — |
| J1 | `PHASE_J1_FETCH.md` | `ma_poc/fetch/` package | J0 |
| J2 | `PHASE_J2_DISCOVERY.md` | `ma_poc/discovery/` package | J1 |
| J3 | `PHASE_J3_EXTRACTION.md` | `ma_poc/pms/` package (bridges to existing `claude_refactor.md`) | J2 |
| J4 | `PHASE_J4_VALIDATION.md` | `ma_poc/validation/` package | J3 |
| J5 | `PHASE_J5_OBSERVABILITY.md` | `ma_poc/observability/` + `scripts/replay.py` | J1 (events plug into every layer) |
| J6 | `PHASE_J6_PROFILE_V2.md` | `ma_poc/models/scrape_profile.py` v2 + migration | J3 |
| J7 | `PHASE_J7_REPORT_V2.md` | `ma_poc/reporting/property_report.py` + `run_report.py` | J6 |
| J8 | `PHASE_J8_INTEGRATION.md` | Modified `scripts/daily_runner.py` | J7 |
| J9 | `PHASE_J9_GATE.md` | `scripts/gate_jugnu.py` + bug-hunt checklist | J8 |

### Why this split

- **One file per phase** — each fits the attention window without context
  thrashing. A monolithic `CLAUDE_JUGNU.md` would be 5,000+ lines and
  Claude Code would lose its place inside it.
- **Shared contracts in their own file** — every phase imports from
  `JUGNU_CONTRACTS.md`. Keeping that single source of truth avoids
  drift between phases when a dataclass field is added or renamed.
- **Explicit handoffs** — every phase file ends with an *Outbound handoff*
  section the next phase's file cross-references in its *Inbound handoff*
  section. This is the only way state survives a phase boundary.
- **J5 is plumbed into every layer** — J5 is implemented late but its
  event-emission hooks are added to L1–L4 as those layers are built.
  Every L1–L4 phase file lists the exact event names it emits, so J5's
  ledger consumer has a fixed schema to code against.

---

## 3. Non-negotiables (hold these across every phase)

These come from the Jugnu architecture doc §7.2 and are hard. The gate
script at J9 enforces them:

1. **Never-fail contract preserved.** No single property can crash the
   run. Every layer catches its own exceptions and emits a structured
   event instead.
2. **46-key output schema frozen.** Downstream consumers are unchanged.
   The keys in `scrape_properties.TARGET_PROPERTY_FIELDS` are final.
3. **State files backward-compatible.** Existing `property_index.json`
   and `unit_index.json` must load without error under the new code.
4. **LLM is the teacher, not the worker.** The median property makes 0
   LLM calls per scrape once its profile is warm. A healthy Jugnu run
   shows `$0` LLM spend for every detected PMS, with any LLM cost
   concentrated in the `unknown` bucket.
5. **Adapters own their quirks.** PMS-specific code lives only in its
   adapter module. No PMS string literals outside the adapter, the
   detector, or the resolver.
6. **Research before code.** Every adapter's top-of-file comment lists
   the real `raw_api/` captures and web sources consulted.
7. **No layer imports a higher layer.** L1 never imports from L2; L2
   never from L3; and so on. The only upward flow is events, which
   use `logging` or the L5 emit helper — never a direct module import.

---

## 4. The mandatory 7-step workflow (from the existing CLAUDE.md)

For each file, in order:

1. **Read requirements fully** — the phase file and its inbound handoff
   before writing any code.
2. **Implement fully** — complete module at one sitting. No half-files.
3. **Write tests immediately** — same commit as the implementation.
4. **Run tests** — `pytest tests/<area>/ -xvs`. Fix failures now.
5. **Static analysis** — `ruff check <file> && mypy --strict <file>`.
6. **Gate validation** — run `python scripts/gate_jugnu.py phase <N>`
   before moving on. Do not loosen the gate to make it pass; fix the
   code.
7. **Bug hunt** — at J9 only, walk the checklist module-by-module.

Never batch these steps across multiple files. One file at a time.

---

## 5. Shared conventions (also enforced by lint at each gate)

- **Python 3.11+**, Pydantic v2 (`model_dump(mode="json")`, not `.dict()`).
- **pytest-asyncio** with `asyncio_mode = "auto"` in `pyproject.toml`.
- **Type hints on every public function** — `mypy --strict` must pass.
- **Relative imports inside `ma_poc/`**; absolute imports elsewhere.
- **`context.close()` not `browser.close()`** — existing convention.
- **`hashlib.sha256` for deterministic hashing.** No `hash()`, which is
  salted.
- **`asyncio.Semaphore` for concurrency limits.** `asyncio.Lock` only for
  shared-state writes (StateStore).
- **`model_dump(mode="json")` for any record that crosses a process or
  disk boundary.** Datetimes become ISO strings automatically.
- **Logging:** `log = logging.getLogger(__name__)` at module top. No
  `print()` in new code — the existing `scripts/entrata.py` may keep
  its prints until J3 replaces them.
- **Error handling:** every external call (HTTP, LLM, Playwright, disk)
  wrapped in `try/except`. Profile/store failures log a warning and
  continue — never raise out of a property-level scrape.

---

## 6. Directory layout after Jugnu

```
ma_poc/
  fetch/                    # J1
    __init__.py
    fetcher.py              # Orchestrator
    retry_policy.py
    proxy_pool.py
    rate_limiter.py
    stealth.py
    conditional.py          # ETag / Last-Modified cache
    response_classifier.py
    contracts.py            # FetchResult (also re-exported from root contracts)
  discovery/                # J2
    __init__.py
    sitemap.py
    frontier.py
    scheduler.py
    dlq.py
    change_detector.py
    contracts.py            # CrawlTask
  pms/                      # J3 (reuses most of claude_refactor.md output)
    __init__.py
    detector.py
    resolver.py
    scraper.py              # orchestrator
    adapters/
      __init__.py
      base.py
      registry.py
      rentcafe.py
      entrata.py
      appfolio.py
      onesite.py
      sightmap.py
      avalonbay.py
      generic.py
  validation/               # J4
    __init__.py
    schema_gate.py
    identity_fallback.py
    cross_run_sanity.py
    contracts.py            # ValidatedRecords
  observability/            # J5
    __init__.py
    events.py
    event_ledger.py
    cost_ledger.py
    replay_store.py
    slo_watcher.py
  reporting/                # J7
    __init__.py
    property_report.py
    run_report.py
  models/
    scrape_profile.py       # v2 (J6)
    unit_record.py          # unchanged

scripts/
  daily_runner.py           # J8 — integrates all layers
  jugnu_baseline.py         # J0
  gate_jugnu.py             # J9
  replay.py                 # J5
  migrate_profiles_v1_to_v2.py  # J6

tests/
  fetch/
  discovery/
  pms/
  validation/
  observability/
  reporting/
  integration/
  profile/

docs/
  JUGNU_BASELINE.md         # J0
  BUG_HUNT_CHECKLIST.md     # J9
```

Anything under `ma_poc/templates/` and `ma_poc/extraction/` stays where
it is — Jugnu does **not** collapse those into adapters. That's out of
scope (explicit in the refactor doc).

---

## 7. The handoff contract between phases

Every phase file has two sections at the top:

```
## Inbound handoff (from phase J{N-1})
## Outbound handoff (to phase J{N+1})
```

Format:

- **Inbound** lists the artefacts the previous phase promised to
  deliver. If anything in that list is missing or broken, stop and
  flag it — do not paper over.
- **Outbound** lists the artefacts this phase promises to deliver. At
  the end of the phase, before opening the next phase file, check each
  item off. If an item cannot be delivered, flag it to the human and
  stop — do not silently drop scope.

Artefacts are one of:

- **Files** — named by path.
- **Functions/classes** — named by fully qualified path.
- **Test names** — named by `pytest` id.
- **Gate conditions** — `scripts/gate_jugnu.py phase N` passes iff …

---

## 8. What to read in what order

1. This file.
2. `JUGNU_CONTRACTS.md` — the cross-layer dataclasses.
3. `PHASE_J0_BASELINE.md` — then do J0, gate, commit.
4. `PHASE_J1_FETCH.md` — then J1, gate, commit.
5. …and so on in order.
6. For J3 specifically: `PHASE_J3_EXTRACTION.md` is short because it
   delegates to the existing `claude_refactor.md` document. Read both.

At any point you can consult `Jugnu_Robust_Crawler_Architecture.docx`
for the higher-level rationale. This instruction set compresses the
architecture doc into actionable build steps; the docx itself is the
authoritative source if the two disagree.

---

## 9. Commit discipline

- One phase = one commit (or one PR).
- Commit message first line: `Jugnu J{N}: <phase name>`.
- Commit message body: paste the gate script's output for phase N.
- Do not commit across phase boundaries. Never commit a half-finished
  phase.
- Branch name: `jugnu-j{N}-<short-name>`, e.g. `jugnu-j1-fetch`.

---

## 10. What is out of scope (do not build in this pass)

These are called out explicitly in the architecture doc §8 and
`claude_refactor.md`. Flag them in PR descriptions so reviewers don't
expect them:

- Tier-6 syndication fallback (Zillow/Apartments.com scraping).
- REIT custom stacks beyond AvalonBay (Equity, UDR, Essex, Camden,
  Mid-America).
- Cross-property clustering (the `client_account_id` cluster key is
  *captured* in J6's profile schema but no learning uses it yet).
- `LEASE_UP_VOLATILE` scrape outcome.
- CAPTCHA solver integration (DLQ the property instead).
- Azure Service Bus distributed execution.
- Collapsing `ma_poc/templates/` and `ma_poc/extraction/` into adapters.

---

*End of master guide. Proceed to `JUGNU_CONTRACTS.md`.*
