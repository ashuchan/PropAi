# Jugnu Pipeline — Per-Property Algorithm Specification

**Entry point:** `ma_poc/scripts/jugnu_runner.py`
**Purpose of this document:** A complete, decision-by-decision specification of what happens to a single property as it flows through the Jugnu pipeline. Intended for algorithmic review / feedback — every branch, threshold, and fallback is called out with the source location.

---

## 1. Pipeline overview

The Jugnu pipeline is a 5-layer architecture with frozen dataclass contracts between layers:

```
CSV row
   │
   ▼
[L2 Discovery]   Scheduler → CrawlTask (priority, render_mode, budget)
   │
   ▼
[L1 Fetch]       9-step fetch → FetchResult (outcome, body, headers, network_log)
   │
   ▼
[Carry-forward branch]  If fetch unusable → copy yesterday's record (SUCCESS verdict)
   │
   ▼
[L3 Extraction]  detect_pms → resolve → adapter.extract → ExtractResult
   │
   ▼
[Self-learning]  update profile maturity, known/blocked endpoints, drift check
   │
   ▼
[L4 Validation]  schema gate → identity fallback → cross-run sanity → ValidatedRecords
   │
   ▼
[Verdict]        SUCCESS | PARTIAL | FAILED_NO_DATA | FAILED_UNREACHABLE | CARRY_FORWARD
   │
   ▼
[Output format]  v1 (46-key) OR v2 (flat) schema
   │
   ▼
[F1 / F2 LLM hooks]  adapter_debugger on Tier-1 dead ends, null_field_recovery on v2 nulls
   │
   ▼
[Reporting]      properties.json, report.json/md, per-property markdown, llm_report.json
```

All layers follow a **never-fail contract** — no single property can crash the run. Every layer returns structured failure outcomes instead of raising.

---

## 2. Inputs a single property carries through the pipeline

- **`csv_row`**: dict of CSV cells (property_id, url, name, city, state, zip, pmc, management_company, pms_platform hint, etc.)
- **`task: CrawlTask`**: frozen dataclass produced by Scheduler — `(property_id, url, reason, render_mode, priority, budget_ms, etag?, last_modified?)`
- **`profile: ScrapeProfile | None`**: the per-property learned profile from `config/profiles/{canonical_id}.json`. Bootstrapped COLD on first sighting.
- **`fetch_result: FetchResult`**: frozen dataclass from L1 — `(url, outcome, status_code, headers, body, elapsed_ms, render_mode, identity_key, proxy_label, error_signature)`

---

## 3. Stage-by-stage decisions for one property

### STAGE 1 — L2 Discovery: CSV row → CrawlTask

**Module:** `ma_poc/discovery/scheduler.py::Scheduler.build_tasks`

**Decision tree:**

1. **Is this property parked in the DLQ?**
   - `dlq.is_parked(property_id)` → **YES**: skip task, emit `TASK_SKIPPED_DLQ`, property does not run this cycle.
   - **NO**: proceed.

2. **Is the property due for a DLQ retry?** (`dlq.due_for_retry(property_id)`)
   - **YES**: create CrawlTask with `priority=0, reason=DLQ_REVIVE`.
     - Retry cadence: hourly for first 6 hours, then daily (`dlq.py:155-158`).
   - **NO**: proceed to change-detector decision.

3. **URL normalization** (`_normalize_url`, `scheduler.py:97`):
   - Bare domain or path → prepend `https://`.
   - `http://` → rewritten to `https://` (HSTS redirect stalls killed 6 properties in prior runs; see legacy failure mode 4 in CLAUDE.md).

4. **Change-detector decision** (`change_detector.py::decide`): inputs are `profile.confidence.maturity`, `days_since_full_render`, `force_full`, and (optionally) `sitemap.lastmod` vs. `frontier.last_attempted`. First match wins:
   - `force_full == True` → **RENDER**, reason=`manual_force`, `use_cond_headers=False`.
   - `days_since_full_render is None OR > 7` → **RENDER**, reason=`stale_render_7d`. Never-rendered properties always get full render.
   - `maturity == "HOT" AND days_since_full_render < 1` → **GET**, reason=`hot_profile_fresh`, use conditional headers.
   - `sitemap_lastmod < frontier.last_attempted` → **GET**, reason=`sitemap_unchanged`, use cond headers.
   - `maturity == "WARM" AND days_since_full_render < 3` → **GET**, reason=`warm_profile_static`, use cond headers.
   - default → **RENDER**, reason=`default_render`, no cond headers.
   - **Note:** HEAD is never produced — no escalation path would turn HEAD's missing body into a successful extraction.

5. **Task priority + budget:**
   - DLQ_REVIVE → `priority=0`, `budget_ms=180_000`.
   - RENDER → `priority=1`, `budget_ms=180_000`.
   - GET/HEAD → `priority=2`, `budget_ms=180_000`.
   - Sitemap probes (internal to Scheduler): `budget_ms=15_000`.
   - Within a priority bucket: host-shuffle to avoid hammering one domain.

**Output:** One `CrawlTask` per property URL.

---

### STAGE 2 — L1 Fetch: CrawlTask → FetchResult

**Module:** `ma_poc/fetch/fetcher.py::fetch`

**9-step flow, never raises:**

1. **Robots.txt allow check**
   - `robots.is_allowed(url, user_agent)` — if disallowed → `FetchResult(outcome=HARD_FAIL, error_sig="ROBOTS_DISALLOWED")`. No further work.

2. **Conditional cache lookup** (for GET/HEAD only, skipped for RENDER)
   - Read `(etag, last_modified)` from SQLite cache. Task-provided values override cache.

3. **Rate-limiter acquire** (`rate_limiter.py`)
   - Per-host token bucket. Default 2 req/sec. `robots.txt Crawl-delay` overrides → `rps = 1.0 / max(delay_sec, 0.1)`.
   - 30-second timeout on acquire.

4. **Identity + proxy selection**
   - **Identity**: deterministic SHA256 hash of `property_id` into 8-slot pool → same browser identity per property across runs. Real UAs (Chrome/Firefox/Edge/Safari × Windows/macOS/Linux). Never LLM-generated strings.
   - **Proxy**: weighted health-score selection, sticky by `property_id`.
     - Start score = 1.0. Success: `+0.05` (cap 1.0). Failure: `−0.25` (floor 0.1). Quarantine threshold: `score < 0.25`.

5. **Request dispatch:**
   - **RENDER**: Playwright Chromium. Network capture handler registered **before** `page.goto()`. Per-response content-type filter (json/xml/html/text). **Body cap 256 KB** (raised from 10 KB — prior cap silently truncated RentCafe/SightMap payloads mid-JSON). Wait `domcontentloaded + 500 ms` (not `networkidle` — analytics trackers block forever). Per-attempt timeout `min(budget_ms, 20_000) ms`.
   - **GET**: httpx full-body fetch.
   - **HEAD**: never produced by scheduler but supported for completeness.

6. **Response classification** (`response_classifier.classify`):
   - `OK` → 2xx with body.
   - `NOT_MODIFIED` → 304 with matching conditional headers.
   - `BOT_BLOCKED` → 403, CAPTCHA text/image detected, Cloudflare/reCAPTCHA/hCaptcha/PerimeterX signature.
   - `RATE_LIMITED` → 429, Retry-After header.
   - `TRANSIENT` → 5xx, timeout, DNS flake.
   - `HARD_FAIL` → SSL error, 4xx (non-429), DNS-permanent, robots-deny.
   - `PROXY_ERROR` → 407 proxy auth, proxy exhausted.

7. **Retry decision** (`retry_policy.decide(outcome, attempt, retry_after)`):
   - **Early-exit for stuck renders**: RENDER mode, attempt ≥ 2, `"TIMEOUT"` in error_sig → break (save ~35s instead of retrying).
   - Otherwise: up to 3 attempts, exponential backoff.
   - On retry: rotate stealth identity counter; pick fresh proxy (sticky_key=None).

8. **On success (`ok()`):**
   - Write `(etag, last_modified, fetched_at)` to conditional cache.
   - Bump proxy health.
   - Persist raw HTML (RENDER only).

9. **Return `FetchResult`** — structured outcome, never an exception.

**Side effects emitted as events:** `FETCH_STARTED`, `FETCH_ATTEMPT`, `FETCH_CAPTCHA_DETECTED`, `FETCH_COMPLETED`, `FETCH_FAILED`.

---

### STAGE 3 — Carry-forward branch check

**Module:** `ma_poc/discovery/carry_forward.py::should_carry_forward`

**Called from `jugnu_runner._process_property` immediately after fetch:**

```
if not fetch_result.ok():
    should_cf, reason = should_carry_forward(None, fetch_outcome=outcome)
    if should_cf:
        cf_record = carry_forward_property(property_id, runs_latest, state_store, reason)
        if cf_record:
            cf_record._meta.verdict = "SUCCESS"  # stamped here, not by Verdict layer
            return cf_record
```

**`should_carry_forward` returns True when:**

1. `fetch_outcome in ("HARD_FAIL", "BOT_BLOCKED", "PROXY_ERROR")` → hard failure.
2. `fetch_outcome == "NOT_MODIFIED"` → 304 means server has nothing new; yesterday's data is still truthful.
3. `fetch_outcome == "TRANSIENT"` after retries exhausted.
4. `scrape_result is None`.
5. `"FAIL" in tier_used.upper()` on the scrape result.
6. `units == [] AND tier_used is not None`.

**`carry_forward_property` fallback path:**
- Try `state_store` first (legacy JSON index under `data/state/`).
- Fall back: walk `runs/{date}/properties.json` newest-first across v1 and v2 schema roots.
- Only accept prior records that have actual `units` — never re-emit a prior FAILED or prior CARRY_FORWARD record.

**Subtle behavior: verdict stamping.** The carry-forward branch in `jugnu_runner` stamps `_meta.verdict = "SUCCESS"` with `verdict_reason = "carry_forward_applied"` before returning, so the CF record lands as SUCCESS in run reports. It does **not** round-trip through the Verdict layer (see Stage 8), so the `CARRY_FORWARD` verdict string is never actually produced in this path — every CF is labelled SUCCESS. **This is a known inconsistency worth flagging for review.**

---

### STAGE 4 — L3 PMS Detection

**Module:** `ma_poc/pms/detector.py::detect_pms`

**Called twice:** once on URL+csv_row alone (before HTML), once after HTML is available. If the second call is more confident, the result is adopted.

**Never-raises:** on any exception → `(pms="unknown", confidence=0.0)`.

**Signal priority (first confident hit wins):**

1. **CSV override**
   - Explicit `pms_platform` column → trust at **0.95** confidence.
   - Unknown value → `"custom"`, **0.75**.

2. **URL host fingerprints**
   - `^\d{3,9}\.onlineleasing\.realpage\.com$` → OneSite, **0.95**.
   - Host ends in: `rentcafe.com`, `sightmap.com`, `appfolio.com`, `entrata.com` → **0.95**.
   - Host ends in `realpage.com` (non-OneSite) → **0.80**.
   - Host ends in `nestiolistings.com` → Funnel, **0.95**.
   - Host ends in `mytouchtour.com` → TouchTour, **0.95**.
   - Host ends in `liveovation.com` → TouchTour, **0.85**.

3. **URL extension heuristic**
   - Path ends `.aspx` on non-Microsoft vanity host → RentCafe/Yardi, **0.70**.

4. **HTML markers (only when `page_html` passed):**
   - Platform giveaways first (`Wix.`, `squarespace`) → **0.85**.
   - PMS-specific strings (`entrata.com`, `/Apartments/module/`, `rentcafe`, `yardi`) → **0.80–0.85**.
   - Funnel (`nestiolistings.com`, `nestio_` globals) → **0.90**.

5. **Management-company prior** (`MGMT_TO_PMS_PRIOR`): mark-taylor → entrata, lindsey → rentcafe, avalonbay → avalonbay, windsor → funnel (overrides Yardi prior), ovation → touchtour — all at **0.70**.

**Consensus boost:** Two independent signals agreeing → `base + 0.10 × (n_agreeing - 1)`, capped at **0.95** (`detector.py:553-566`).

**Confirmation check (post-network-capture):** If adapter defines `matches_response_body(body) -> bool` and **no captured API response matches**, demote to `"unknown"`. Guards against URL-based misrouting (e.g., URL says RentCafe but captures show Funnel).

---

### STAGE 5 — CTA-hop resolution

**Module:** `ma_poc/pms/resolver.py::resolve_target`

Only runs when a Playwright `page` is available. Jugnu in fetch-only mode skips this step.

**Algorithm:**

1. **Short-circuit:** `initial_detection.confidence ≥ 0.85 AND url matches PMS fingerprints` → `ResolvedTarget(method="no_hop")`.

2. **CTA link extraction:**
   - Query every `<a href>`. Filter anchor text against `/availab|floor|pricing|apply|lease/i`.
   - Score: `"availab"` 100, `"floor"` 80, `"pricing"` 70, `"apartment"` 55, `"apply"` 50.
   - Sort by score, cap at 5 candidates.

3. **For each candidate:** `detect_pms(href)` — if it matches any adapter's static fingerprints → `method="cta_link"`.

4. **Iframe check:** `<iframe>` src pointing to sightmap.com / rentcafe.com / leasing portals → `method="iframe"`.

5. **Redirect chain:** if page redirected during load to a PMS host → `method="redirect"`.

6. **Failed:** `method="failed"` — generic adapter takes over.

**Output:** `ResolvedTarget(original_url, resolved_url, hop_path, final_detection, method)`.

---

### STAGE 6 — Extraction cascade (inside adapter)

#### 6a. Dispatch

- `detected.pms == "unknown"` → `GenericAdapter`.
- Otherwise → PMS-specific adapter (entrata / rentcafe / sightmap / appfolio / realpage / onesite / funnel / touchtour / avalonbay / squarespace / wix).

PMS-specific adapters call into **helpers** shared with GenericAdapter for JSON-LD and API parsing, but they do NOT run LLM or Vision tiers. LLM/Vision only fires inside `GenericAdapter` — enforced by contract.

#### 6b. GenericAdapter tier cascade (`pms/adapters/generic.py`)

**Execution order, first non-empty result stops the cascade. Every tier emits `extract.tier_attempted` with outcome / units_found / duration_ms / reason.**

| Sub-tier | Key | Deterministic? | What it does |
|---|---|---|---|
| 0 | `generic:blocked_filter` | Yes | Drops API responses matching `profile.api_hints.blocked_endpoints` before any extractor sees them. |
| 0 | `generic:profile_replay` | Yes | Replays saved `LlmFieldMapping` (json_paths + envelope) on matching API URLs. Zero LLM cost. Tier = `TIER_1_PROFILE_MAPPING`. |
| 1 | `generic:api_narrow` | Yes | `parse_generic_api` on responses with unit signals. Emits `market_rent_low/high` ints + `rent_range` string. |
| 2 | `generic:api_broad` | Yes | Broad parser + host-specific (SightMap, RealPage) parsers. |
| 3 | `generic:jsonld` | Yes | JSON-LD Apartment/Offer parser. **Rejects plan-name-only output** (no rent, no sqft) so LLM tiers still get a chance. |
| 4 | `generic:embedded_json` | Yes | `__NEXT_DATA__`, `__NUXT__`, `window.__INITIAL_STATE__`, SSR blobs. |
| 5 | `generic:dom_scan` | Yes | CSS-selector DOM cascade. Filters junk (`MODULE_*`, "Lease Magnet", "Pop-Up") at extract time. |
| 6a | `generic:llm_api_targeted` | No | `analyze_api_with_llm` on each candidate API, max **3/property**. Returns units + json_paths + response_envelope → persisted as `LlmFieldMapping`. Tier = `TIER_4_LLM_API`. |
| 6b | `generic:llm_dom_targeted` | No | `analyze_dom_with_llm` on the tightest rent-containing DOM section, max **1/property**. Returns units + CSS selectors. Tier = `TIER_4_LLM_DOM`. |
| 6c | `generic:llm` | No | Monolithic fallback (`extract_with_llm`). Fires **only when 6a+6b returned empty**. Captures `navigation_hint` for link-hop. |

#### 6c. LLM gate + budget

- **Default:** `skip_llm = (ctx.detected.pms != "unknown")`.
- **Relaxed gate (`LLM_GATE_RELAXED`):** when the detected PMS adapter returned empty AND the page has ≥5KB text AND ≥1 rent signal. Catches SightMap/RentCafe cases where the API wasn't captured.
- **Budget:** 3 targeted API calls + 1 targeted DOM call + 1 monolithic fallback per property per run. Tracked in `ma_poc/observability/cost_ledger.py`.

#### 6d. Sanity checks applied inside Tier 1/2/3 parsers

- **Dedup gate** (`parse_generic_api`): candidate list rejected unless 3+ dicts have BOTH a unit-id key AND a rent-like key.
- **Junk filters** (`_parsing.is_junk_floor_plan`, `is_junk_unit_number`): drop CMS module names, vendor prefixes (`[Riedman]`), stop-word unit numbers ("Left", "s").
- **Rent sanity:** reject rent outside `$200–$50,000` (catches misidentified fields like `rent=14` from a review score).
- **Sqft cap:** `_MAX_SQFT = 20_000`.

#### 6e. Self-learning payload surfaced to the profile updater

Every `scrape_jugnu` result dict carries:
- `_llm_interactions` — cost-accounting records for every LLM call.
- `_llm_analysis_results` — `{api_url: LlmFieldMapping | "noise:<reason>"}`.
- `_llm_field_mappings` — mapping dicts written on this run.
- `_llm_hints` — `{css_selectors, platform_guess, navigation_hint, ...}`.
- `_llm_navigation_hints` — URLs extracted from LLM `navigation_hint` fields, forwarded to link-hop as priority-1000 candidates.
- `_explored_links` — `{sub_url: had_data_bool}` (populated even when recovery failed, so profile learns which links NOT to revisit).
- `_winning_page_url` — the URL/API that produced the winning data.

#### 6f. Property context threaded into every prompt

`AdapterContext` carries `property_name, city, state, zip_code, pmc` sourced from the CSV row. All three prompt templates (`tier4_extraction.txt`, `api_analysis.txt`, `dom_analysis.txt`) reference these placeholders. Before the 2026-04-20 fix, these were hard-coded to empty strings inside the generic adapter.

---

### STAGE 7 — Profile self-learning loop

**Module:** `services/profile_updater.py::update_profile_after_extraction`, then `services/drift_detector.py`.

Runs **after extraction**, before validation. Swallows all exceptions (profile learning must never block the scrape).

**On successful extraction:**
- `winning_page_url` ← `result._winning_page_url`; path → `availability_page_path`.
- If tier ∈ (TIER_1_API, TIER_1_WIDGET, ...) and body looks like units → API URL added to `profile.api_hints.known_endpoints`.
- Widget endpoints (`/apartments/module/widgets/`) tracked separately as `widget_endpoints`.
- `LlmFieldMapping` from Tier 6a saved to `profile.api_hints.llm_field_mappings` (cap 20 entries; `success_count` incremented).
- `css_selectors` from Tier 6b saved to `profile.dom_hints.field_selectors`.
- `availability_links` ← successful links; `explored_links` ← links that had no data (cap 30).
- `consecutive_successes += 1`, `consecutive_failures = 0`.

**On failed extraction:**
- LLM-classified-as-noise APIs → `profile.api_hints.blocked_endpoints` with reason (cap 50, FIFO).
- `explored_links` ← all explored links that had no data.
- `consecutive_failures += 1`, `consecutive_successes = 0`.

**Maturity transitions:**
- `consecutive_successes ≥ 3` → `WARM → HOT`.
- `consecutive_successes ≥ 1` → `COLD → WARM`.
- `consecutive_failures ≥ 3` → `→ COLD`.

**Drift detection** (`drift_detector.py`), runs immediately after profile update:
1. **Unit count drop >30%** — `units_extracted < expected_count × 0.7` → flag `unit_count_drop`.
2. **All rents null** — `units_extracted > 0 AND null_rents == len(units)` (checks v1 keys `rent_range, market_rent_low, asking_rent` and v2 `rent_low`) → flag `all_rents_null`.
3. **Timeout pattern** — `_timeout AND consecutive_failures ≥ 2` → flag `timeout_pattern`.

**Demotion rules:**
- Severe drift (`all_rents_null` or `timeout_pattern`): demote to `COLD`, reset `consecutive_successes`.
- Non-severe drift on a HOT profile: demote to `WARM`.
- Otherwise: no change.

---

### STAGE 8 — L4 Validation

**Module:** `ma_poc/validation/orchestrator.py::validate`

Runs per-record, then computes property-level flags.

**Per-record pipeline:**

1. **Schema gate** (`schema_gate.py::schema_check`):
   - Rent: extract from `asking_rent | market_rent_low | rent`. Reject if `< 0` (`INVALID_RENT_NEGATIVE`) or `> 50_000` (`INVALID_RENT_ABSURD`).
   - Sqft: extract from `sqft | square_feet`. Reject if `< 0` or `> 20_000` (`INVALID_SQFT_*`).
   - Date: try ISO, then plain date. Reject if neither parses (`INVALID_DATE_FORMAT`).
   - Unit ID: if missing → `compute_fallback_id(record)`; on failure (<2 identifying fields) → reject (`IDENTITY_FALLBACK_INSUFFICIENT`).

2. **Identity fallback** (`ma_poc/scripts/identity_fallback.py::compute_fallback_unit_id`):
   - **Canonical location:** `ma_poc/scripts/identity_fallback.py` (moved from validation/ in PR #21).
   - Hash inputs in fixed order: `property_id` → normalised `floor_plan_name` → `beds` → `baths` → `sqft` rounded to nearest 10.
   - **Rent and available_date deliberately excluded** — these are mutable attributes, not physical identity.
   - Field name aliases accepted: `floor_plan_name` or `_floor_plan`, `beds`/`bedrooms`/`_bedrooms`, `baths`/`bathrooms`/`_bathrooms`, `_sqft`/`area`/`sqft` (`_sqft` takes priority).
   - `area = -1` sentinel treated as absent (coerced to empty string before hashing).
   - Require `floor_plan` AND at least one other non-empty field — otherwise return None.
   - Output: `inferred_{sha256[:16]}`. Never `hash()`.
   - Legacy `compute_fallback_id` (v1) retained for backward compat with existing state files — still includes rent. Migrate all new callers to `compute_fallback_unit_id`.
   - If inferred: set `inferred_id=True`, accept, emit `IDENTITY_FALLBACK`.

3. **Cross-run sanity** (`cross_run_sanity.py::sanity_check`): compared against historical record. **Flags only — never rejects:**
   - Rent swing `>20%` → `rent_swing_>20pct`.
   - Rent swing `>50%` → `rent_swing_>50pct` (both may fire simultaneously).
   - Sqft change `>5%` → `sqft_changed`.
   - Floor-plan rename (case-insensitive normalized) → `floor_plan_renamed`.

**Property-level decision after all records processed:**

4. **`next_tier_requested` flag** set when EITHER:
   - Majority rejected: `rejected_count > accepted_count AND (rejected + accepted) > 0`, OR
   - F1 quality gate fails — `accepted.length > 0` but `property_passes_quality_gate(accepted)` is False (all units are "hollow", i.e. no unit has ≥1 substantive field from `{beds, rent_low, floor_plan_name, area}` for v2 / `{bedrooms, asking_rent, market_rent_low, sqft, floor_plan_type}` for v1).

`next_tier_requested` triggers the F2 null-field-recovery path on this run AND signals the cascade to escalate next run.

**Output:** `ValidatedRecords(accepted, rejected, flagged, next_tier_requested)`.

---

### STAGE 9 — Verdict computation

**Module:** `ma_poc/reporting/verdict.py::compute`

**Decision rules (first match wins):**

1. `carry_forward_applied == True` → **CARRY_FORWARD**. (In practice, Jugnu's CF branch in Stage 3 stamps SUCCESS directly and never reaches Verdict, so this case is theoretical in the current wiring — see known issue.)
2. `fetch_outcome not in ("OK", "NOT_MODIFIED")` → **FAILED_UNREACHABLE**.
3. `extract_result is None OR records == []` → **FAILED_NO_DATA**.
4. `rejected_count > accepted_count AND (rejected + accepted) > 0` → **PARTIAL**.
5. `units_hollow == True` (F1 quality gate) → **FAILED_NO_DATA**.
6. Otherwise → **SUCCESS**.

**Stamping:** `_meta.verdict` and `_meta.verdict_reason` are written on the property record. Both `run_report.py` and `slo_watcher.py` key off `_meta.verdict` + `_extract_result.tier_used` (after the 2026-04-19 fix — they previously read `_meta.scrape_tier_used` which only the legacy pipeline writes).

---

### STAGE 10 — Output formatting (v1 / v2)

**Module:** `jugnu_runner._format_output` → `_format_v1` or `_format_v2`.

**Schema selection:** CLI `--schema-version` > env `SCHEMA_VERSION` > default `"v1"`.

**v1 (46-key legacy schema):**
- CSV values take precedence over scraped metadata for: name, address, city, state, zip, pmc.
- Scraped metadata fills blanks.
- Computed aggregates from units: `Total Units`, `Average Unit Size (SF)`.
- 14 external-only fields (Census Block, Tract Code, Construction dates, Market/Submarket, Asset Grades, Lease Start) — CSV passthrough only; always null if CSV blank.

**v2 (flat schema with normalized units):**
- `apartment_id` coerced to int (strips commas, parses via float).
- `website_design` derived from platform label (`entrata → "Powered by Entrata"` etc.).
- `concessions` pulled from `result.concessions_text` or `md.concessions`.
- Per-unit transform `_format_v2_unit`:
  - `unit_id`: `unit_id → unit_number → _unit_number`.
  - `rent_low/high`: `market_rent_low/high → asking_rent → parse_rent_range("$1,200 - $1,500")`.
  - `floor_plan_name`: `_floor_plan → floor_plan_name → floorplan_name`.
  - `area`: `_sqft → sqft → area`. **Rejected to `-1` if outside [150, 10_000] sqft** (prior runs leaked values of 9, 12, 50, 70, 100 because any positive int was accepted).
  - `beds / baths`: **`None` when source emitted nothing** — no silent default to 0 / 1.0 (lets downstream spot parser gaps).
  - Junk filter applied again (belt-and-braces): `is_junk_floor_plan` / `is_junk_unit_number`.
- `lease_term` and `move_in_date` plumbed end-to-end but still mostly null — parsers don't target them yet.

---

### STAGE 11 — LLM diagnostics (F1 / F2 hooks)

#### F1: `adapter_debugger` — Tier-1 dead-end diagnosis

**Module:** `ma_poc/services/llm_diagnostics.py::adapter_debugger`

**Entry conditions (in `jugnu_runner._process_property`):**
- `verdict == "FAILED_NO_DATA"` AND `tier_used.startswith("TIER_1_")` AND `run_dir is not None`.
- Diagnosis file doesn't already exist (one per property per run).

**Behavior:**
- Resolves adapter name from tier (`TIER_1_API_RENTCAFE → rentcafe`, falls back to `generic`).
- Fetches the adapter's parser source via `get_adapter_parser_source`.
- Iterates top 3 raw API responses; calls LLM with `(adapter_source, api_response, property_context)`.
- Stops after first diagnosis.

**Output `AdapterDiagnosis`:**
- `failure_category`: `case_mismatch | missing_wrapper_key | field_name_mismatch | wrong_endpoint_type | auth_required | ...`
- `wrapper_fix`: suggested key path to unwrap nested units.
- `field_fixes`: `[(original_key, actual_key, fix_type, code_change)]`.
- `can_auto_fix: bool`.
- `estimated_units_recoverable: int`.
- `adapter_code_patch`: unified-diff-style change.

Logged + merged into `result._llm_interactions` (for cost tracking).

#### F2: `null_field_recovery` — v2 null field rescue

**Module:** `ma_poc/services/llm_diagnostics.py::null_field_recovery` (called from `jugnu_runner._run_null_field_recovery`).

**Entry conditions:**
- `schema_version == "v2"`.
- `tier_used.startswith("TIER_1_")` AND raw API responses captured.
- At least one unit has `rent_low is None OR unit_id is None`.

**Behavior:**
- Processes up to 5 null units.
- For each: sends `(partial_unit, source_fragment, tier_used, parser_logic_summary, property_context)` to LLM.
- Applies recoveries in-place on the formatted unit dicts **only if `confidence ≥ 0.85`**.

**Fields recoverable:** `rent_low`, `rent_high`, `unit_id`, `floor_plan_name`.

Both F1 and F2 are best-effort — never raise, always log to `llm_diagnostics/` under the run dir.

---

## 4. Concurrency model

- `jugnu_runner` uses `AsyncPool` from `ma_poc/scripts/concurrency.py`.
- Pool size = `min(RAM_based_cap, cpu_count × 2, MAX_CONCURRENT_BROWSERS_env)`, clamped to `[1, 32]`.
- RAM-based cap: `available_RAM × 70% / 250 MB per browser`.
- Properties run concurrently inside a single event loop; `list.append/extend` on `all_llm_interactions` is safe because asyncio is single-threaded.

---

## 5. Output artifacts per run

Under `data/{v2/?}runs/{date}/`:
- `properties.json` — merged list of property records (new batches are merged by `canonical_id` on top of any existing file, so `--start-index` resumes don't clobber earlier batches).
- `report.json` / `report.md` — run summary with totals, tier distribution, SLO section.
- `cost_ledger.db` — SQLite per-property cost breakdown.
- `llm_report.json` — run-wide aggregate of LLM calls (written only when any fired).
- `llm_reports/{canonical_id}.json` — per-property LLM report.
- `property_reports/{canonical_id}.md` — per-property markdown.
- `llm_diagnostics/{canonical_id}_adapter_debug.json` — F1 artifact.
- `llm_diagnostics/{canonical_id}_null_field_recovery.json` — F2 artifact.

Persistent state in `data/{v2/?}state/`:
- `frontier.sqlite` — URL frontier.
- `dlq.jsonl` — dead-letter queue.
- `cache/conditional.sqlite` — ETag / Last-Modified cache.

---

## 6. Threshold & magic-number reference

| Concern | Threshold | Source |
|---|---|---|
| DLQ parking | `consecutive_failures ≥ 3` | `frontier.py`, `dlq.py` |
| DLQ retry — early phase | hourly for 6h | `dlq.py:155-156` |
| DLQ retry — late phase | daily after 6h | `dlq.py:157-158` |
| Scheduler — stale render | `days_since_full_render > 7` | `change_detector.py:62` |
| Scheduler — HOT probe window | `days < 1` | `change_detector.py:65` |
| Scheduler — WARM probe window | `days < 3` | `change_detector.py:80` |
| Standard task budget | 180_000 ms | `scheduler.py:86` |
| Sitemap fetch budget | 15_000 ms | `sitemap.py:64` |
| Sitemap child cap | 10 | `sitemap.py:19` |
| Stealth identity pool | 8 identities | `stealth.py:26-75` |
| Proxy health — success | `+0.05` (cap 1.0) | `proxy_pool.py:91` |
| Proxy health — failure | `-0.25` (floor 0.1) | `proxy_pool.py:103` |
| Proxy quarantine | `health < 0.25` | `proxy_pool.py:69` |
| Rate limiter default | 2 req/sec | `rate_limiter.py:29` |
| Network capture body cap | 256 KB | `fetcher.py:384` |
| Render per-attempt timeout | `min(budget_ms, 20_000) ms` | `fetcher.py:398` |
| Render timeout early-exit | attempt ≥ 2 AND "TIMEOUT" in error_sig | `fetcher.py:213-217` |
| Retry attempts | 3 | `retry_policy` |
| Detector consensus boost | `+0.10` per agreeing signal (cap 0.95) | `detector.py:565` |
| LLM budget — API | 3 calls / property | generic adapter |
| LLM budget — DOM targeted | 1 call / property | generic adapter |
| LLM budget — monolithic | 1 call / property | generic adapter |
| LLM relaxed-gate text threshold | ≥ 5KB text + ≥1 rent signal | generic adapter |
| LLM field mapping cap | 20 entries / profile | `profile_updater.py` |
| LLM blocked endpoint cap | 50 entries / profile | `profile_updater.py` |
| Explored links cap | 30 entries / profile | `profile_updater.py` |
| Profile maturity — HOT | `consecutive_successes ≥ 3` | `profile_updater.py:171` |
| Profile maturity — WARM | `consecutive_successes ≥ 1` | `profile_updater.py:173` |
| Profile maturity — COLD | `consecutive_failures ≥ 3` | `profile_updater.py:175` |
| Drift — unit count drop | `< 0.7 × expected` | `drift_detector.py:32` |
| Schema gate — max rent | $50_000 | `schema_gate.py:19` |
| Schema gate — max sqft | 20_000 | `schema_gate.py:20` |
| Sanity — rent swing flag | >20% | `cross_run_sanity.py:54` |
| Sanity — rent swing high | >50% | `cross_run_sanity.py:52` |
| Sanity — sqft change flag | >5% | `cross_run_sanity.py:62` |
| v2 unit area sanity | `[150, 10_000]` sqft | `jugnu_runner._format_area` |
| Null-field-recovery confidence gate | `≥ 0.85` | `jugnu_runner._run_null_field_recovery` |
| Null-field-recovery units processed | ≤ 5 | `jugnu_runner._run_null_field_recovery` |
| F1 adapter_debugger responses tried | ≤ 3 | `jugnu_runner._process_property` |
| Max concurrent browsers | `min(RAM/250MB × 0.7, cpu×2, env)` clamped [1, 32] | `concurrency.py` |

---

## 7. Invariants & never-fail contracts

- **L1 Fetch**: `fetch()` never raises. Always returns a `FetchResult`.
- **L2 Discovery**: Scheduler yields each URL at most once per run. Frontier dedupes by URL.
- **L3 Extraction**: `detect_pms()` is fuzz-safe for None / "" / binary. `get_adapter()` never returns None (falls to GenericAdapter). LLM/Vision never fires in PMS-specific adapters.
- **L4 Validation**: Schema gate never raises on malformed input. Identity fallback uses `hashlib.sha256`, never `hash()`.
- **L5 Observability**: `emit()` swallows all exceptions. Event ledger tolerates truncated lines from prior crashes. Cost ledger and all SQLite writes use threading locks.
- **Profile updater** is wrapped in try/except in `jugnu_runner`; learning failures never block the scrape.
- **Report writers** (per-property markdown, LLM report) are best-effort — a write failure logs a warning and moves on.

---

## 8. Known issues / areas to flag for review

The items below are inconsistencies, ambiguities, or design trade-offs that surfaced while mapping the algorithm. They are intentionally listed for external feedback.

~~**`identity_fallback.py` was dead code.**~~ Fixed in PR #21.
  `compute_fallback_unit_id` is now wired into `scrape_properties._add()`
  and produces stable, rent-free `inferred_` IDs for units without a natural key.
  The function lives at `ma_poc/scripts/identity_fallback.py`.
  `compute_fallback_id` (v1, rent-volatile) is retained as a deprecated shim only.

1. **Carry-forward verdict mislabelling.** The Stage 3 branch stamps `_meta.verdict = "SUCCESS"` with `verdict_reason = "carry_forward_applied"` and returns without going through the Verdict layer. The `CARRY_FORWARD` verdict string defined in `verdict.py` is therefore never produced in this path. Downstream reporting that filters on `verdict == "CARRY_FORWARD"` will find zero matches. Expected behavior: should CF be a first-class verdict, or is "SUCCESS + reason tag" sufficient?

2. **HEAD render mode is unused.** `change_detector.decide` never returns HEAD, and the fetcher's HEAD path produces `body=None` which fails downstream extraction. The code branch for HEAD exists in the fetcher but is dead. Either remove it or wire an escalation path (HEAD → GET on 200) so `HOT profile fresh` probes could be cheaper than GET.

3. **Conditional-cache usage.** `NOT_MODIFIED` triggers carry-forward (re-emit yesterday's data), but the CF path does **not** update the conditional-cache fetched_at timestamp. A stale cache entry + 304 + CF loop could re-emit ancient data indefinitely. Is there a max-age ceiling on CF re-emission?

4. **LLM budget not strictly enforced across tiers.** Budget comments say "3 targeted API + 1 targeted DOM + 1 monolithic", but I didn't see a single shared counter that blocks the next tier if budget is exhausted — each tier has its own cap. If all three are called on the same property, total LLM calls could hit 5. Verify whether this is intentional.

5. **Two-pass PMS detection.** Detector runs once offline, then again with HTML — if the second is more confident it wins. But events are only emitted for the final detection. The intermediate signals that got overridden aren't logged, making misrouting diagnosis hard.

6. **Confirmation-check asymmetry.** Post-capture confirmation demotes to `"unknown"` when URL-predicted adapter can't match any response body, but there's no symmetric check for under-detection (URL said "unknown" but response bodies match a known adapter). Self-learning eventually catches this via blocked_endpoints → known_endpoints, but the first run loses units.

7. **Sanity gate flags without rejecting.** `rent_swing_>50%` is noted but the unit is still accepted. Against a stale history, a legitimate rent change can masquerade as an error. Reverse case: a $10 → $10000 transcription error passes validation. Consider whether swing flags should escalate to rejection at some threshold (e.g., >200%).

8. **Drift detector depends on `expected_count`.** Derived from `profile.confidence.last_unit_count`. First run has no expected count, so drift never fires on a COLD→WARM transition. Only HOT profiles get strong drift protection.

9. **F1 adapter_debugger runs once per property per run.** If the first diagnosis is a low-confidence guess, the tighter signal from responses 2 and 3 is never used. Consider running all 3 and picking the highest-confidence diagnosis.

10. **F2 null-field-recovery hard-codes 5 units.** Properties with 50 units where 30 have null rent_low will only get 5 rescued. Either process all nulls (cost permitting) or sample deterministically.

11. **Incremental merge only keys on `_meta.canonical_id`.** Records without canonical_id are appended without dedup, so two runs that failed to resolve identity for the same property will write two entries.

12. **Property record fallbacks.** `_format_v1` "Unique ID" falls through `apartmentid | Unique ID | canonical_id`. If the CSV has both `apartmentid="ABC"` and `Unique ID="XYZ"`, `apartmentid` wins. Verify this matches the CSV contract with RealPage.

13. **Profile maturity transitions only check consecutive counters.** A HOT profile that fails once then succeeds twice stays WARM (not HOT) because the third success is needed. Consider whether a probationary path (HOT with one recent failure) is useful.

14. **`skip_llm = (detected.pms != "unknown")` is a strong default.** Any detected PMS adapter gets no LLM assistance even when it produces zero units — the relaxed gate catches some but not all cases (requires 5KB text). A quieter site with 2KB of rent data still won't get LLM help.

15. **Rent-sanity lower bound $200/month.** Rules out some student-housing markets, room rentals, and subsidised units. Is a per-market floor needed?

---

## 9. Suggested review prompts

When submitting this file for feedback, questions worth asking:

- **Correctness:** Do the first-match-wins rules in Stage 1, 6b, 8, and 9 ever conflict in practice? Is there a property fingerprint that can hit multiple rules in an unintended order?
- **Coverage:** Are there known PMS patterns (embedded leasing portals, fully-iframed sites, widget-only sites) that would slip past every stage as zero-units without fitting any specific failure mode?
- **Cost vs. accuracy:** Is the LLM budget (3+1+1 per property) the right trade-off given the 500-property scale? Could cheaper heuristic-first tiers cover more ground?
- **Determinism:** Profile replay + deterministic tiers are meant to minimize cost on repeat runs. Are there paths (particularly Stage 3 CF and Stage 11 F2) where repeated runs can drift silently?
- **Observability:** Does the verdict labelling (`SUCCESS` for CF, `PARTIAL` for majority-rejected) match what a human reviewer would expect when triaging a run report?
- **Schema v2:** The `-1` sentinel for area and `None` for beds/baths defeat collapse-to-zero bugs but require downstream consumers to handle two "missing" states. Is this the right contract?
- **Self-learning stability:** Can a single bad run (e.g., site outage producing 0 units) demote a HOT profile back to COLD and force a full LLM re-learn on the next run? What guardrails exist against transient demotion?
