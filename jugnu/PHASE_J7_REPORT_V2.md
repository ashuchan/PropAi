# Phase J7 — Report v2 (Per-property + Run-level)

> Turn the per-property markdown report into a debugging artefact with
> verdict at the top, detection next, and LLM transcripts collapsed.
> Add per-PMS slicing and SLO checks to the run-level report. The
> property-5317 report should show `FAILED_UNREACHABLE` at the top
> and zero LLM transcripts.
>
> Reference: Jugnu architecture §5.2 (per-property report target
> format), §5 (healthy run report example), §7.1 J7 gate. Bridges to
> `claude_refactor.md` Phase 7.

---

## Inbound handoff (from J6)

From J6 you must have:

- `ScrapeProfile` v2 with `profile.stats` and
  `profile.confidence.last_success_detection` available.
- Post-migration state: < 10% of profiles have
  `api_provider == "unknown"`.

From J5 you must have:

- `CostLedger.rollup_by_pms()` — run-level report consumes this.
- `events.jsonl` with one event per layer transition —
  per-property report consumes the property's slice.
- `SloWatcher.check()` — run-level report calls this.

---

## What J7 delivers

Two reports, two formats (markdown + JSON), clear separation of
concerns:

1. **Per-property report** —
   `data/runs/<date>/reports/<cid>.md`. One per property.
2. **Run-level report** —
   `data/runs/<date>/report.md` + `report.json`. One per run.

---

## Module breakdown

```
ma_poc/reporting/
├── __init__.py
├── verdict.py              # Compute property verdict from extract + validate
├── property_report.py      # Build per-property markdown
├── run_report.py           # Build run-level markdown + JSON
├── renderers/
│   ├── __init__.py
│   ├── markdown.py         # Shared markdown helpers (table, collapsible)
│   └── json_encoder.py     # Datetime + Decimal handling
└── templates/
    ├── property.md.j2      # Jinja2 template for per-property
    └── run.md.j2           # Jinja2 template for run-level
```

### Responsibilities

| Module | Owns |
|---|---|
| `verdict.py` | Deriving the verdict label (SUCCESS / FAILED_UNREACHABLE / FAILED_NO_DATA / CARRY_FORWARD) from fetch, extract, validate results |
| `property_report.py` | Building one property's markdown from its events + records + verdict |
| `run_report.py` | Building the run-level markdown from all properties' outputs + cost ledger + SLO check |
| `renderers/markdown.py` | Shared helpers (table, `<details>` wrapping, code block escaping) |
| `renderers/json_encoder.py` | JSON serialisation for datetime, UnitRecord, enums |
| `templates/*.j2` | Jinja2 templates — presentation separate from data |

Using Jinja2 here isn't over-engineering: reports are the thing a
human reads, and having the layout separate from the data-shape code
lets you tweak presentation without risking a regression in the
numbers.

---

## The verdict model

```python
class Verdict(str, Enum):
    SUCCESS = "SUCCESS"
    FAILED_UNREACHABLE = "FAILED_UNREACHABLE"   # Fetch hard-failed
    FAILED_NO_DATA = "FAILED_NO_DATA"           # Fetch OK, no records
    CARRY_FORWARD = "CARRY_FORWARD"             # Carry-forward applied
    PARTIAL = "PARTIAL"                         # Some units dropped by validation

@dataclass(frozen=True)
class VerdictResult:
    verdict: Verdict
    reason: str                                 # 1-line human reason
    source: Literal["fetch", "extract", "validate", "carry_forward"]

def compute(
    fetch_result: FetchResult,
    extract_result: ExtractResult | None,
    validated: ValidatedRecords | None,
    carry_forward_applied: bool,
) -> VerdictResult: ...
```

Decision rules (first match wins):

1. `carry_forward_applied` → `CARRY_FORWARD`.
2. `fetch_result.outcome != OK` → `FAILED_UNREACHABLE`.
3. `extract_result is None or extract_result.empty()` →
   `FAILED_NO_DATA`.
4. `len(validated.rejected) > len(validated.accepted)` → `PARTIAL`.
5. Else → `SUCCESS`.

**Named tests (6):**

- `test_verdict_ssl_error_is_failed_unreachable`
- `test_verdict_empty_extract_is_failed_no_data`
- `test_verdict_carry_forward_wins_over_fetch_failure` (carry-forward
  signal trumps fetch outcome in the label)
- `test_verdict_majority_reject_is_partial`
- `test_verdict_all_accept_is_success`
- `test_verdict_is_deterministic` (same inputs → same output)

---

## Per-property report target format

From architecture doc §5.2 — verdict at top, detection table next,
pipeline timing, changes since last run, LLM transcripts collapsed
behind `<details>`. Exact structure:

```markdown
# Property {cid} — {property_name}

**Verdict:** `{VERDICT}` — {reason}

**URL:** {entry_url}

**Run date:** {ISO date}  ·  **Scrape time:** {duration_ms}ms

## Detection

| Field | Value |
|---|---|
| Detected PMS | `{pms}` (confidence {0.95}) |
| Adapter | `{adapter_name}` |
| Resolver method | `{no_hop | cta_link | iframe | redirect | failed}` |
| Final URL | {resolved_url} |
| Evidence | {bulleted list of detection signals} |

## Pipeline timing

| Layer | Elapsed (ms) | Notes |
|---|---|---|
| L1 Fetch | {ms} | attempts: {n} · proxy: {redacted} · render_mode: {m} |
| L2 Discovery | {ms} | source: {csv | sitemap | retry} |
| L3 Extract | {ms} | tier: `{tier_used}` · records: {n} |
| L4 Validate | {ms} | accepted: {a} · rejected: {r} · flagged: {f} |

## Records
- Units captured: {n}
- Units accepted: {a}
- Units with inferred IDs: {i}
- Units flagged: {f}

### Changes since last run
| Change | Count | Sample |
|---|---|---|
| New units | {n} | {sample unit_ids} |
| Rent changes | {n} | {sample} |
| Disappeared | {n} | {sample} |

## LLM activity (collapsed)
<details>
<summary>{n} LLM call(s), ${cost} total — click to expand</summary>

| Call | Tier | Cost | Prompt hash | Response summary |
|---|---|---|---|---|
...
</details>

## Errors
{bulleted list of non-fatal errors from each layer}

## Profile state
- Maturity: `{COLD | WARM | HOT | PARKED}`
- Consecutive successes: {n}
- Consecutive failures: {n}
- Consecutive unreachable: {n}
- Preferred tier: `{tier}`
```

**Key constraints:**
- Verdict must be in the **first line** (grep-friendly).
- LLM transcripts are always inside `<details>` — no exception, even
  for debug runs. The property-5317 report must show **zero** LLM
  activity; the `<details>` block is rendered but empty.
- Timing table sums to the total; no phantom time.

---

## Run-level report target format

From architecture doc §5 example, verbatim structure:

```markdown
# Daily Run Report — {YYYY-MM-DD}

## Totals
- csv_rows: {n}
- properties_ok: {n} ({pct}%)
- properties_fail: {n} ({pct}%)
- carry_forward: {n} (of failures)
- DLQ parked: {n}

## Properties by detected PMS
| PMS | Count | Success | LLM calls | LLM cost |
|---|---|---|---|---|
| entrata | … | … | 0 | $0.000 |
…

## Failures by error signature (aggregated)
| Signature | Count | Sample cids |
|---|---|---|
…

## Change detection
- Skipped via ETag 304: {n} ({pct}%)
- Skipped via sitemap: {n} ({pct}%)
- Forced full (7-day): {n} ({pct}%)
- Rendered: {n} ({pct}%)

## Cost summary
- Total LLM: ${…}
- Total vision: ${…}
- Total proxy MB: {…} ({${estimate} at {rate}/MB})

## SLO status
- {✓ | ✗} end-to-end success ≥95%  ({observed}%)
- {✓ | ✗} LLM cost <$1/day         ({observed})
- {✓ | ✗} vision fallback ≤5%      ({observed}%)
- {✓ | ✗} drift detector noise <2% ({observed}%)
```

The SLO section reads directly from `SloWatcher.check()`. A failed
SLO gets a `✗` and renders a single-line explanation.

---

## File creation order

1. `ma_poc/reporting/__init__.py`
2. `ma_poc/reporting/renderers/json_encoder.py`
3. `ma_poc/reporting/renderers/markdown.py`
4. `ma_poc/reporting/templates/property.md.j2`
5. `ma_poc/reporting/templates/run.md.j2`
6. `ma_poc/reporting/verdict.py`
7. `ma_poc/reporting/property_report.py`
8. `ma_poc/reporting/run_report.py`

Tests under `tests/reporting/`.

---

## Named tests

### `tests/reporting/test_verdict.py` — 6 tests (listed above)

### `tests/reporting/test_property_report.py` — 10 tests

| Test | Check |
|---|---|
| `test_property_report_verdict_is_first_line` | Any report's first non-empty line after the `#` heading contains `**Verdict:**` |
| `test_property_5317_shows_failed_unreachable` | Fixture with SSL error → verdict line reads `FAILED_UNREACHABLE` |
| `test_property_5317_shows_zero_llm_calls` | Same fixture → `<details>` block summary says "0 LLM call(s)" |
| `test_property_report_hides_raw_llm_content_by_default` | LLM section is wrapped in `<details>` — the full prompt/response is only inside the block |
| `test_property_report_timing_table_sums` | Sum of layer ms == total scrape ms (±10ms for template overhead) |
| `test_property_report_detection_evidence_bulleted` | Multiple evidence items render as a bullet list, not a comma string |
| `test_property_report_profile_state_shown` | Maturity / counters from profile rendered |
| `test_property_report_changes_section_populated` | New/rent_change/disappeared counts shown |
| `test_property_report_handles_missing_profile` | Property with no prior profile → "Profile state" section says "(new)" — no crash |
| `test_property_report_is_deterministic` | Same inputs → byte-identical markdown |

### `tests/reporting/test_run_report.py` — 9 tests

| Test | Check |
|---|---|
| `test_run_report_header_has_date` | First line matches `# Daily Run Report — \d{4}-\d{2}-\d{2}` |
| `test_run_report_pms_table_has_all_detected_pmss` | Detected PMSs appear as rows |
| `test_run_report_llm_cost_is_zero_for_non_unknown_pmss` | Fixture with entrata/rentcafe successful and unknown failed → only unknown shows non-zero cost |
| `test_run_report_failure_signatures_grouped` | 10 SSL failures + 5 timeouts → 2 rows in the failures table |
| `test_run_report_change_detection_counts_sum_to_100pct` | HEAD + sitemap + full + rendered % sums to 100% |
| `test_run_report_slo_all_green_shows_checkmarks` | All SLOs pass → all rows start with ✓ |
| `test_run_report_slo_violation_shows_x` | Drift noise 11.2% → drift row starts with ✗ |
| `test_run_report_writes_json_alongside_md` | After build, both `report.md` and `report.json` exist |
| `test_run_report_json_has_all_top_level_keys` | `report.json` contains `totals`, `pms_table`, `failures`, `change_detection`, `cost`, `slo` |

---

## Refactoring / code-quality checklist

- [ ] Reports are Jinja2-templated — **no markdown strings interpolated
      in Python**. Anything over one line of markdown goes in a
      `.j2` file.
- [ ] Verdict logic is a pure function (`compute()` takes dataclasses,
      returns a dataclass; no I/O).
- [ ] Report builders are pure (inputs: dicts and dataclasses;
      output: strings/dicts; no disk I/O). A separate thin `write()`
      function does the disk write.
- [ ] No file over 300 lines.
- [ ] `mypy --strict` + `ruff` clean on `ma_poc/reporting/`.
- [ ] Coverage ≥ 85%.

---

## Gate — `scripts/gate_jugnu.py phase 7`

Passes iff:

- All 25 tests pass.
- Static analysis clean, coverage ≥ 85%.
- **Observable check:** regenerate `reports/5317.md` from stored
  04-15 (or 04-17 if newer) data. The output:
    - has `**Verdict:** FAILED_UNREACHABLE` on first line
    - has 0 LLM transcripts inside the `<details>` block
    - total scrape time reflects fetch-layer short-circuit
      (should be < 5s, not 60s+)
- **Observable check:** the run-level report's "Properties by
  detected PMS" table has a row for every PMS that appeared in the
  detection events, with LLM cost == 0 for every non-unknown PMS.
- **Observable check:** SLO section renders at least one ✓ and
  reports the actual observed value, not a placeholder.

---

## Outbound handoff (to J8 Integration)

- **Functions:**
    - `ma_poc.reporting.verdict.compute()`
    - `ma_poc.reporting.property_report.build(cid, run_dir,
       event_ledger, ...)` → `str`
    - `ma_poc.reporting.run_report.build(run_dir, cost_ledger, slo,
       ...)` → `(md_str, json_dict)`
    - Companion `write_*` functions in each module
- **Disk layout:**
    - `data/runs/<date>/reports/<cid>.md` — one per property
    - `data/runs/<date>/report.md` — one per run
    - `data/runs/<date>/report.json` — one per run
- **Templates** stable under `ma_poc/reporting/templates/` — future
  tweaks land here without code changes.
- **Baseline delta:** the 5317-style scenario now produces a clean
  FAILED_UNREACHABLE report instead of a garbage-laden LLM
  transcript.

Commit: `Jugnu J7: per-property + run-level reports with verdict,
PMS slicing, SLO`.

---

*Next: `PHASE_J8_INTEGRATION.md`.*
