# Phase 6 — Generic HTML / JSON-LD extractor improvements

Date: 2026-05-21
Owner: Phase 6 of the HAR-replay investigation
Predecessors: Phases 0–3 (cookie-mint, L1 render fallback, sticky-route memoization, retry-without-cookies)

## Why Phase 6 exists

The 133-HAR worklist sorts each property into a bucket
([per-har-worklist.jsonl](investigations/2026-05-21-har-replay/per-har-worklist.jsonl)):

| Bucket | n | What it is |
|---|---:|---|
| **`actionable_html_extractor`** | **55** | unit data sits in the page, but the existing generic HTML/JSON-LD path doesn't recognise the shape |
| `discoverable_via_http_probe` | 25 | reachable via `probe_get`; routing/adapter selection problem, not extraction |
| `covered_by_existing_adapter` (all flavors) | 28 | already wired; verify they actually fire |
| `needs_chrome_probe` | 12 | unit data lives in a same-origin iframe / DOM-only widget (RealPage Leasing) |
| `actionable_new_api` | 7 | new API to instrument; out of Phase 6 scope |
| `probe_blocked_cf` | 6 | anti-bot; covered by the blockwall v2 grind, separate strategy |
| **Total** | **133** | |

The `actionable_html_extractor` bucket is **the single largest lever** —
55 properties (41% of the HAR sample, ~95% of which map to `T2_LLM_only`
in production). Each one currently costs an LLM call per scrape. A
single deterministic parser change replaces 55 paid LLM calls per cycle.

Phase 6's question: **what shapes of in-page unit data is the current
generic extractor missing, and which sub-fixes get the biggest pickup
for the least surface area?**

---

## Pattern analysis — the 55 `actionable_html_extractor` properties

A read-through of every `actionable_html_extractor` per-har note groups
the 55 into 5 shape-clusters plus a tail:

| Sub-bucket | n | Shape | Phase |
|---|---:|---|---|
| **A. Namespaced JS member assignment** | **~30** | `ysi.floorplansList = [{...}]`, `propConfig.fp_data = {...}`, Yardi/RentCafe SSR PascalCase | **6.1** |
| B. HTML floor-plan tables | ~8 | `<table>` with `<th>Plan</th><th>Beds</th><th>Rent</th>` rows | 6.2 |
| C. `data-*` attribute payload | ~6 | `<div data-unit="..." data-rent="..." data-sqft="...">` cards | 6.3 |
| D. Non-Apartment JSON-LD | ~5 | `Product`, `Offer`, `Residence`, custom `@type` carrying rent/sqft | 6.4 |
| E. Mis-typed `<script>` blocks | ~3 | `type="text/x-template"`, `type="application/x-json"`, no `type=` | 6.5 |
| Tail (per-property quirks) | ~3 | one-offs — escape via Phase 6.1's catch-all or LLM | — |

Phase 6.7 (RealPage Leasing widget) is parallel: it targets the
**12-property `needs_chrome_probe` cluster**, not the 55 `actionable_html_extractor`
bucket. Treated separately below.

---

## Phase 6.1 — Namespaced JS member-assignment extraction *(IMPLEMENTED 2026-05-21)*

**Shape recognised:**

```html
<script>
  ysi.floorplansList = [
    {"Id": 12345, "Beds": 1, "Baths": 1, "MinSqFt": 720, "MinRent": 1450, ...},
    ...
  ];
</script>
```

**Why the existing `_ASSIGNMENT_RE` misses it:**
the legacy regex anchors on `var/let/const/window.X` only; dotted
namespaces (`ysi.floorplansList`, `propConfig.fp_data`, `app.config.fp`)
slide past.

**Why the existing extractor's non-greedy match would also be wrong:**
Yardi/RentCafe SSR nests empty arrays (`"Amenities": []`) inside the
floorplans list. A non-greedy `\[.*?\]` regex stops at the first inner
`]`, truncating the body to fragment-1.

**Implementation** ([ma_poc/pms/adapters/_html_extract.py](ma_poc/pms/adapters/_html_extract.py)):

1. `_NAMESPACED_LHS_RE` — regex that finds dotted-namespace LHS positions
   only (requires at least one `.` to avoid overlap with legacy regex).
2. `_extract_balanced_value(text, start)` — quote-aware, depth-tracking
   bracket walker. Skips leading whitespace, handles `\"` escapes,
   never falsely closes inside a JSON string. Returns `None` on
   unbalanced input (truncation) rather than inventing a closing.
3. **Strategy C** in `extract_embedded_blobs_from_html` — wired AFTER
   the legacy `var X = ...` strategy, runs `_NAMESPACED_LHS_RE.finditer`,
   walks each match forward via `_extract_balanced_value`, JSON-validates,
   filters values under 200 chars, emits blob with
   `url = "embedded:script-member:<lhs>"`.

**Quality gates** (carried forward from the legacy strategy):
- JSON-loads validity required.
- Min body length 200 chars (filters trivial config noise).
- Unit-keyword post-filter applied by downstream parsers, not in 6.1.

**Tests** ([test_namespaced_assignment_extraction.py](ma_poc/tests/pms/adapters/test_namespaced_assignment_extraction.py)):
17 tests across three groups:
- `_extract_balanced_value` (7): nested arrays, nested objects,
  brackets-in-strings, escaped quotes, no-bracket return-None,
  unbalanced return-None, leading-whitespace skip.
- `_NAMESPACED_LHS_RE` (4): two-segment, deep nesting, bare-identifier
  exclusion, statement-boundary permissiveness (gate-by-validation
  documented).
- End-to-end on a real Yardi fixture (5): finds `ysi.floorplansList`,
  3+ floor plans, PascalCase keys present, nested empty `Amenities` array
  preserved, no false positives on clean HTML, invalid-JSON discarded,
  too-short values rejected.
- Source-grep contract (1): pins `_NAMESPACED_LHS_RE.finditer` +
  `_extract_balanced_value` + `embedded:script-member:` URL prefix
  references so a refactor can't silently drop the wiring.

**Status:** code shipped; tests applied a start-position fix
(`text.index("=") + 1` to align test calls with the production call
site that uses `m.end()` past `\s*=\s*`). Final test re-run + lint
pending — see todo list.

**Estimated pickup:** ~30 properties moved out of `T2_LLM_only`.

---

## Phase 6.2 — HTML floor-plan tables *(not started)*

**Shape:**

```html
<table class="floorplans">
  <thead><tr><th>Plan</th><th>Beds</th><th>Baths</th><th>Sqft</th><th>Rent</th><th>Available</th></tr></thead>
  <tbody>
    <tr><td>The Oak</td><td>1</td><td>1</td><td>720</td><td>$1,450</td><td>Yes</td></tr>
    ...
  </tbody>
</table>
```

**Implementation sketch:**
1. New extractor `extract_floorplan_tables_from_html(html)` in
   `_html_extract.py`.
2. BeautifulSoup over `<table>` elements; score each table by:
   - Has at least one `<th>` or first row matches header-keywords
     (`{beds, br, bath, ba, sqft, sq ft, square feet, rent, price,
     available, availability}`).
   - At least 2 data rows.
3. For tables that score, map columns by header keyword, emit one
   blob per table with `url = "embedded:html-table:<index>"` and
   body shaped as `[{plan, beds, baths, sqft, rent, available}, ...]`.
4. Filter: at minimum, `beds` AND (`rent` OR `sqft`) must resolve to
   non-empty in ≥1 row; otherwise discard the table.

**Tests target:** 8 tests — header detection, 1-bed/2-bed/3-bed parsing,
`$1,450` and `From $1,450` rent normalisation, "Call for Pricing" → null,
malformed `colspan`/`rowspan` handling, multi-table page (e.g. one per
building), false-positive guard (amenities table with `<th>Feature</th>`).

**Estimated pickup:** ~8 properties.

---

## Phase 6.3 — `data-*` attribute payload *(not started)*

**Shape:**

```html
<div class="unit-card"
     data-unit="A-101"
     data-beds="1"
     data-baths="1"
     data-sqft="720"
     data-rent="1450"
     data-available="2026-06-01">
  ...
</div>
```

**Implementation sketch:**
1. New extractor `extract_data_attr_cards_from_html(html)` —
   BeautifulSoup over all elements with ≥3 `data-*` attributes
   matching the unit-vocab set (`unit, plan, beds, baths, sqft, rent,
   price, available, availability_date, floorplan, building, floor`).
2. Group siblings sharing the same parent + class pattern; emit
   one blob per group.
3. Filter: same `beds + (rent | sqft)` minimum as Phase 6.2.

**Tests target:** 6 tests — basic card parse, vocab variants
(price vs rent, sqft vs sq_ft), nested-div sibling grouping, units
across multiple parent containers (don't merge), boolean-ish flags
(`data-available="true"` → `AVAILABLE`), single-card-no-siblings
acceptance.

**Estimated pickup:** ~6 properties.

---

## Phase 6.4 — Broader JSON-LD `@type` matching *(not started)*

**Shape:** the current `tier2_jsonld.py` looks for `Apartment`,
`ApartmentComplex`, `Offer`. Real HARs show unit-bearing JSON-LD also
inside `Product`, `Residence`, `SingleFamilyResidence`, `Place`, and
sometimes `@type` as an array or custom `RealEstateListing`.

**Implementation sketch:**
1. Expand the type-match set in `tier2_jsonld.py` to include
   `Product`, `Residence`, `SingleFamilyResidence`, `Place`,
   `RealEstateListing`. Match if `@type` is a string OR if it's a
   list and ANY element matches.
2. Field mapping per type:
   - `Product` → `offers.price` → rent, `name` → unit/plan, `description` → notes
   - `Residence` / `SingleFamilyResidence` → `floorSize` → sqft,
     `numberOfRooms` → beds, `offers.price` → rent
   - `RealEstateListing` → custom; map best-effort + log fields seen
3. Confidence: existing scoring stays; new types start at 0.85 cap
   (vs 1.0 for `Apartment`) because field-mapping is best-effort.

**Tests target:** 5 tests — Product extraction, Residence extraction,
`@type` as array, mixed Apartment + Product page (prefer Apartment),
unknown type silently skipped.

**Estimated pickup:** ~5 properties.

---

## Phase 6.5 — Mis-typed `<script>` block tolerance *(not started)*

**Shape:** the current extractor scans `<script>` blocks but skips
non-JS MIME types. Captures showed unit JSON in
`type="text/x-template"` (Vue.js SSR), `type="application/x-json"`
(custom), or `type=""` (omitted, default JS).

**Implementation sketch:**
1. In `extract_embedded_blobs_from_html`, broaden the `<script>`
   filter to accept:
   - No `type` attribute (default JS — already accepted).
   - `type` in `{text/javascript, application/javascript,
     application/json, application/ld+json, text/x-template,
     application/x-json, x-template}`.
2. Run the same Strategy A/B/C extractor sweep on accepted bodies.
3. JSON-validity + min-length filter already discards garbage.

**Tests target:** 4 tests — `text/x-template` accepted,
`application/x-json` accepted, `type=""` accepted, unknown
`text/html` rejected.

**Estimated pickup:** ~3 properties (plus marginal lift to 6.1/6.4
on `text/x-template` Vue pages).

---

## Phase 6.7 — RealPage Leasing widget DOM scrape *(spec ready, not started)*

**Different bucket:** targets the 12-property `needs_chrome_probe`
cluster, not the 55-property `actionable_html_extractor` bucket.

**Shape:**
- Property page embeds `<div class="realpage widget">{"realpageId":"NNNN"}</div>`.
- Widget renders a same-origin iframe at
  `<property>.com/.../#!/oll/search-floorplan` (URL fragment routed
  client-side by the widget JS).
- Cards in the iframe DOM contain:
  ```
  Floor Plan Name (h3-ish)
  N Bed | N Bath | NNN sq ft
  $NNNN*  (or "Call for Pricing")
  (N) Available
  ```

**Why HAR/curl can't reach this:** the widget XHRs to
`leasing.realpage.com` and renders client-side; the API response body
is JSON but auth-gated by per-property tokens minted at widget-load
time. Replay would expire on stale token; rebuilding the auth flow
loop is more work than scraping the rendered DOM.

**Implementation sketch:**
1. New tier 3.5 adapter `realpage_leasing_widget` in
   `ma_poc/pms/adapters/_realpage_leasing.py`.
2. Detector: page has `<div class="realpage widget">` element with
   parseable `realpageId` JSON.
3. Extractor (Playwright path only — does NOT work via curl_cffi):
   - Navigate page; wait for the widget iframe to appear.
   - Switch into the iframe.
   - `wait_for_function(() => document.body.innerText.length > 2000)`
     to defeat the re-render-on-resize race that bit probing.
   - Query for floor-plan card selectors (per probing notes:
     `[role="article"]` or `.floorplan-card` — confirm at integration).
   - For each card, regex out the 4 lines into
     `{plan, beds, baths, sqft, rent_min, availability_count}`.
4. Sticky-route alignment (Phase 2.1): on success, write
   `last_winning_probe_source = "realpage_leasing_widget"` so the next
   run skips the HAR/probe cascade and goes straight to the iframe.
5. Drift detection: 3 consecutive empty-cards → fall through to LLM.

**Tests target:** 6 tests against a saved iframe-DOM fixture from
one of the 12 probed properties — detector match, card parse, card
parse with "Call for Pricing", multi-bed/multi-bath card, no-cards
empty page, missing `realpageId` rejection.

**Estimated pickup:** 12 properties (and unlocks the cluster for
future similar widgets — the same pattern is documented in
`investigations/2026-05-21-har-replay/per-har/_chrome_probes.md` to
extend to fmgnj, sherwoodacres, ten68west).

**Risks:**
- Browser-only — measurably slower than tier 1/2/3.
- Widget JS does its own ETag re-render on scroll/resize; the
  `wait_for_function` body-length gate is the documented mitigation.
- Same-origin assumption may not hold across the 12 — confirm during
  integration (probing covered ~3).

---

## Sequencing + recommended landing order

1. **Land 6.1** (this branch). Re-run tests, regression sweep, ruff.
   This is the biggest single lever (~30 props).
2. **Land 6.2 (HTML tables)** next. Self-contained extractor, no
   adapter-routing changes, lowest integration risk. ~8 props.
3. **Land 6.5 (MIME relaxation)** as a 1-line follow-up to 6.1 —
   tiny diff, multiplies 6.1/6.4 coverage. ~3 props plus marginal
   uplift to 6.1.
4. **Land 6.4 (broader JSON-LD)** — modifies an existing extractor,
   needs the confidence-cap test isolation. ~5 props.
5. **Land 6.3 (data-\*)** — newest pattern, smallest cluster, easiest
   to get wrong (false positives on amenity galleries). Ship last
   among the HTML cluster. ~6 props.
6. **6.7 (RealPage Leasing widget)** is parallelizable — different
   adapter, different test surface, different tier (3.5 / Playwright).
   Pull it forward only if the 12-property cluster's LLM cost is
   acute. Otherwise queue after the HTML cluster.

**Cumulative pickup if all of 6.1–6.5 + 6.7 land:** ~64 properties out
of `T2_LLM_only`, of which 52 use **0 LLM tokens** at runtime
(6.1–6.5 are deterministic). The 12 from 6.7 still use a browser but
no LLM.

---

## Out of scope for Phase 6

- Anti-bot work — covered by [blockwall_v2 strategy](investigations/2026-05-21-t3-grind/artifacts/blockwall_v2/STRATEGY.md).
- HAR-replay rung itself (Phase 4.x) — the per-property manifest
  approach in [SUMMARY.md](investigations/2026-05-21-har-replay/SUMMARY.md).
- Vision/OCR — terrainaustin-style JPEG floor plans need vision tier;
  not solvable by HTML parsing.
- New PMS classifiers (iloveleasing, On-Site.com, _fp-renderable CMS,
  Site123) — captured in
  [per-har/_chrome_probes.md](investigations/2026-05-21-har-replay/per-har/_chrome_probes.md);
  separate routing work.

---

## Status

| Sub-phase | Status | Tests |
|---|---|---:|
| 6.1 | **Shipped** | 17/17 |
| 6.2 | **Shipped + wired into `generic.py`** as sub-tier 4.7 | 12/12 |
| 6.3 | **Shipped + wired into `generic.py`** as sub-tier 4.8 | 9/9 |
| 6.4 | **Shipped** (Accommodation/House/Suite added; Hotel/Lodging deliberately excluded) | 9/9 |
| 6.5 | **Shipped** (`_SCRIPT_TYPE_ACCEPT` + `_JSON_SCRIPT_TYPE_ACCEPT`) | 8/8 |
| 6.7 | **Detector + iframe-DOM parser shipped**; Playwright orchestration still TBD (needs live-browser fixture for end-to-end test) | 15/15 |

**Cumulative test surface:** 70 new tests, all passing. Full regression sweep
(`pms/` + `core/`) **1444 passed, 2 skipped, 0 failed** — no regressions
from the pre-Phase-6 baseline of 1391.

**What's not yet shipped:** the Playwright orchestration glue for 6.7
that drives the iframe (`page.frame_locator(...).inner_html()`,
`wait_for_function` body-length gate, sticky-route memoization with
`last_winning_probe_source = "realpage_leasing_widget"`). The detector
+ parser are deterministic and unit-tested; the orchestration needs a
live-browser fixture against one of the 12 probed properties before
landing. File: [`ma_poc/pms/adapters/_realpage_leasing.py`](ma_poc/pms/adapters/_realpage_leasing.py).
