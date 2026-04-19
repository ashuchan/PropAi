# Phase J3 — Layer 3 Extraction (PMS-first)

> This phase bridges to an existing document, `claude_refactor.md`, which
> already specifies the detector → adapters → generic cascade in great
> detail. J3's job is to (a) execute that plan, and (b) apply a small
> number of Jugnu-specific deltas on top.
>
> Reference: Jugnu architecture §4.4 (Layer 3 detail), §3.3 (Extraction
> gaps), §7.1 J3 gate. And the full `claude_refactor.md`.

---

## Inbound handoff (from J2)

From J2 you must have:

- `ma_poc.discovery.build_tasks_for_run` producing `CrawlTask`s with
  `etag`, `last_modified`, `render_mode`, `reason` populated.
- `ma_poc.fetch.fetch()` returning `FetchResult` with
  `outcome == OK` containing a body and (for RENDER) a `network_log`.
- `data/state/frontier.sqlite` populated with today's URLs.
- Carry-forward firing on fetch failures (J2 observable check).

---

## How to read this phase

1. **Read `claude_refactor.md` in full first.** It is the primary
   instruction for the bulk of J3. It uses its own phase numbering
   (Phase 0–9) which is *internal to the refactor doc* — do not
   confuse those phases with the Jugnu J0–J9 numbering.
2. **Apply the deltas below.** They are small and specific.
3. **Merge the two gates.** The Jugnu J3 gate requires `claude_refactor`
   phases 1–5 all to be green, plus the deltas here.

### Mapping between refactor-doc phases and what J3 needs

| `claude_refactor.md` phase | Status for Jugnu J3 |
|---|---|
| 0 — Repo survey + baseline | **Skip** — Jugnu J0 already did this |
| 1 — PMS detection module | **Required** |
| 2 — Adapter interface + registry | **Required, with delta below** |
| 3 — Adapter implementations | **Required** |
| 4 — CTA-hop + leasing-portal resolver | **Required** |
| 5 — Scraper orchestrator rewrite | **Required, with delta below** |
| 6 — Profile v2 schema + migration | **Defer to Jugnu J6** |
| 7 — Property report v2 | **Defer to Jugnu J7** |
| 8 — daily_runner.py integration | **Defer to Jugnu J8** |
| 9 — Bug hunt + gate validation | **Defer to Jugnu J9** |

So J3 = refactor-doc phases 1, 2, 3, 4, 5 + the deltas listed below.

---

## Jugnu-specific deltas on top of `claude_refactor.md`

These are the only places Jugnu's L3 differs from the refactor doc.
Implement the refactor doc as written, then layer these on top.

### Delta 1 — `AdapterContext` adds `fetch_result`

Per `JUGNU_CONTRACTS.md` §6, `AdapterContext` now has a `fetch_result`
field:

```python
@dataclass
class AdapterContext:
    base_url: str
    detected: DetectedPMS
    profile: ScrapeProfile | None
    expected_total_units: int | None
    property_id: str
    fetch_result: FetchResult       # NEW for Jugnu — the L1 result
```

- **Why:** the adapter no longer triggers its own fetch. The L1 layer
  has already fetched (possibly with retries, proxy rotation, etc.).
  The adapter receives the result and the live Playwright `Page` (for
  adapters that need DOM interaction).
- **For adapters that don't need live DOM** (e.g. pure API parsers
  that work from `fetch_result.network_log`), the adapter ignores
  `page` and reads the network log.
- **Tests:** add one test per adapter —
  `test_<pms>_ignores_page_when_network_log_sufficient` — passing a
  stub `Page` that raises on any method, and a fully-populated
  `fetch_result.network_log`. Assert extraction succeeds.

### Delta 2 — orchestrator takes a `FetchResult`, does not fetch

Per refactor doc Phase 5, `ma_poc/pms/scraper.py` replaces
`scripts/entrata.py`. The Jugnu delta: `scrape()` signature is now:

```python
# ma_poc/pms/scraper.py
async def scrape(
    task: CrawlTask,
    fetch_result: FetchResult,
    page: Page | None,
    profile: ScrapeProfile | None,
    **kwargs,
) -> dict:  # keep the existing 46-key result shape
```

- If `fetch_result.outcome != OK`, do not call any adapter. Build a
  `FAILED_UNREACHABLE` result dict directly and return. **No LLM
  fallback on an empty body** — this is the property-5317 bug fix.
- If `page is None` (e.g. L1 returned a GET-mode result), only
  API-first adapters may run; DOM-reliant adapters fail out to the
  generic cascade with a degraded tier.
- The `fetch_result.network_log` is passed into every adapter via
  `AdapterContext.fetch_result`.

### Delta 3 — `tier_used` string namespacing

Refactor doc used strings like `TIER_1_API_ENTRATA_WIDGET`. Jugnu
standardises on a two-segment namespace:

```
<adapter_name>:<tier_key>
```

Examples:
- `entrata:widget_api`
- `rentcafe:jsonld`
- `onesite:units_endpoint`
- `sightmap:payload_joined`
- `generic:tier1_api`
- `generic:tier4_llm`
- `generic:tier5_vision`

This lets J5's cost ledger group cost by adapter without string
parsing. Update the refactor doc's tests that assert specific
`tier_used` values to use the new namespace (a single find-replace
across `tests/pms/`).

### Delta 4 — event emission from the orchestrator

Emit in `ma_poc/pms/scraper.py`:

- `extract.pms_detected` — right after `detect_pms()`, with
  `pms`, `confidence`, `evidence`
- `extract.adapter_selected` — with `adapter_name`, `pms`
- `extract.tier_started` — per tier attempted, with `tier_used`
- `extract.tier_won` — when an adapter returns units
- `extract.tier_failed` — when an adapter returns empty/errors
- `extract.llm_called` — if generic adapter invokes LLM
- `extract.vision_called` — if generic adapter invokes vision

Use the same stub `emit()` helper as J1. J5 will replace it.

### Delta 5 — generic adapter reads cond-GET state

When the generic adapter's cascade is about to invoke LLM on an
empty page (the property-5317 scenario), it must short-circuit:

```python
# in ma_poc/pms/adapters/generic.py
if ctx.fetch_result.outcome != FetchOutcome.OK or not ctx.fetch_result.body:
    return AdapterResult(
        units=[],
        tier_used="generic:no_body_short_circuit",
        winning_url=None,
        api_responses=[],
        blocked_endpoints=[],
        llm_field_mappings=[],
        errors=[f"No body to extract from: {ctx.fetch_result.outcome.value}"],
        confidence=0.0,
    )
```

This is the direct fix for "both LLMs reported 'no content provided'
— total waste: $0.01375" from the refactor doc's opening anecdote.

### Delta 6 — no LLM import in any adapter except `generic.py`

Refactor doc already enforces this; Jugnu tightens it. Add to
`scripts/gate_jugnu.py phase 3`:

```python
# Gate check: grep LLM imports in adapters except generic
for adapter_file in Path("ma_poc/pms/adapters").glob("*.py"):
    if adapter_file.name in ("__init__.py", "base.py", "registry.py", "generic.py"):
        continue
    text = adapter_file.read_text()
    assert "openai" not in text.lower(), f"{adapter_file} must not import LLM"
    assert "llm_extractor" not in text, f"{adapter_file} must not use LLM extractor"
    assert "vision_extractor" not in text, f"{adapter_file} must not use Vision"
```

### Delta 7 — cost accounting on `ExtractResult`

Every `ExtractResult` carries `llm_cost_usd`, `vision_cost_usd`,
`llm_calls`, `vision_calls` (see `JUGNU_CONTRACTS.md` §3). The
generic adapter populates these; all deterministic adapters set
them to 0 and 0.

---

## Deferred items from `claude_refactor.md`

The refactor doc specifies refactor-phases 6–9 (profile v2,
property report v2, daily_runner integration, bug hunt). Jugnu
reshuffles these into J6–J9. **Do not implement refactor-phases
6–9 as part of J3** — they are separate Jugnu phases with their own
gates and handoffs. J3 stops at the orchestrator and the adapters.

---

## Refactoring / code-quality checklist

From the refactor doc, plus Jugnu-specific:

- [ ] No cross-adapter imports (refactor doc rule).
- [ ] No PMS string literals outside their own adapter, detector, or
      resolver (refactor doc rule, enforced by grep).
- [ ] Every adapter's top-of-file comment lists the `raw_api/`
      fixtures and web sources (refactor doc rule).
- [ ] `mypy --strict ma_poc/pms/` clean (refactor doc rule).
- [ ] Coverage ≥ 85% on `ma_poc/pms/` (refactor doc rule).
- [ ] New: no LLM import outside `generic.py` (delta 6).
- [ ] New: `tier_used` follows the `adapter:tier_key` namespace
      (delta 3).
- [ ] New: `AdapterContext.fetch_result` is populated in every
      call site (delta 1).
- [ ] New: orchestrator short-circuits on `fetch_result.outcome != OK`
      before any adapter is invoked (delta 2).

---

## Gate — `scripts/gate_jugnu.py phase 3`

Passes iff:

- All 4 refactor-doc gates (phase 1, 2, 3, 4, 5) pass. Run them via
  the existing `scripts/gate_refactor.py` if it's still present, or
  inline the checks into `gate_jugnu.py`.
- Delta 6 grep check passes (no LLM in non-generic adapters).
- Delta 3 grep check passes (all `tier_used` strings follow the
  namespace).
- Delta 5 test passes: a fixture with `fetch_result.outcome=HARD_FAIL`
  runs through the orchestrator without invoking LLM and returns
  `tier_used = "generic:no_body_short_circuit"`.
- **Observable check:** re-run the property-5317 scenario
  (`ERR_SSL_PROTOCOL_ERROR`). The `properties.json` record for that
  property:
    - has `extraction_tier_used == "generic:no_body_short_circuit"`
    - has `_llm_interactions == []`
    - has `SCRAPE_OUTCOME` indicating fetch failure (either
      FAILED_UNREACHABLE via J2 carry-forward, or a tagged empty
      record)
- **Observable check:** a limit-20 run shows LLM cost concentrated
  entirely in `unknown` PMS properties (the "healthy Jugnu run"
  pattern from architecture doc §5.2).

---

## Outbound handoff (to J4 Validation)

- **Module** `ma_poc.pms.scraper.scrape()` — the only entry point L4
  uses, returning a dict with `extraction_tier_used`,
  `_extract_result` (an `ExtractResult`), and `_profile_hints` keys.
- **Module** `ma_poc.pms.adapters.registry.get_adapter()` — stable,
  used by the orchestrator and by J7's report builder for
  per-PMS grouping.
- **Contract** `ExtractResult` populated with cost fields, used by
  J5's cost ledger.
- **Event names** `extract.*` fixed.
- **No LLM spend on empty bodies** — the property-5317 fix lands here.

Commit: `Jugnu J3: PMS detector + adapters + orchestrator (bridges to
claude_refactor.md)`.

---

*Next: `PHASE_J4_VALIDATION.md`.*
