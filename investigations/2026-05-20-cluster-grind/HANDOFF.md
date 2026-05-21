# Session Handoff — 2026-05-20 Cluster Grind + Path B/C Architecture

> **Read this first** before continuing the canary-iterate work. Every fix in
> this document is shipped + tested + pushed. The next session should be able
> to canary-rebuild at HEAD and measure cumulative recovery without re-doing
> anything below.

---

## Branch + push status

- **Worktree**: `/Users/ankur/PropAi-main/.claude/worktrees/angry-murdock-c19e06`
- **Local branch**: `claude/portal-hop-may19`
- **Push refspec**: `git push origin claude/portal-hop-may19:fix/resolver-path-patterns-may13`
- **Remote branch (canonical)**: `origin/fix/resolver-path-patterns-may13`
- **Current tip**: `dc9b548`
- **Pre-session baseline**: `0956c59` (cluster #3 G5+Knock follow-up, prior session)

> ⚠️ `origin/claude/portal-hop-may19` diverged at `726ff6a` — do NOT push there.
> Always use the explicit refspec above.

```bash
# Verify push state
git log --oneline origin/fix/resolver-path-patterns-may13 -1
# → should show dc9b548
```

---

## Test suite — cumulative delta this session

| Snapshot | Tests pass |
|---|---|
| Pre-session baseline | 1062 |
| After all session commits | **1206** |

Net: **+144 new tests** across 14 commits. Run the full PMS + validation suites:

```bash
python -m pytest ma_poc/tests/pms/ ma_poc/tests/validation/ -q
# Expected: 1206 passed, 2 skipped
```

Pre-existing failures (verified unrelated by bisect vs `0956c59`):
- `test_extract_cross_page_link_hop.py::test_h5_visited_urls_dedupe`
- `test_available_date_parsing.py::test_dom_card_label_prefixed_dates_recovered[...May 19...]` (date rollover)
- `test_shard_safeguards.py::test_fix1_jugnu_timeout_handler_emits_failed_record`
- `test_phase_a_correctness.py::test_a_no_bare_imports_in_scraper`
- 3× `test_scripts_layout.py`

---

## Commits in this session (oldest → newest)

| # | Commit | Title |
|---|---|---|
| 1 | `727b31c` | Cluster #5: SightMap embed-code discovery beyond `<iframe>` |
| 2 | `a623649` | Path B Piece 1: empty-exit label registry |
| 3 | `d7833d8` | Path B Piece 2: detector ranked candidates |
| 4 | `822f497` | Cluster #5 narrowing: skip SightMap URLs in JSON-value position |
| 5 | `50387a0` | Path B Piece 3a: empty-exit retry telemetry (telemetry-only) |
| 6 | `98cc5ce` | Path B Piece 3b: enable empty-exit retry |
| 7 | `337ebaa` | Cluster #4: bootstrap profile BEFORE L1 fetch |
| 8 | `99e71a2` | Path C: extend retry to quality-gate failures |
| 9 | `5a7a676` | Cluster #6: OneSite emits proper empty-exit labels |
| 10 | `bffb24e` | Cluster #7: Equity adapter emits proper empty-exit labels |
| 11 | `8f92568` | Path C extension: no_rent + no_area + plan-level fallback |
| 12 | `2cb1569` | Cluster #5 RentCafe sub-cluster: `/residentservices/` slug |
| 13 | `bf5879f` | Cluster #4 soft-404 recovery |
| 14 | `dc9b548` | Cluster #4 (c): captcha-widget on real page is not a bot wall |

---

## Architecture summary — Path B + Path C

The 1,429-failure cohort grind surfaced a deeper pattern: every adapter was
already self-reporting "ran but produced nothing usable" via structured
`tier_used` labels, but no consumer was acting on those signals. The
orchestrator's `next_tier_requested` flag was emitted (in
`ma_poc/validation/orchestrator.py:209`) and only logged as a warning at
[`jugnu.py:907`](ma_poc/scripts/runners/jugnu.py).

Path B + C wire this end-to-end:

```
┌─────────────────────────────────────────────────────────────┐
│ 1. ROUTING (detector picks PMS)                             │
│    detect_pms(url, csv_row, html)                           │
│    Sources: URL host > URL ext > HTML markers > mgmt prior  │
└─────────────────────────────────────────────────────────────┘
              │
              ▼
┌─────────────────────────────────────────────────────────────┐
│ 2. FIRST DISPATCH                                            │
│    adapter = get_adapter(pms);  adapter.extract(page, ctx)  │
└─────────────────────────────────────────────────────────────┘
              │
              ▼
┌─────────────────────────────────────────────────────────────┐
│ 3. PATH B/C RETRY (ma_poc/pms/scraper.py ~line 862)         │
│    Trigger ladder (first match wins):                       │
│      • empty_exit:    is_empty_exit(tier) AND not units     │
│      • quality_gate:  units AND no physical dimension       │
│      • no_rent:       units AND dims OK AND no rent signal  │
│      • no_area:       units AND dims+rent OK AND no area    │
│    On trigger: detect_pms_candidates(exclude={tried})       │
│      → re-dispatch, max PATH_B_MAX_RETRIES=2                │
│    Win condition: units AND dims AND rent_signal            │
│    Plan-level fallback on retry-fail:                       │
│      restore baseline, tag tier _PLAN_LEVEL,                │
│      result['_verdict_quality'] = 'SUCCESS_PLAN_LEVEL'      │
└─────────────────────────────────────────────────────────────┘
              │
              ▼
┌─────────────────────────────────────────────────────────────┐
│ 4. F2 LLM RESCUE (existing; unchanged)                       │
└─────────────────────────────────────────────────────────────┘
              │
              ▼
┌─────────────────────────────────────────────────────────────┐
│ 5. UNIVERSAL EMBED RECOVERY + STEP 8 GENERIC FALLBACK       │
└─────────────────────────────────────────────────────────────┘
```

### Path B/C feature flags (env vars)

| Env var | Default | Effect |
|---|---|---|
| `PATH_B_RETRY_ENABLED` | `"1"` | Set to `"0"` / `"false"` / `"no"` to fall back to telemetry-only (emits `RETRY_WOULD_DISPATCH`, doesn't re-dispatch) |
| `PATH_B_MAX_RETRIES` | `"2"` | Maximum retry attempts (excluding the initial dispatch) |

### Event kinds (observability)

| EventKind | When | Payload keys |
|---|---|---|
| `RETRY_WOULD_DISPATCH` | Telemetry-only mode hits an empty-exit | `previous_pms, previous_tier, empty_exit_reason, trigger_reason, next_pms, next_confidence, remaining_candidates` |
| `RETRY_DISPATCHED` | Real retry attempt fires | `attempt, previous_pms, previous_tier, empty_exit_reason, trigger_reason, next_pms, next_confidence` |
| `RETRY_SUCCESS` | Retry attempt recovered units | `attempt, previous_pms, previous_tier, trigger_reason, won_pms, won_tier, unit_count` |

### Path B/C critical files

- `ma_poc/pms/empty_exit.py` — the registry. `is_empty_exit()`,
  `empty_exit_reason()`, `_EMPTY_EXIT_SUFFIXES`, `_EMPTY_EXIT_VERDICTS`.
  Live-grep contract test in `ma_poc/tests/pms/test_empty_exit.py` walks every
  adapter source and ensures new labels can't slip past the registry silently.
- `ma_poc/pms/detector.py` — `detect_pms_candidates(url, csv_row, page_html,
  exclude, max_candidates)` + `_iter_html_markers()` generator (the refactor
  of the legacy `_detect_html_markers` first-match return). The legacy
  one-call wrapper is preserved for back-compat.
- `ma_poc/pms/scraper.py` ~line 862 — the inline retry hook. Has both
  modes (telemetry-only when `PATH_B_RETRY_ENABLED=0`, full retry by default).
  Source-grep contract test in `ma_poc/tests/pms/test_path_b_retry_telemetry.py`
  pins every symbol the hook depends on.
- `ma_poc/validation/schema_gate.py` — `property_has_rent_signal()`,
  `property_has_area_signal()`, plus `_has_rent` / `_has_area` predicates.

### Path B/C key design docs

- `investigations/2026-05-20-path-b-design/DESIGN.md` — the architecture
  proposal (Pieces 1 / 2 / 3a / 3b).

---

## Cluster status (post-session)

| # | Cluster | Status | Commits |
|---|---|---|---|
| 1 | `SYNDICATION_ONLY_WIX` → AppFolio tenant | ✅ shipped pre-session | `23ad093` |
| 2 | `NOT_ENCORESKYLINE_TEMPLATE` (Jonah-gate) | ✅ shipped pre-session | `21c5607` |
| 3 | `G5_EMPTY` / `G5_NO_URN` (+ Knock co-resident) | ✅ shipped pre-session | `d99da26` + `0956c59` |
| 4 | `generic:no_body_short_circuit` — CF-403 sub-pattern | ✅ this session | `337ebaa` |
| 4 | `generic:no_body_short_circuit` — soft-404 sub-pattern | ✅ this session | `bf5879f` |
| 4 | `generic:no_body_short_circuit` — shea-style 200-OK | ✅ this session | `dc9b548` |
| 5 | `SIGHTMAP_SHAPE_REJECTED` | ✅ this session | `727b31c` + `822f497` |
| 5 | `RENTCAFE_SHAPE_REJECTED` (`/residentservices/` variant) | ✅ this session | `2cb1569` |
| 6 | `TIER_1_API_ONESITE` | ✅ this session | `5a7a676` |
| 7 | Equity REIT (Essex was no-data, NOT a bug) | ✅ this session | `bffb24e` |

---

# Per-fix detail

Each entry below tells the next session: **what** changed, **why**, **where**,
**how to verify**, and **what's NOT covered**.

---

## Path B Piece 1 — empty-exit label registry (`a623649`)

### Files
- `ma_poc/pms/empty_exit.py` (NEW, 194 LOC)
- `ma_poc/tests/pms/test_empty_exit.py` (NEW, 286 LOC, 59 tests)

### What
Pure-Python module defining which `tier_used` strings count as
"adapter ran, produced nothing usable — try a different PMS":

- **Suffix patterns**: `_EMPTY`, `_NO_URN`, `_NO_RESPONSE`, `_SHAPE_REJECTED`,
  `_PARSE_FAILED`, `_AMENITIES_ONLY`, `_API_ERROR`, `_NO_PLAN`,
  `_NO_PLAN_LINKS`, `_RESEARCH_BLOCKED`
- **Verbatim labels**: `NOT_ENCORESKYLINE_TEMPLATE`,
  `ENCORESKYLINE_NO_PLAN_LINKS`, `SYNDICATION_ONLY_WIX`,
  `SYNDICATION_ONLY_SQUARESPACE`
- **Exclusions**: `TIER_4_LLM_*` prefix — LLM is already the last-resort tier;
  retrying from it has nowhere to go

### Public API
```python
from ma_poc.pms.empty_exit import is_empty_exit, empty_exit_reason

is_empty_exit("TIER_1_API_G5_EMPTY")  # True
is_empty_exit("TIER_1_API_KNOCK")     # False — bare success
is_empty_exit("TIER_4_LLM_DOM_EMPTY") # False — LLM tier excluded
empty_exit_reason("TIER_1_API_G5_EMPTY")           # "_EMPTY"
empty_exit_reason("NOT_ENCORESKYLINE_TEMPLATE")    # "NOT_ENCORESKYLINE_TEMPLATE"
```

### Key safety net
`test_every_literal_empty_exit_in_source_is_classified` + sibling tests
walk every `ma_poc/pms/adapters/*.py` file and assert every empty-exit-shaped
`tier_used = ...` assignment is recognized by the registry. If a future
adapter adds a new label without updating the registry, CI fails loudly.

---

## Path B Piece 2 — detector ranked candidates (`d7833d8`)

### Files
- `ma_poc/pms/detector.py` — refactor + new function
- `ma_poc/tests/pms/test_detector.py` — 9 new tests (76 total)

### What

1. **Internal refactor**: `_detect_html_markers(html)` → `_iter_html_markers(html)` generator.
   27 `return` statements mechanically converted to `yield` (via an
   AST-verified script). `_detect_html_markers` is preserved as a thin
   `next(_iter_html_markers(html), None)` wrapper — full back-compat for
   existing callers.

2. **New public API**: `detect_pms_candidates(url, csv_row, page_html,
   exclude, max_candidates) -> list[DetectedPMS]`
   - Walks the same priority chain as `detect_pms` (CSV override > host
     > URL ext > HTML markers > mgmt prior)
   - Yields ALL matching PMSs in priority order, deduped
   - Honors `exclude` set (used by retry to skip already-tried PMSs)
   - Caps at `max_candidates` (default 4)
   - Never raises (defensive try/except around the whole walk)
   - `unknown` PMS never appears in the candidate list

3. **Back-compat guarantee** (pinned by
   `test_detect_pms_candidates_first_equals_detect_pms`):
   `detect_pms(url, csv_row, html).pms == detect_pms_candidates(...)[0].pms`
   for every non-unknown case.

### Real-world routing examples (live-verified)
- Flatiron (G5+Knock+SecureCafe): `[knock, rentcafe]`
- Alta (G5+Knock only): `[knock]`
- Griffis (SightMap+Knock): `[sightmap, knock]`

---

## Path B Piece 3a → 3b — retry hook in scraper.py (`50387a0` → `98cc5ce`)

### Files
- `ma_poc/observability/events.py` — 3 new event kinds
- `ma_poc/pms/scraper.py` ~line 862 — inline retry hook
- `ma_poc/tests/pms/test_path_b_retry_telemetry.py` (NEW) — 24 tests covering
  predicate + retry behavior + source-grep contract

### What
Inline hook in `scrape_jugnu` right after the first adapter dispatch.

- `PATH_B_RETRY_ENABLED=0` → telemetry-only (emits `RETRY_WOULD_DISPATCH`)
- `PATH_B_RETRY_ENABLED=1` (default) → real retry up to `PATH_B_MAX_RETRIES=2`

Hook updates `result['_adapter_used']` + `result['_detected_pms']` on winning
retry so downstream reporting + profile-updater see the correct winner.

Exception during retry: recorded on `fallback_chain` as
`retry_failed:<pms>:<ExcType>`, swallowed. Retry must never block scrape.

---

## Path C — quality-gate retry trigger (`99e71a2`)

### Files
- `ma_poc/pms/scraper.py` — extended the retry predicate
- `ma_poc/tests/pms/test_path_b_retry_telemetry.py` — 5 new tests (29 total)

### What
Adds a second trigger to the retry loop: when the adapter returned units
but they all fail the **dimension** gate
(`property_passes_quality_gate(units) == False`), retry. Event payload
carries `trigger_reason='quality_gate'` to split from `empty_exit`.

Win condition: retry result must have units AND pass the dimension gate.

---

## Path C extension — `no_rent` + `no_area` + plan-level fallback (`8f92568`)

> **Most important architectural change of the session.** Per the
> `project_jsonld_recovery_2026-05-20.md` memo + explicit user constraint:
> "rent or area missing for all units is biggest quality gap for path c ...
> getting floor plan level data is okay but just should be flagged and one
> another path should be retried to see if there is scope to get unit, if
> unit then pick that (quality check) else floor plan".

### Files
- `ma_poc/validation/schema_gate.py` — 2 new predicates + private helpers
- `ma_poc/pms/scraper.py` — extended Path C trigger + plan-level fallback
- `ma_poc/tests/validation/test_schema_gate_unit_quality.py` — 13 new tests
- `ma_poc/tests/pms/test_path_b_retry_telemetry.py` — 6 new tests, helper updated

### New predicates
```python
from ma_poc.validation.schema_gate import (
    property_has_rent_signal,   # >=threshold (default 0.5) rows have rent
    property_has_area_signal,   # >=threshold rows have sqft/area
)
```

Tolerant of int / float / string-currency forms. Rejects `None`, `0`, `""`,
`bool`, `"call for pricing"`, the `-1` sentinel.

### Extended trigger ladder (in order)
1. `empty_exit` — `is_empty_exit(tier) and not units`
2. `quality_gate` — `units and not property_passes_quality_gate(units)`
3. **`no_rent`** — `units and dims_pass and not rent_signal` ← NEW
4. **`no_area`** — `units and dims_pass and rent_pass and not area_signal` ← NEW

### Tightened win condition
Retry winner must satisfy **units + dimension gate + rent signal**.
Retries producing more rent-less rows are NOT promoted.

### Plan-level fallback
When the baseline triggered Path C AND all retries failed AND the baseline
had units, restore the baseline with:

- `adapter_result.tier_used = "{original_tier}_PLAN_LEVEL"`
- `result["_verdict_quality"] = "SUCCESS_PLAN_LEVEL"`
- `result["_plan_level_reason"] = "no_rent"` (or `"quality_gate"` / `"no_area"`)

Implementation uses a separate `_current_result` for trigger evaluation so the
baseline isn't mutated during retry iteration.

### Coverage of the 298-prop JSON-LD ALL-fail bucket

| Sub-bucket | % of 298 | Path |
|---|---|---|
| RentCafe-Nestin per-plan | 63% | Path C `no_rent` → retry. Recovery requires the Nestin adapter (NOT YET BUILT — see "Pending" below) |
| RentCafe SecureCafe portal | 26% | Retry routes to RentCafe → SecureCafe drill-down (existing adapter) |
| Knock-by-domain | 3% | Retry → Knock adapter if it's the next candidate |
| Blueberry plan-cards-only | 3% | No co-resident PMS → plan-level fallback honestly flags it |
| JCM custom + dead URL | 6% | Plan-level fallback OR genuine FAILED |

---

## Cluster #4 — `generic:no_body_short_circuit` (64 props)

Three sub-patterns identified; **2 fixed, 1 pending**.

### Sub-pattern (a): Cloudflare 403 — fixed by `337ebaa`

#### Files
- `ma_poc/fetch/__init__.py` — `fetch(task, profile=None)` accepts new arg
- `ma_poc/scripts/runners/jugnu.py` — bootstrap profile in H4 fallback
- `ma_poc/tests/fetch/test_top_level_fetch_profile_arg.py` (NEW, 3 tests)
- `ma_poc/tests/scripts/test_jugnu_bootstrap_before_fetch.py` (NEW, 3 tests)

#### What
The tier escalator at `ma_poc/fetch/fetcher.py:197` gates on
`profile is not None`. First-run properties had `profile=None` at fetch
time (bootstrap happened only AFTER fetch at L3), so CF-403 sites never
got residential proxy escalation.

Fix:
1. Extend top-level `fetch()` to accept + forward `profile`.
2. In `jugnu.py` H4 fallback, bootstrap a COLD profile when
   `profile_for_dispatch is None`, then call
   `jugnu_fetch(task, profile=profile_for_dispatch)`.
3. L3 step still bootstraps as safety net — idempotent via the same
   `profile_store.get_profile(...)` call.

#### Live evidence
- `tidesateastchase.com`: HTTP 403 `cf-mitigated: challenge`
- `liveatpalmhaven.com`: HTTP 403 `cf-mitigated: challenge`

Matches durable memory: "9,272 bot-block events, 0 residential fetches" —
profile was None for every property on the clean-run canary.

### Sub-pattern (b): soft-404 with content — fixed by `bf5879f`

#### Files
- `ma_poc/pms/scraper.py` ~line 2748 — soft-404 recovery branch
- `ma_poc/tests/pms/test_soft_404_recovery.py` (NEW, 9 tests)

#### What
Some marketing sites return HTTP 404 with full apartment content + nav
links. Examples (live-probed):
- `liveatcrossroadsranch.com/home`: 404 + 59 KB body with
  `/apartments/tx/houston/floor-plans` link
- `olympusproperty.com/.../tacara-at-westover-hills/`: 404 + 189 KB Duda body

The fetcher classifies as `DEAD_URL` → `scrape_jugnu` short-circuits to
`generic:no_body_short_circuit`.

Fix: when `outcome=DEAD_URL` AND body ≥ 10 KB AND body contains one of these
inventory nav markers, **skip the short-circuit**:

```
/floor-plans, /floorplans, /availability, /available-units,
/availableunits, /apartments/, sightmap.com/embed/, rentcafe.com,
knockdoorway
```

Records `_soft_404_recovery=True` + `_soft_404_status` on the result dict so
reports distinguish "soft-404 recovered" from "genuine 200 OK".

#### NOT addressed: actual unit recovery
Skipping the short-circuit unlocks the pipeline; actually getting units
still requires the existing link-hop to navigate to `/floor-plans` (or
wherever the embed lives) and the SightMap / RentCafe adapter to extract
there. With clusters #5 + Path C in place, the pipeline is now equipped.

### Sub-pattern (c): shea-style 200-OK — fixed by `dc9b548`

#### Files
- `ma_poc/fetch/captcha_detect.py` — split `_FINGERPRINTS` into CHALLENGE-only
  vs WIDGET-dual-use; added `body_size` kwarg
- `ma_poc/fetch/response_classifier.py` — added `body_size` kwarg to
  `classify()`; threaded to both `looks_like_captcha` call sites
- `ma_poc/fetch/fetcher.py` — compute + forward `body_size` at the two
  full-body `classify()` call sites
- `ma_poc/tests/fetch/test_captcha_detect.py` — 7 new tests (14 total)

#### Live evidence (verified end-to-end on real shea bytes)

shea body has `g-recaptcha` string at offset 953 — inside a JS function
called `wcagFix()` that adds a WCAG-accessibility label to the
reCAPTCHA textarea for the contact-form widget. The widget is for
legitimate user-form submission, NOT a challenge interstitial.

| Path | `is_captcha` | outcome |
|---|---|---|
| OLD (no body_size) | `(True, 'recaptcha')` | `BOT_BLOCKED / CAPTCHA_RECAPTCHA` → `no_body_short_circuit` |
| NEW (body_size=150194) | `(False, None)` | `OK` → extraction proceeds |

#### Fix details

Split `_FINGERPRINTS` into two dicts:

- `_CHALLENGE_ONLY_FINGERPRINTS` (always trusted): cloudflare
  (`challenge-platform`, `__cf_chl_`, `Just a moment...`) + perimeterx
  (`_pxhd`, `PerimeterX`)
- `_WIDGET_DUAL_USE_FINGERPRINTS` (only trusted when body_size ≤ 30 KB):
  recaptcha (`g-recaptcha`, `www.google.com/recaptcha`) + hcaptcha
  (`hcaptcha.com`, `h-captcha`)

The 30 KB threshold (`_LIKELY_CHALLENGE_BODY_MAX`) sits well above
typical challenge-interstitial sizes (3–15 KB) and well below real
apartment page sizes (50–300 KB).

#### Back-compat
`looks_like_captcha` keeps the same single-positional-arg signature for
existing callers; `body_size` is an optional kwarg. When omitted, falls
back to `len(body)` — same outcome as today for the 7 existing tests
(all use small body fixtures).

---

## Cluster #5 — SHAPE_REJECTED (43 props)

Two sub-clusters: SightMap (majority) + RentCafe (`/residentservices/` variant).

### Sub-cluster: SightMap — `727b31c` + `822f497`

#### Files
- `ma_poc/pms/adapters/sightmap.py` — broadened then narrowed regex
- `ma_poc/tests/pms/adapters/test_sightmap.py` — 9 new tests (+6 broaden, +3 narrow)

#### What
Three real-world SHAPE_REJECTED properties (live-verified):

| Site | Embed URL in DOM as |
|---|---|
| griffisresidential | `<a data-src="https://sightmap.com/embed/m9pzd4ezvk1">` (Fancybox) |
| cambridgeondevonshire | `var EngrainedUrl = 'https://sightmap.com/embed/zlpoyde8wg4';` (JS) |
| soltrafirewheel | `<a data-src=".../embed/rxwjjkedw1e">` (Fancybox) |

The old `_SIGHTMAP_IFRAME_RE` required `<iframe src=...>`. Fix: replaced
with `_SIGHTMAP_EMBED_URL_RE` that matches `sightmap.com/embed/{code}` in
any DOM context.

#### Narrowing follow-up (`822f497`)
The broadening caused a regression in
`test_portal_hint_survives_full_scrape_chain` — it now matched URLs inside
JSON-value position (`"embed_url":"https://sightmap.com/embed/..."` config
blobs). Skip JSON-position matches (preceded by `":` with optional
whitespace) so config-blob URLs go through the generic adapter's portal-hint
path as intended. Real cluster-5 properties carry the URL in HTML attribute
or JS-variable position, not JSON; verified the narrowing doesn't lose
them.

#### End-to-end verification (real HTML, curl_cffi)
- griffisresidential: code recovered → 20 units parsed (vs 20 main strict)
- cambridge: 10 units (vs 10 main strict)
- soltrafirewheel: 110 units (vs 126; temporal)

### Sub-cluster: RentCafe `/residentservices/` — `2cb1569`

#### Files
- `ma_poc/pms/adapters/rentcafe.py` — broadened `_SECURECAFE_URL_RE`
- `ma_poc/tests/pms/adapters/test_rentcafe.py` — 5 new tests

#### What
Live-probed top-3 RentCafe SHAPE_REJECTED Bucket-A properties:

| Property | DOM only has | `/onlineleasing/<slug>/availableunits.aspx` |
|---|---|---|
| cityridgedc.com | `/residentservices/city-ridge-clo/userlogin.aspx` | **59 AvailUnitRow** (curl_cffi chrome120) |
| thedylanchicago.com | `/residentservices/160-n-morgan/userlogin.aspx` | **2 AvailUnitRow** |
| uncommondevelopers | `/onlineleasing/the24/guestlogin.aspx` (matched) | 0 (legit no availability) |

Marketing sites only link to resident-services portal. The parallel
`/onlineleasing/<slug>/availableunits.aspx` URL exists on the same SecureCafe
tenant with the same slug.

Fix: extended `_SECURECAFE_URL_RE` to accept both `/onlineleasing/` AND
`/residentservices/` with the same `(sub, slug)` named groups.
`_find_securecafe_base` is unchanged — it already unconditionally
synthesizes the `/onlineleasing/<slug>` URL from the captured slug.

---

## Cluster #6 — TIER_1_API_ONESITE (30 props) — `5a7a676`

### Files
- `ma_poc/pms/adapters/onesite.py` — three-way label split on failure paths
- `ma_poc/tests/pms/adapters/test_onesite.py` — 4 new tests (12 total)

### What
Live-probed top-3 OneSite numeric-subdomain hosts via Chrome MCP:

| Property | bodyLen | OneSite floorplans API fired? |
|---|---|---|
| 9141461.onlineleasing.realpage.com (toapts) | 386 | No (page is OLL widget shell) |
| 8921439.onlineleasing.realpage.com (riverpointe) | similar | No |
| 9009994.onlineleasing.realpage.com (acadia) | similar | No |

Chrome MCP confirmed the page loads `property.onesite.realpage.com/ollr/widgetLoader.js`
+ `leasing.realpage.com/oll/apploader.js` but never fires
`api.ws.realpage.com/v2/property/.../floorplans` in the canary settle
window.

The OneSiteAdapter set `tier_used = "TIER_1_API_ONESITE"` at init line
151 and never overwrote it on failure paths — Path B/C didn't recognize
it as an empty exit, and Step 8 generic fallback didn't fire because
some downstream paths gated on the success-looking label.

Fix: split outcomes by what was captured:
- `TIER_1_API_ONESITE` — real units parsed (unchanged)
- `TIER_1_API_ONESITE_EMPTY` — responses captured, validity rejected
  all OR floorplans list empty
- `TIER_1_API_ONESITE_NO_RESPONSE` — no RealPage-shaped responses
  captured at all (cluster #6 OLL-shell case)

Both `_EMPTY` and `_NO_RESPONSE` are already in the empty-exit registry —
test_onesite.py pins this contract. Existing 293707 happy-path fixture
still passes (real floorplan data → bare label).

---

## Cluster #7 — Equity REIT (25 props; Essex was no-data) — `bffb24e`

### Files
- `ma_poc/pms/adapters/equity.py` — same three-way label split as #6
- `ma_poc/tests/pms/adapters/test_equity.py` (NEW, 8 tests; first dedicated Equity test file)

### What
Same bug shape as cluster #6 — `EquityAdapter` set bare `TIER_1_API_EQUITY`
at init, never overwrote on failure. Fix mirrors `5a7a676`:
- `TIER_1_API_EQUITY` — real ea5-unit blocks parsed
- `TIER_1_API_EQUITY_EMPTY` — blocks parsed, validity rejected
- `TIER_1_API_EQUITY_NO_RESPONSE` — no ea5-unit blocks in HTML

### Essex re-classification
The original cluster #7 memo bundled 27 Essex props with the 25 Equity ones.
**Honest re-examination of the worklist**: all 27 Essex props are in
**Bucket C** (`feature_tier=generic:no_body_short_circuit` AND `main_tier`
matches → main also has no data). They're genuine no-data territory, not
an adapter bug. Original "52 prop" cluster size was misleading.

---

# Follow-on session ship list (2026-05-20, after the cluster grind above)

Five additional commits landed on `fix/resolver-path-patterns-may13` after
the 13 above, clearing items (1)–(3) of the original Pending list AND
adding two new items derived from the
`project_tier3_dom_recovery_2026-05-20.md` probe memo (TIER_3_DOM
ALL_fail bucket: 200 props, 40% RealPage misroutes + 16% brand-CMS URL
pattern + 20% dead URLs). Test suite delta: 1206 → **1509 passed**
(+303 new tests across the 5 commits + cumulative coverage).

| # | Commit | Title | Bucket impact |
|---|---|---|---|
| 1 | `c01d4af` | Verdict-honesty downgrade: SUCCESS → SUCCESS_PLAN_LEVEL | ~1,031 props honest-labelled (clears the inflated-SUCCESS audit) |
| 2 | `4961e2e` | RentCafe-Nestin per-plan DOM recovery | ~225 props / ~4,400 strict units (JSON-LD + TIER_3_DOM + TIER_MERGED overlap) |
| 3 | `e69c208` | Knock-by-domain resolver: `/v1/profile?domain=` fallback | ~~~21 props / ~315 strict units~~ — **0/4 in live probe** (Aspen Square pages have no static Knock signals; community hash is JS-rendered; resolver fails gracefully, no canary regression). See "Pre-canary probe — Knock-by-domain limitation" below. |
| 6 | `bd3c606` | RentCafe-Nestin: 3 pre-canary probe bug fixes (table prefix-leak, card regex, Stonewater applyGA layout) | Hardens the +225 props from row 2 — without these, Chatwell/Hayden ship polluted unit numbers, Altair ships bogus units, Stonewater extracts 0. |
| 7 | `de8632e` | RentCafe-Nestin: accept absolute hrefs from Playwright-rendered HTML | Without this, the recovery emits 0 units when running through ``jugnu`` (only worked in standalone unit tests). Unblocks the entire +225 props from row 2 in canary. |
| 8 | `9355711` | RentCafe-Nestin: clear stale homepage CF cookies before detail probes | Without this, CF-protected Nestin sites (Stonewater, Chatwell, etc.) return 13/13 403 on detail-page fetches because the homepage's path-scoped ``cf_clearance`` cookie poisons subsequent requests. With it, e2e local probe goes from 0→24 units (Stonewater) and 0→7 units (Chatwell). |
| 4 | `df569b5` | Detector: Engrain widget signal → sightmap | ~56 props / ~1,400 strict units (RealPage+SightMap misroutes in TIER_3_DOM) |
| 5 | `d58d624` | Generic-DOM: brand-CMS URL discovery | ~46 props / ~920 strict units (Lincoln, McKinley, HG Living, MG Properties brand sites) |

**Combined expected canary impact** (revised after the 2026-05-20 pre-
canary probe): **~327 properties / ~6,685+ strict-quality unit rows**
(was ~348 / ~7,000+ before the Knock-by-domain limitation was
discovered). The Aspen Square cluster (~21 props / ~315 strict units)
slips to a later session pending a Playwright-based fix or HAR-replay.
The bulk still recovers via the RentCafe-Nestin per-plan adapter (now
hardened by the `bd3c606` post-probe bug fixes) plus existing-adapter
detector enhancements (Engrain signal, brand-CMS URL discovery).

## Per-commit detail (follow-on)

### `c01d4af` Verdict-honesty downgrade

Files: `ma_poc/reporting/verdict.py`,
`ma_poc/scripts/runners/jugnu.py`,
`ma_poc/tests/reporting/test_verdict_honesty_downgrade.py`

`compute()` gains two new params, both back-compat default-None:

- `verdict_quality_override`: when the scraper.py Path C plan-level
  fallback stamps `result["_verdict_quality"]="SUCCESS_PLAN_LEVEL"`,
  this override downgrades an otherwise-SUCCESS verdict. Reason:
  `path_c_plan_level_fallback`.
- `units`: the final unit list. When provided AND verdict would be
  SUCCESS, applies the verdict-honesty downgrade — if every unit has
  `inferred_*` UID prefix → reason `all_inferred_uids (N units)`; if
  `property_has_rent_signal(units)` is False → reason
  `no_rent_signal (N units)`.

FAILED_*, DEAD_URL, CARRY_FORWARD, PARTIAL are unaffected — only
otherwise-SUCCESS verdicts get downgraded. jugnu.py call site updated
to pass both new args. 16 tests covering all branches + back-compat.

### `4961e2e` RentCafe-Nestin per-plan DOM recovery

Files: `ma_poc/pms/adapters/_rentcafe_nestin.py` (NEW, 363 LOC),
`ma_poc/pms/adapters/rentcafe.py`,
`ma_poc/tests/pms/adapters/test_rentcafe_nestin.py` (NEW, 470 LOC).

New standalone module that scrapes per-plan detail pages on the
`/floorplans/{plan-slug}` URL pattern. Signal: `resource.rentcafe.com`
image CDN host in rendered HTML. Two layouts handled:

- **Layout A1 (`<table>` shape)** — Stonewater, Chatwell, Hayden:
  parse `<th>` headers + `<tbody>` rows. Columns by `data-label` or
  positional index.
- **Layout A2 (card / div-block shape)** — Altair, Hampton, LINQ,
  Hampton Meridian: scan repeated DOM blocks containing
  `APARTMENT[:#]\s*<value>` text; pull rent + optional Date Available
  from the nearest enclosing container.

**Rent regex correction** baked in (per the JSON-LD memo): now handles
sub-$1000 and decimals (`$823.00`, `$1,099.00`). Prior
`\$[1-9][0-9],?[0-9]{2,3}` missed Chatwell's $823 entirely.

**Real unit numbers preserved** (no `inferred_` prefix): `#1120` →
`1120`, `#4112-3` → `4112-3`, `#B306` → `B306`.

**Discovery flow**:
1. Scan rendered HTML for `<a href="/floorplans/{slug}">` per-plan links
2. If none on landing, fetch `/floorplans` index via `probe_get`
   (curl_cffi + optional residential proxy + Web-Unlocker escalation)
   and re-scan
3. For each detail URL, fetch + parse via A1-table → A2-card cascade
4. Emit `make_unit_dict` rows with `tier_used="TIER_1_DOM_RENTCAFE_NESTIN"`

**Wiring**: `RentCafeAdapter.extract()` calls the new recovery after the
XHR / WP-probe / SecureCafe / hosted-table tiers but before the
failure-classification fallback. Layout B (Blueberry-shape plan-cards-
only) is intentionally not handled here — recovery returns `[]` and the
caller's existing plan-level emit + the new verdict-honesty downgrade
honestly label those `SUCCESS_PLAN_LEVEL`.

23 tests covering both layouts + discovery + e2e + helpers.

### `e69c208` Knock-by-domain resolver

Files: `ma_poc/pms/adapters/knock.py`,
`ma_poc/tests/pms/adapters/test_knock_by_domain.py` (NEW, 416 LOC).

Adds a second extraction path to `KnockAdapter`. The existing
`knockDoorway.init()` static-HTML path stays the primary; when it
fails, the by-domain resolver kicks in:

  GET /v1/profile?code=w&domain={SITE_URL}&refresh=true
    → {profile: {property: "2007584", ...}}

  GET /v1/property/2007584/units
    → {units_data: {units: [...], layouts: [...]}}

No auth, no public_key required.

**Signal-detection rules** (per the JSON-LD memo's `utm_knock`-is-red-
herring rule):

  ✓ `doorway-api.knockrentals.com` in HTML
  ✓ `knockrentals.com/widget` in HTML
  ✓ Aspen Square URL pattern (`aspensquare.com/apartments/{state}/{city}/{slug}`)
  ✓ `utm_knock=` URL param AND `resource.rentcafe.com` ABSENT
  ✗ `utm_knock=` + RentCafe-CDN → DISQUALIFIED (RentCafe is the real
    backend; verified false positives: 10X Iona Lakes, Main Street Square)

Emits `tier_used="TIER_1_KNOCK_API_BY_DOMAIN"` on successful by-domain
recovery so reports + Path B/C can distinguish from the init-path tier.
17 tests covering signal detection + resolver behavior + adapter wiring.

### `df569b5` Detector: Engrain widget signal → sightmap

Files: `ma_poc/pms/detector.py`, `ma_poc/tests/pms/test_detector.py`.

New pass-1 detector signal for the RealPage+Engrain interactive-map
stack. Engrain loads the SightMap iframe dynamically post-JS, so static
HTML lacks `sightmap.com/embed/` — but it carries paired `data-unit` /
`data-floorplan` attributes (Engrain's hydration placeholders) AND a
`realpage.com` script load.

When both signals appear, route to sightmap (0.88 confidence, just
below the strict iframe match at 0.90) so the SightMap adapter's
iframe-fallback discovery (cluster #5 broadening) fires. If it can't
reach the embed code, empty-exit retry routes to the next candidate
via Path B/C.

Negative-signal gates prevent false positives:
- `data-unit` alone (no `data-floorplan`) does NOT fire — too generic.
- No `realpage.com` reference does NOT fire — many marketing-template
  CMSes have `data-unit` + `data-floorplan` for other purposes.
- When `sightmap.com/embed/` IS present, the existing strict iframe
  branch (0.90) still wins.

Verified live (2026-05-20 TIER_3_DOM ALL_fail probe, 7 of 25 props):
Sawmill Station, Headwaters Autumn Hall, Stadia Med Main, Delwyn,
Broadstone SoBro, Millennium River Oaks, Soleste Seaside. 5 tests.

### `d58d624` Generic-DOM: brand-CMS URL discovery

Files: `ma_poc/pms/adapters/_generic_dom_floorplans.py`,
`ma_poc/tests/pms/adapters/test_generic_dom_floorplans.py`.

Adds a final fall-through path in the in-page JS recovery. When the
standard `/floorplans` / `/floor-plans` / etc. subpaths return no plan
cards, scan landing-page anchor hrefs for the multi-property brand-CMS
URL pattern `/apartments/{state}/{city}[/{property-slug}]/(floor-plans|floorplans)`
and probe each unique match.

Supports both URL shapes:
- 3-segment (state/city/tail): Lincoln, McKinley, Renaissance
  (`/apartments/tx/san-antonio/floor-plans`)
- 4-segment (state/city/property-slug/tail): HG Living
  (`/apartments/wa/burien/alcove-at-seahurst/floor-plans`)

Ordering: brand-CMS scan is LAST so common `/floorplans` paths still
win first. Verified live: Fairways 5, Museum Terrace, Villas Willow
Glen, Renaissance at Northpark, Roundtree, Golfside Lake (McKinley),
Alcove at Seahurst (HG Living), Bristol at Sunset (MG Properties). 4
tests including JS structure pin + regex matches 8 real URLs + regex
rejects 7 non-brand URLs + e2e recovery.

### `de8632e` + `9355711` RentCafe-Nestin: e2e pipeline wiring fixes

Files: `ma_poc/pms/adapters/_rentcafe_nestin.py`,
`ma_poc/pms/adapters/rentcafe.py`,
`ma_poc/tests/pms/adapters/test_rentcafe_nestin.py`.

Pre-canary local e2e (running the full ``jugnu`` pipeline against the
4 verified Nestin URLs — Stonewater, Chatwell, Hayden, Altair) caught
TWO production-wiring bugs that the synthetic unit tests + standalone
probes both missed. Without these, the ~225-property Nestin recovery
would have emitted 0 in canary for CF-protected sites:

1. **`de8632e` Absolute-href detection.** ``_find_floorplan_detail_urls``
   only matched relative ``<a href="/floorplans/{slug}">`` anchors.
   Playwright's ``page.content()`` rewrites those to absolute URLs
   (``https://www.stonewaterpark.com/floorplans/{slug}``). The raw
   curl_cffi HTML keeps them relative, so standalone probes worked
   but the pipeline saw 0 detail URLs. Fix: accept both shapes;
   cross-domain absolutes still rejected. 4 regression tests.

2. **`9355711` Stale homepage CF cookies poisoning detail probes.**
   The L1 fetcher installs the homepage's ``cf_clearance`` cookie via
   the ``_clearance_cookies`` ContextVar. ``probe_get`` auto-attaches
   it to every request. But ``cf_clearance`` is PATH-scoped — sending
   it on a different-path URL triggers a fresh CF challenge that
   FAILS (because the cookie is now "wrong" instead of just "missing").
   Verified: standalone ``probe_get`` with no cookies → 200 + 210 KB
   unit HTML; in-pipeline ``probe_get`` with stale cookies → 403.
   Fix: the Nestin fetcher wraps each ``probe_get`` call with
   ``set_clearance_cookies(None)`` + restores via the token in
   ``finally``. Scoped to Nestin only — SecureCafe / WP / hosted-table
   adapters keep their homepage clearance. 2 regression tests.

   Also added ``page=`` parameter to ``recover_rentcafe_nestin_per_plan``
   that uses ``page.evaluate(fetch(...))`` for detail fetches when a
   live Playwright page is available. Canary currently passes
   ``page=None`` (L1-only mode), so the cookie-clear path is the
   active fix; the page.evaluate path is preserved for future
   RENDER-mode enablement.

**E2E verification after both fixes** (all 4 verified targets):
- Stonewater Park: 24 real units, 24 rents, TIER_1_DOM_RENTCAFE_NESTIN
  (was 18 inferred_*, 0 rents, TIER_2_JSONLD)
- Chatwell Club: 7 real units, 7 rents, TIER_1_DOM_RENTCAFE_NESTIN
  (was 11 inferred_*, 0 rents, TIER_2_JSONLD)
- Altair Escondido: 8 real units (SecureCafe — unchanged)
- Hayden Place: 9 real units (SecureCafe — unchanged)

**Lesson**: unit tests + standalone probes are unit-of-intent only.
The pipeline path is the only thing that exercises real cookie state,
Playwright DOM rewrites, and other production-only side-effects. Run
the full ``jugnu`` runner pre-canary — every time.

### `bd3c606` RentCafe-Nestin: 3 pre-canary probe bug fixes

Files: `ma_poc/pms/adapters/_rentcafe_nestin.py`,
`ma_poc/tests/pms/adapters/test_rentcafe_nestin.py`.

Live curl_cffi probe against the 4 verified Nestin targets surfaced
three real-world shape gaps the synthetic fixtures didn't cover:

1. **Chatwell / Hayden table** — real HTML omits `data-label` attrs AND
   ships a `<span class="sr-only">Apartment</span>` accessibility label
   that `get_text()` concatenates with the value. Unit numbers were
   emitting as `"Apartment: #1120"`. Fix: `_normalize_unit_number()`
   strips the "Apartment[: #]" prefix via a dedicated regex.
2. **Altair card** — `\bApartment\b[:\s#]+` matched chrome text like
   "Altair Apartment Homes" / "Apartment Available", emitting bogus
   units like "Homes" / "Available". Fix: `_CARD_APT_RE` now requires
   the `#` after "Apartment" (chrome text never has it).
3. **Stonewater (NEW Layout A3)** — static HTML uses
   `<a id="4112-3" onclick="applyGAClick('A1', '1 Bed(s)', '900',
   '1099.00', ...)">Apply Now</a>` — neither table nor card. Fix:
   new `_parse_applyga_button_layout()` extracts unit + rent + sqft
   from the button id + onclick args; inserted in the cascade between
   table (A1) and card (A2) layouts.

Post-fix verification: 4/4 Nestin targets emit clean unit numbers with
real rents. 6 new regression tests pinning each fix.

## Pre-canary probe (2026-05-20) — Knock-by-domain limitation

The same live probe also tested the Knock-by-domain resolver
(`e69c208`) against the 4 verified Aspen Square targets. **0/4
succeeded.** Root cause:

- The static-HTML Aspen Square pages carry **zero Knock signals** —
  no `knockDoorway`, no `doorway-api.knockrentals.com`, no
  `knockrentals.com/widget`, no `utm_knock=` param, no
  `community_id`. The Knock widget is loaded dynamically via JS
  post-render.
- `/v1/profile?code=w&domain={url}` returns `"PropertyId is not set"`
  because the public Knock API requires a prior
  `/v1/property/community/{hash}` bootstrap call, and that hash only
  exists in the post-JS DOM.

The by-domain resolver fails gracefully (try/except → falls through
to Path B/C retry → next adapter), so the canary won't regress — but
the Aspen Square cluster (~21 props, ~315 strict units in the
follow-on impact estimate) needs a different recovery strategy.

**Options for the next session** (NOT blocking canary):
- Playwright integration (renders the JS, harvests the community
  hash, then calls the API) — most reliable, highest infra cost.
- HAR-replay: the user's HAR file captures one verified
  community_id; check whether Knock community IDs are stable per-
  property over time. If yes, build a one-time harvest map.
- Operator-route (Aspen Square may have a separate guest-card API
  not behind Knock); needs a fresh probe.

## Open items from this follow-on (for the next session)

* **Push refspec** — these 5 commits are local-only at the moment
  (commit SHAs above). Push when ready via the standard refspec:
  ``git push origin claude/portal-hop-may19:fix/resolver-path-patterns-may13``
  (or via the current worktree branch if you're on a different one —
  always confirm the target branch is `fix/resolver-path-patterns-may13`).
* **Canary fleet rebuild at HEAD** (item 4 below) is now the gating
  step — none of the recovery work above has measured impact until
  the next canary runs against the new branch tip.
* **Memory files referenced**:
  `~/.claude/projects/-Users-ankur-PropAi-main/memory/project_jsonld_recovery_2026-05-20.md`
  and `project_tier3_dom_recovery_2026-05-20.md`
  carry the verified spot-check property lists (35 + 25 named cids
  respectively) for post-canary attribution.

---

# Pending / not-yet-done

> **2026-05-20 follow-on session update**: items (1), (2), and (3) are
> now SHIPPED on `fix/resolver-path-patterns-may13` — see the
> **Follow-on session ship list** above for commit SHAs. Two additional
> items not in this pending list were shipped too (TIER_3_DOM ALL_fail
> recovery work from `project_tier3_dom_recovery_2026-05-20.md`):
> Engrain widget detector signal + brand-CMS URL discovery.

## (1) RentCafe-Nestin per-plan adapter — ✅ SHIPPED `4961e2e`

~80% of the 298-prop JSON-LD inflated-SUCCESS bucket. Path C now signals
`no_rent` and would route the next candidate — but if `rentcafe` is the
detected PMS, the existing RentCafe adapter doesn't have a Nestin per-plan
extractor. The memo gives the exact spec:

- Layout A1 (`<table>` shape): Stonewater, Chatwell, Hayden Place — 15%
- Layout A2 (card shape with `APARTMENT: #X` text): Altair, Hampton, LINQ
  — 20%
- Layout B (plan-cards-only, Blueberry shape): rare ~3%

Verified-target list of 35 properties in the memo (sections "Verified
properties" + "Additional 10" + "Additional 15") — re-canary spot-check
targets.

## (2) Knock-by-domain adapter — ✅ SHIPPED `e69c208`

~3% of the JSON-LD bucket. 2-call resolver:
- `doorway-api.knockrentals.com/v1/profile?code=w&domain={url}` → `property_id`
- `doorway-api.knockrentals.com/v1/property/{pid}/units` → unit array

Memo has the full draft `recover_knock_by_domain()` function. Verified live
on Adley at 72nd (cid 47710): 15 unit rows with full data.

Caveat: only fire when `resource.rentcafe.com` is **absent** (negative
signal). `utm_knock=` URL tag alone is NOT a reliable Knock-by-domain
signal — 2 RentCafe-hosted properties (10X Iona Lakes, Main Street Square)
have `utm_knock=` URLs but inventory in RentCafe.

## (3) Verdict-honesty downgrade in validation — ✅ SHIPPED `c01d4af`

Path C now stamps `result["_verdict_quality"] = "SUCCESS_PLAN_LEVEL"` on
the result dict, but the per-property verdict assignment in
`ma_poc/validation/orchestrator.py` (or wherever the verdict is finalized)
still labels these `SUCCESS`. Need to consume `_verdict_quality` (or
detect `_PLAN_LEVEL` suffix on `tier_used`) and downgrade
`SUCCESS → SUCCESS_PLAN_LEVEL` so the headline is honest.

Also: when every accepted unit has an `inferred_*` UID prefix OR
`market_rent_low is None`, downgrade verdict regardless of Path C marker.

## (4) Canary fleet rebuild at HEAD — pending measurement

The cumulative effect of all session work needs an end-to-end canary run
against the full 4,982-property fleet. Per
`reference_canary_per_sha_run.md`:

```bash
# Build image at HEAD
git worktree add /tmp/cb_dc9b548 dc9b548
cd /tmp/cb_dc9b548
git show dc9b548:ma_poc/config/Floorplan-comparisons.csv > ma_poc/config/Floorplan-comparisons.csv
gsutil -q cp gs://jugnu-canary/property-list/properties_full_4982.csv ma_poc/config/properties.csv
# Write the minimal .gcloudignore per the recipe
gcloud builds submit \
  --tag us-central1-docker.pkg.dev/jugnu-494013/jugnu-images/jugnu:canary-dc9b548 \
  --project jugnu-494013 \
  --timeout=2400 .

# Run it (job spec template per the recipe — see memory)
gcloud run jobs replace /tmp/jm_canary_dc9b548.yaml --region us-central1 --project jugnu-494013
gcloud run jobs execute jugnu-canary-dc9b548 --region us-central1 --project jugnu-494013
```

Env vars (per `reference_canary_per_sha_run.md`):
- `ENABLE_TIER_ESCALATION=true`
- `ENABLE_RESIDENTIAL_TIER=true` (required for cluster #4 CF-403 fix to work)
- `ENABLE_DC_PROXY_TIER=false`
- `ENABLE_TIER4_LLM=false` (matches original canary for apples-to-apples)
- `PATH_B_RETRY_ENABLED=1` (default, enables Path B/C)
- `PATH_B_MAX_RETRIES=2` (default)
- Omit `PROFILE_GCS_PREFIX` (clean run)

Key metrics to compare against `jugnu-canary-llmoff-resi-sw9p4`
(2026-05-19 baseline):
- Total strict-quality units (target: significantly above 80,930)
- Per-cluster recovery rates (events.jsonl: filter by previous_tier,
  count RETRY_SUCCESS)
- `_soft_404_recovery=True` count (cluster #4 sub-pattern b)
- New union coverage % vs main (target: above 65.6%)

---

# Lessons learned this session (memoize for future)

These are saved as standalone memory files:

- **`feedback_probe_before_fix.md`** (saved 2026-05-19): always live-probe ≥3
  cluster properties before generalizing. Cluster #3 G5 first pass overfit
  to one outlier (Flatiron, which had a securecafe marker) and missed the
  dominant pattern (G5+Knock co-resident); re-probing 6 more properties
  caught it. **Apply to every cluster fix.**

- Run **both** raw curl AND Chrome MCP for live probes. Cluster #6 OneSite:
  raw curl showed nothing useful (OneSite host is an empty 386-byte shell);
  Chrome MCP network log revealed the `property.onesite.realpage.com/ollr/`
  + `leasing.realpage.com/oll/apploader.js` paths.

- **End-to-end verification on real HTML, not just synthetic test fixtures**.
  Cluster #5 first commit (`727b31c`) was too broad and broke
  `test_portal_hint_survives_full_scrape_chain` — the synthetic test data in
  the existing test file used JSON config blob shape, which my new regex
  caught when it shouldn't. Live-probed real cluster-5 properties don't have
  the URL in JSON-value position, so narrowing was safe. **Always run the
  full PMS test suite after a regex broadening.**

- **Pre-existing test failures** in non-`tests/pms/` directories should be
  bisected before assuming the new commit caused them. 4 of 7 failures
  during this session pre-existed at `0956c59`.

- The **source-grep contract test** pattern is durable: walk
  `ma_poc/pms/adapters/*.py`, find every `tier_used = ...` assignment, assert
  the registry classifies it. Catches drift between adapters and the
  retry mechanism without per-adapter manual review.

- **Push always uses the explicit refspec**:
  `git push origin claude/portal-hop-may19:fix/resolver-path-patterns-may13`.
  `origin/claude/portal-hop-may19` is a different branch.

---

# How to verify everything is intact

```bash
# 1. Confirm tip + push
git log --oneline -1
# dc9b548 Cluster #4 sub-pattern (c): captcha-widget on real page is not a bot wall
git log --oneline origin/fix/resolver-path-patterns-may13 -1
# dc9b548 ... (must match)

# 2. Run the full PMS + validation suites
python -m pytest ma_poc/tests/pms/ ma_poc/tests/validation/ -q
# Expected: 1206 passed, 2 skipped, 0 failed

# 3. Ruff clean on all session-touched files
ruff check \
  ma_poc/pms/empty_exit.py \
  ma_poc/pms/detector.py \
  ma_poc/pms/scraper.py \
  ma_poc/pms/adapters/sightmap.py \
  ma_poc/pms/adapters/rentcafe.py \
  ma_poc/pms/adapters/onesite.py \
  ma_poc/pms/adapters/equity.py \
  ma_poc/fetch/__init__.py \
  ma_poc/scripts/runners/jugnu.py \
  ma_poc/observability/events.py \
  ma_poc/validation/schema_gate.py \
  ma_poc/tests/pms/test_empty_exit.py \
  ma_poc/tests/pms/test_path_b_retry_telemetry.py \
  ma_poc/tests/pms/test_soft_404_recovery.py \
  ma_poc/tests/pms/test_detector.py \
  ma_poc/tests/pms/adapters/test_sightmap.py \
  ma_poc/tests/pms/adapters/test_rentcafe.py \
  ma_poc/tests/pms/adapters/test_onesite.py \
  ma_poc/tests/pms/adapters/test_equity.py \
  ma_poc/tests/fetch/test_top_level_fetch_profile_arg.py \
  ma_poc/tests/scripts/test_jugnu_bootstrap_before_fetch.py \
  ma_poc/tests/validation/test_schema_gate_unit_quality.py
# Expected: All checks passed!

# 4. Smoke-test Path B/C with default flags
python -c "
from ma_poc.pms.empty_exit import is_empty_exit
from ma_poc.pms.detector import detect_pms_candidates
assert is_empty_exit('TIER_1_API_G5_EMPTY')
assert not is_empty_exit('TIER_1_API_G5')
cands = detect_pms_candidates('https://example.com/', page_html='<iframe src=\"https://sightmap.com/embed/abc123xyz\"></iframe>')
print('candidates:', [c.pms for c in cands])
"
# Expected: candidates: ['sightmap']
```

---

# Durable probe + results state

- **Worklist**: `investigations/2026-05-19-canary-iterate/artifacts/probe/feature_fail_1429_worklist.csv` (1,429 properties with bucket + main_tier + main_strict)
- **Per-property probe results**: `investigations/2026-05-19-canary-iterate/artifacts/probe/feature_fail_results.jsonl` (31 entries after this session, append-only)
- **Path B design memo**: `investigations/2026-05-20-path-b-design/DESIGN.md`
- **Project memory files**:
  - `~/.claude/projects/-Users-ankur-PropAi-main/memory/project_canary_iterate_2026-05-20.md` (live state)
  - `~/.claude/projects/-Users-ankur-PropAi-main/memory/project_jsonld_recovery_2026-05-20.md` (Nestin/Knock adapter specs)
  - `~/.claude/projects/-Users-ankur-PropAi-main/memory/feedback_probe_before_fix.md` (live-probe discipline)
  - `~/.claude/projects/-Users-ankur-PropAi-main/memory/reference_canary_per_sha_run.md` (canary build/run recipe)

---

# Quick recap for the impatient next-session reader

- **Don't redo any of the 13 commits listed at top** — they're shipped + tested + pushed.
- **Path B/C** is the architecture: detector finds candidates, retry hook re-dispatches on empty-exit / quality-gate / no-rent / no-area, plan-level fallback flags but preserves data.
- **Pending workstreams**: (1) canary rebuild + measure, (2) Nestin per-plan adapter, (3) Knock-by-domain adapter, (4) verdict-honesty downgrade, (5) shea-style 200-OK sub-pattern.
- **Push refspec**: `git push origin claude/portal-hop-may19:fix/resolver-path-patterns-may13` (never to `origin/claude/portal-hop-may19`).
- **Live-probe ≥3 cluster props before generalizing**. The G5 first pass cost a rewrite by overfitting to one outlier.
