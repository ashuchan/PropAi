# Plan-summary emission — Phase 3 & 4 roadmap

**Context.** Phases 0–2 are shipped (Knock, APTS247, AspenSquare,
post_process.admitted semantics, SightMap, G5, AvalonBay).
This document holds the remaining adapter-by-adapter migration so each
chunk is a stand-alone PR / commit with its own test, its own canary
sample, and a clear rollback boundary.

**Pattern (recap).** For every adapter whose source API exposes a
parent floor-plan envelope (some list of plans, each containing a
units sub-array OR an explicit price/availability count), the parser
must:

1. Track which plan IDs contributed at least one unit-level row.
2. After the unit loop, iterate the floor-plan envelope and emit a
   plan-only row (``unit_number=""``) for each plan NOT in that set.
3. Let ``post_process.classify()`` route the plan-only rows to
   ``plan_summaries`` automatically.

The no-dup invariant is enforced by step 1 + step 2's set-difference
filter. Phase 1's ``pp.admitted`` semantic change (units only) keeps
the partition clean at the AdapterResult boundary.

---

## Phase 3 — medium-volume Tier-1 adapters

One commit per adapter. Each commit gets:
- Parser edit with `covered_<envelope-key>_ids: set[...]` tracking.
- Plan-only emission loop (`unit_number=""`).
- Unit test in `tests/pms/adapters/test_plan_summary_phase3.py` with
  three cases per adapter: (a) all plans have units → no plan rows,
  (b) mixed → only uncovered plans emit, (c) post_process roundtrip
  asserts the no-dup invariant.
- Per-adapter canary verification: pick 2 known properties on that
  PMS, dry-run locally + cloud, diff per-PID `floor_plans` count.

### Phase 3.1 — `entrata.py`
- **Source**: `floorplans` list at top of `/Apartments/module/widgets/...`
  response; each plan has `unitTypeId` + nested `units` array.
- **Code site**: `parse_entrata_floorplans` in
  [ma_poc/pms/adapters/entrata.py](ma_poc/pms/adapters/entrata.py).
- **Verification needed**: confirm whether plans-without-units are
  already emitted (audit said OK but didn't inspect the inner loop).
  Read a real Entrata `/widgets/floorplans` body from `data/raw_api/`
  before editing.
- **Canary**: PIDs 40867 (Garden Park), 290347 (Bowman Station — known
  multi-plan Entrata).

### Phase 3.2 — `realpage_oll.py`
- **Source**: `Workflow.ActivityGroups[].GroupActivities[]` with
  nested unit list. Each `GroupActivity` carries plan metadata.
- **Code site**: `parse_realpage_oll_payload` (search for
  `GroupActivities` reads).
- **Notes**: Audit said OK + emits plan-only. Verify the `else` branch
  exists; if the `Units == []` case truly skips, add emission.
- **Canary**: Pull a successful RealPage OLL property — search
  `data/scrape_events.jsonl` for `tier_used="TIER_1_API_REALPAGE_OLL"`
  and pick 2 PIDs.

### Phase 3.3 — `amli.py`
- **Source**: `queries[].state.data` arrays as floor-plan list with
  nested `units[]`. Plan rows without units are silently dropped per
  the audit (line 152-153 comment in amli.py).
- **Code site**: `parse_amli_payload`.
- **Canary**: AMLI portfolio is ~20 properties; pick 2 with known
  mixed plan availability.

### Phase 3.4 — `cortland.py`
- **Source**: `floorplans[].availprice` envelope; when `availprice`
  is empty/missing, no plan row emitted today (`parse_cortland_units`
  line 99-131 silently drops).
- **Code site**: `parse_cortland_units`.
- **Canary**: Pick 2 Cortland properties.

### Phase 3.5 — `irvine.py`
- **Source**: `groups[].units[]` envelope; when `units == []`, group
  silently disappears.
- **Code site**: `parse_irvine_payload`.
- **Canary**: Irvine Company has a small fixed cohort — pick 2.

### Phase 3.6 — `essex.py`
- **Source**: `floorplans[].units[]`; plans with `units == []` are
  skipped at parse_essex_bulk line 94-95.
- **Code site**: `parse_essex_bulk`.
- **Canary**: Pick 2 Essex properties.

### Phase 3.7 — `funnel.py`
- **Source**: `rentals` list (flat) OR `listing[].rentals[]` (envelope).
  Variant detection needed — only the envelope-variant requires the
  fix; the flat shape is already correct.
- **Code site**: `parse_funnel_payload`.
- **Special handling**: Add a shape-detection branch so the flat
  variant doesn't double-process.
- **Canary**: Pick 2 Funnel properties.

### Phase 3.8 — `resman.py`
- **Source**: `unitTypes[]` groups with nested `Units[]`; today emits
  plan-only row when `Units == []` AND `MarketRent` truthy
  (line 143). Plans with 0 units AND no MarketRent silently drop.
- **Code site**: `parse_resman_unit_types`.
- **Fix**: Drop the `MarketRent`-truthy precondition — emit plan
  row regardless of rent availability. Plans without rent become
  plan_summaries with `market_rent_low=None`.
- **Canary**: Pick 2 ResMan properties.

### Phase 3.9 — `onesite.py`
- **Source**: per-plan emission. Currently uses
  `unit_number=fp.id` as a synthetic identifier, routing every plan
  row to ``units`` (incorrect — they should be plan_summaries).
- **Code site**: per-plan emit block.
- **Fix shape**: Different from the others — instead of adding a
  plan-only loop, replace `unit_number=fp.id` with `unit_number=""`
  for rows that have no real unit identifier. Real per-apartment
  rows (when OneSite returns any) keep their `unit_number`.
- **Canary**: Pick 2 OneSite properties.

### Phase 3.10 — `encoreskyline_template.py`
- **Source**: per-plan Jonah widget interaction yields per-plan unit
  rows. When the widget returns 0 units for a given plan, no
  plan-level fallback.
- **Code site**: Plan-iteration loop that calls into
  `_encoreskyline_units` per plan.
- **Fix**: After each per-plan widget call, if 0 units were
  extracted, emit a plan-only row from the plan-level metadata
  (already known: plan name, beds, baths, sqft).
- **Canary**: Pick 2 EncoreSkyline properties.

**Phase 3 total**: 10 commits, ~30-50 LOC of adapter code each, plus
~50-80 LOC of tests each. Cumulative ~500 LOC of code + ~800 LOC of
tests. Estimated 2-3 engineer-days at 1 commit per 30 min including
canary verification.

---

## Phase 4 — secondary parsers & DOM/LLM paths

These are lower priority because either:
- The source rarely has a real floor-plan envelope (DOM scraping
  with only unit cards).
- The output is already mostly correct because the parser emits
  plan-level rows by default.
- OR the gap is in a fallback path that's already rare in production.

### Phase 4.1 — `_rentcafe_nestin.py` (plan-level fallback)
- **Source**: per-detail-page fetch returns N unit rows (0 ≤ N ≤ many).
- **Gap**: When a detail page parses 0 rows (the page exists but the
  table-or-card parser missed it), the plan identity is lost.
- **Fix**: When `parse_nestin_detail_page` returns `[]` AND the
  fetch was 200 + had unit-signal markers, emit a plan-only row
  carrying `floor_plan_name` from the section heading + the detail
  URL as the source.
- **Test**: Mock a detail page with valid heading + no rows.
- **Note**: Phase 1 telemetry will surface these via the
  `parser_silent_empty` outcome already wired in.

### Phase 4.2 — `appfolio.py` + `_appfolio_embed.py`
- **Source**: SSR-rendered iframe HTML. Each plan card is a `.js-listing-card`.
- **Gap**: When the SSR returns 0 cards (empty iframe), the property
  emits 0 units AND 0 plan_summaries. The marketing site DOES list
  plan names in a separate static HTML section.
- **Fix**: After the SSR parse, fall back to scraping the
  property's homepage `<h*>` headings + nearby card markup for
  plan-name candidates. Emit one plan-only row per discovered name.
- **Risk**: Higher false-positive rate. Gate with a count-of-2+
  match against beds/baths/sqft text near the name.
- **Test**: 2 fixtures — SSR-empty + homepage with 4 plan names.

### Phase 4.3 — `_generic_dom_floorplans.py`
- **Source**: Discovers plan cards via DOM scan on `/floorplans`.
- **Gap**: Cards that fail the 2-of-{bed/bath, sqft, $} signal-score
  gate are silently discarded.
- **Fix**: When score is 1 (one signal present, not enough for
  unit-class confidence), STILL emit a plan-only row carrying just
  the signal we did find + the card text. ``unit_number=""`` so
  post_process routes correctly.
- **Risk**: Adds noise. Cap at top-10 plan candidates per page.

### Phase 4.4 — `generic.py` JSON-LD branch
- **Source**: `ApartmentComplex` with `Apartment[]` Offers in
  JSON-LD.
- **Gap**: When the Offer parser yields 0 valid units (Offer
  schema wrong, no `price`), the `Apartment` identity is lost
  even though the JSON-LD `name` is present.
- **Fix**: In `parse_jsonld_apartment_complex`, when a child
  `Apartment` has a `name` + dimensions but no valid Offer rent,
  emit a plan-only row carrying the name + dimensions.

### Phase 4.5 — `generic.py` LLM branch
- **Source**: LLM-extracted unit list.
- **Gap**: LLM prompts ask for "available units" — plans without
  available units are not included in the response.
- **Fix**: Augment `dom_analysis.txt` and `tier4_extraction.txt`
  prompts to ALSO ask for "advertised floor plans with no current
  availability" as a separate `plan_summaries` array. Parser
  routes these to the plan-only emission path.
- **Risk**: Token budget + LLM compliance. Requires prompt v3
  validation against ~10 known PIDs before shipping.

### Phase 4.6 — `equity.py` defensive review
- **Source**: Audit said "flat unit rows, no envelope". Confirm by
  reading a captured response — if there's a plan envelope, add
  the fix; otherwise document and move on.

### Phase 4.7 — `rentmanager.py` defensive review
- **Same as 4.6**: confirm flat-only shape, document.

### Phase 4.8 — `maac.py` defensive review
- **Same as 4.6**: confirm flat-only shape (audit said envelope at
  parse-call-level — investigate).

### Phase 4.9 — `rentvision.py` (Gap B activation)
- **Status**: Plan-only emission already correct. The bug was
  Phase 1's `pp.admitted` doubling — fixed in Phase 1.
- **Action**: Add a regression test that asserts the
  post-Phase-1 partition holds.

**Phase 4 total**: 9 commits, mixed sizes — adapter fixes ~20-100 LOC
each; LLM-prompt change is risk-heavy and needs validation. Estimated
3-4 engineer-days.

---

## Rollout ordering

For each phase, ship in this order:
1. Adapter parser edit + isolated parse-function tests.
2. End-to-end test that runs the adapter's `extract()` against a
   captured fixture and verifies the AdapterResult's partition.
3. Local canary (2 PIDs from the affected cohort).
4. Cloud canary on `canary-introspect` with 5 PIDs.
5. Spot-check production output 24h after merge for unit-count
   delta vs prior day (don't ship more than one adapter per day).

## Risk gates

For Phase 3:
- If a parser fix increases `n_admitted` count by >20% on the canary,
  pause and verify the new plan_summaries aren't accidentally
  unit-level rows that escaped the partition.
- If `_post_process_meta.cross_page_dedup_collapses` jumps after a
  fix, the plan-row emission is colliding with an existing
  unit_number — needs investigation before rollout.

For Phase 4:
- 4.5 (LLM prompt change) must NOT ship until prompt v3 passes a
  10-PID validation cohort with ≥80% plan_summaries recall against
  hand-labeled ground truth.
- 4.3 (generic_dom_floorplans low-score emission) has the highest
  risk of false-positives. Ship behind a feature flag
  (`ENABLE_LOW_SCORE_PLAN_EMISSION=1` default off) for the first
  cloud canary; promote to default after 1 week of clean data.

## Tracking

Each commit message should carry:
- `Phase: 3.<N>` or `Phase: 4.<N>`
- `Adapter: <name>`
- `Canary PIDs: <list>`
- Reference to this doc.

After each merge, update the master audit table at
[HOLISTIC_ADAPTER_AUDIT.md](c:/tmp/canary_plan_summary_fixes/HOLISTIC_ADAPTER_AUDIT.md) (or its in-repo successor) by
moving the adapter from "NEEDS FIX" to "DONE" with the canary diff.
