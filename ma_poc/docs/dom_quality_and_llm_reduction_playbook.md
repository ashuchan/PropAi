# DOM Data Quality + LLM Reliance Reduction Playbook

**Working directory for every command:** `ma_poc/`
**Audience:** Claude Code (or any engineer) working on (a) tightening data quality at the DOM extraction layer and (b) progressively replacing LLM calls with deterministic adapters.
**Created:** 2026-05-23.
**Companion playbook:** [`failed_no_data_debugging_playbook.md`](failed_no_data_debugging_playbook.md) for the FAILED_NO_DATA debugging surface. This document is about properties that EXTRACT SUCCESSFULLY but ship low-quality data — and properties whose extraction we want to keep moving off the LLM.

---

## Two-problem framing

We are solving two related but distinct problems on the extraction stack:

| # | Problem | Why it matters |
|---|---|---|
| **P1** | **DOM data quality is poor on TIER_4_LLM_DOM rows** — junk leaks into typed columns, deposit/fee amounts get routed as rent, plan-level rows get shipped as unit-level | Downstream rent intelligence, comparator analytics, and competitor pricing all consume these rows. Bad rents at deposit-leak prices ($219 / $250 / $300) corrupt market-rate signals. |
| **P2** | **LLM_DOM is the single largest cost stream (~82% of run LLM cost)** and the largest tier by property count (1,373 / 4,982 = 27.6% on 2026-05-22) | Self-learning replay absorbs 90.6% of LLM_DOM calls at $0 (saved DOM hints replay), but first-touch cost compounds with CSV churn and degraded-quality saves get evicted on first miss. The structural fix is to capture more cohorts deterministically. |

These problems interact. Many low-quality rows are LLM hallucinations from non-deterministic prompts. Every cohort we move to a deterministic adapter both (a) lowers LLM spend AND (b) eliminates the hallucination surface for that cohort.

---

## TL;DR — what we know as of 2026-05-23

### Cost picture (run 2026-05-22)

- Total LLM spend: **$6.63** ([cost_ledger.db](../data/reports/cloud_run_2026-05-22/) + [llm_report.json](../data/reports/cloud_run_2026-05-22/) agree to ~rounding)
- Model: **`qwen/qwen3-235b-a22b-2507`** via OpenRouter (NOT gpt-4o-mini despite the stale stamp in `cost_ledger.detail`)
- Tier shares: DOM_ANALYSIS = $5.42 (82%), TIER_6_LLM = $1.09 (16%), F2_NULL_FIELD_RECOVERY = $0.09 (1%), API_ANALYSIS = $0.03 (<1%)
- Run-to-run trend: $14.60 (05-16) → $12.98 → $10.23 → $8.01 → $10.07 → $6.95 → $6.63 (05-22). Declining as self-learning replay rate grows.
- Worth-checking-against-OpenRouter: a $20/run number suggests either (a) older pre-self-learning runs, (b) double-counted same-day runs ([c:/tmp/run-2026-05-20](C:/tmp/run-2026-05-20) + [c:/tmp/run-2026-05-20-morning](C:/tmp/run-2026-05-20-morning) sum to ~$20.80), or (c) provider-side billing markup.

### TIER_4_LLM_DOM cohort (run 2026-05-22)

- 1,366 successes + 1 failure = **1,367 properties**
- **1,237 cost $0** (replayed from saved selectors) / **129 paid LLM calls** ($0.83 in successes.csv terms, but the ledger says LLM_DOM totalled $4.77 — successes.csv `llm_cost` undercounts; trust the ledger)
- **841 of 1,367 (61.5%) are pms_detected=rentcafe** — the RentCafe adapter cascade exits empty and generic falls back to LLM_DOM
- 22 portfolio root-domains have ≥3 properties in this tier; top 6 cover 86 properties (livebh.com=32, weidner.com=17, byredwood.com=14, venterraliving.com=9, equityapartments.com=8, princetonmanagement.com=7)

### TIER_4_LLM_DOM data quality (run 2026-05-22, 6,884 unit rows)

| Defect | Rows / Props | Verified root cause |
|---|---:|---|
| `rent < $1,000` (suspect) | 660 (9.6%) | Mix of concession-text leak, deposit/fee leak, genuine low-income |
| `rent < $500` (most-suspect) | 138 (2.0%) | Confirmed deposit/fee leak in canonical cases |
| **All-same-rent across ≥3 plans** | 12 properties | LLM copying "from $X" banner text into rent for every plan (PID 11727 risebedfordlake: 13 plans all $675 — `concession_text="from $675"` in LLM response) |
| `available_date` contains non-date string | 142 (2.1%) | jugnu.py:2037 fallback ships raw producer string (e.g. `"Longhorn"`, `"Only 2 Vacant Apartments Left!"`) when format_loose_date returns None |
| `availability_status='WAITLIST'` | 21 rows | Non-canonical value slipping through unnormalized |
| `unit_id == floor_plan_name` | 29 rows | Plan code used as unit identity (degenerate per-unit identity) |
| `fpn` length > 35 + multiple ` - ` | 99 rows | LLM concatenating plan + property + community name |
| `beds=0` + no "studio" in fpn | 198 (2.9%) | LLM defaulted bedrooms to 0 under uncertainty |
| `units=[]` but `floor_plans!=[]` | 233 props (17.1%) | Mostly genuine "pricing not published" — LLM returns `market_rent_low=None` correctly |
| `rent_low` fill | 88.4% | 798 rows missing rent — most are "Contact for pricing" |
| `available_date` fill | 56.9% | 2,964 rows missing — biggest single gap (playbook §19.1 avail-date-only-gap) |

---

## Phase 0 — Anti-patterns I caught myself in (2026-05-23)

Today's session, sequence of mistakes worth remembering. Most of these are session-local — they don't appear in `failed_no_data_debugging_playbook.md` because they're DQ-specific.

| # | Anti-pattern | What I did wrong | What to do instead |
|---|---|---|---|
| **DQ-1** | **Conflated "$0 per-property" with "no LLM involvement"** | Reported "90.6% of TIER_4_LLM_DOM cost $0" as if that meant LLM_DOM isn't a major cost. Reality: those rows replay saved selectors that came from prior LLM calls; the tier-level cost (cost_ledger filtered to TIER_4_LLM_DOM) is $4.77/run and 82% of run-level LLM spend. The cohort-share misleadingly looks "free". | Always cite the **tier-level cost ledger total**, not the per-property field from successes.csv. The two disagree because successes.csv stamps cost per terminal-tier, not per-LLM-call. |
| **DQ-2** | **Shipped speculative selectors without live verification** | Added `.dmContent` (DudaMobile) and `[data-selenium-id^='Rent_']` extractors to `_COMPACT_ROW_EXTRACTORS` based on saved-profile patterns alone. The lincolnatwolfchase live grep showed the `data-selenium-id` prefixes were all `mobile`/`address`/`SID`/`click`, NOT `Rent_*`/`Sqft_*` — the `Rent_*` prefix lives on the SecureCafe portal (cross-origin CF-blocked from prod per failed_no_data §20.3). I shipped dead code. | Per failed_no_data anti-pattern #18: live-fetch ≥5 sample PIDs and verify the selector matches BEFORE shipping. Reverted same session — see "What was reverted" below. |
| **DQ-3** | **Assumed format_loose_date is the bug because "Longhorn" ships** | Initial hypothesis: the lenient date parser had a hole. Test: `format_loose_date("Longhorn") → None`. Parser is correct. The actual leak is at the v2 emit boundary: `jugnu.py:2037-2042` writes the raw producer string back into `available_date` when the parser returns None. The leak is a PRODUCT-CALL FALLBACK, not a parser bug. | When a known-good parser appears to ship garbage, trace the post-parser write path. The leak is almost always in a "fallback" added later, not in the parser itself. |
| **DQ-4** | **Skipped Playwright verification because urllib worked on the index page** | Live-fetched livebh-1.html with urllib, got 461KB body with 26 JSON-LD blocks, declared the Pass-5 parser verified. But urllib lies on JS-hydrated PMS sites — failed_no_data anti-pattern #17 — and many DOM cards (`.floorplan-slide` rent text) only render under Playwright. Got lucky here because livebh DOES inline the data in SSR. | Run Playwright on at least one sample of each new cohort before declaring a fix "verified." urllib output can be misleading. |
| **DQ-5** | **Trusted my own implementation as much as agent implementations I'd seen warned about** | Today I shipped 4 changes (Pass 5, .floorplan-slide, ApartmentComplex sibling fix, two speculative extractors) and only verified 3 live. The two unverified ones were the worst — see DQ-2. | Same standard for own work as for agent work. The "I wrote it five minutes ago" memory doesn't substitute for live verification. |

---

## Phase 1 — The defect taxonomy (TIER_4_LLM_DOM cohort)

For every DQ defect in this cohort, this is the source of truth on root cause and recommended fix path. Reference these by code (A-H + FP/SR).

### A — Rent below $500 (deposit / app-fee leak)

**Population:** 138 unit rows (2.0% of cohort).

**Evidence (live-confirmed):**
- PID **226980** modaatthehill — units: "THE JOPLIN $219 1BR 678sqft", "THE CAMPBELL $246 2BR 855sqft", "The Jefferson $259 2BR 958sqft". LLM response in [shard_17/llm_report/226980.json](../../tmp/run-2026-05-22/shard_17/llm_report/226980.json) emitted only "THE GRANT $1,452 0BR 572sqft" (a single studio at market rate). The 3 deposit-leak rows came from `generic:dom_scan` which ran AFTER the LLM and added them. floor_plans[] on the same property has 7 entries (The Grant + 6 others, no rent) — the cleanly extracted plan-summary partition.
- PID **11762** estellecreek — "B2 $250 2BR 2BA 1004sqft" (clear deposit value)
- PID **22187** mckinley/golfsidelake — "The Spruce $250 4BR 2.5BA 1658sqft" (rent < $300 for a 4BR townhome is impossible at this address)

**Root cause:** when the page has both a "Floor Plans" section (with realistic rent) AND a separate "Fees", "Specials", or "Application" table, the dom_scan tier walks dollar amounts indiscriminately and the lowest-$ amount on the page ends up bound to the plan via a same-table-row heuristic.

**Recommended fix path:** Same-rent guard (see §B below — the two overlap heavily). NOT a hard rent floor at $500: low-income / Section 8 housing has legitimate $400-$500 rents.

### B — Same-rent across ≥3 plans (concession or fee leak)

**Population:** 12 properties (~100 rows).

**Evidence (live-confirmed):**
- PID **11727** risebedfordlake: 13 floor_plans rows ALL at rent_low=$675. The LLM response in [shard_1/llm_report/11727.json](../../tmp/run-2026-05-22/shard_1/llm_report/11727.json) emits `"concession_text": "from $675"` AND `"market_rent_low": 675` on every row. Confidence 0.9. The "from $675" is a marketing banner that the LLM also copied into the rent field. Pure hallucination — same source string mapped to two fields.
- PID **232870** sorrento: 6 rows all at $250
- PID **232788** sussexmanorapts: 13 rows all at $300
- PID **22187** golfsidelake: 15 rows all at $250

**Combined signal with §FP (unit_id presence):** 11 of 13 same-rent properties have **NO real unit_id** (every row is `inferred_*` or None). Only PID 282648 (apolloridge) has real unit_ids `"4518-C"`, `"4516-G"`, `"4518-L"` AND its uniform rent ($720) is plausible for the market. PID 266792 (bellaire) has 3 rows ALL with THE SAME inferred unit_id (`inferred_f7e988ff17dc09cd`) — degenerate identity, almost-certainly bad emit.

**Recommended fix path:**

```
post_process gate (in extraction/classify.py or new module):
  if N_rows_same_rent >= 3
     AND rent_low < $1000  (configurable per-market)
     AND no_real_unit_ids (all are `inferred_*` or None)
     AND concession_text contains f"${rent_low}" or "${rent_low}" (when present):
       → null rent_low/rent_high on all matching rows
       → move rows to plan_summaries partition
       → emit `validate.same_rent_suspect_concession_leak` issue
```

**Failure modes considered:**
- FP risk on uniform-priced low-income housing: mitigated by `no_real_unit_ids` AND `< $1000` gates.
- FP on properties with 3 identical 1BRs at one rate point: very rare; in that case the rate point is also ≥ market, so `< $1000` blocks the fire.

### C — `available_date` contains non-date string (LEAK)

**Population:** 142 unit rows (2.1%).

**Evidence (live-confirmed):**

Top unparseable values in shipped `available_date`:
```
198  'Not Available'
 44  'Date: Available'
 23  'to'
 15  'Only 1 Vacant Apartment Left!'
 14  '/ month'
 11  'Dec. 2'
 10  'Only 2 Vacant Apartments Left!'
 10  'Open for Application on ____'
  9  'Available'
  9  'Sign Waitlist'
  +  plan names: 'Longhorn', 'Palmwood', 'Hastings', 'Carlisle', ...
```

**Root cause:** [`scripts/runners/jugnu.py:2037-2042`](../scripts/runners/jugnu.py#L2037-L2042) (added 2026-05-21 as a "product call"):

```python
if avail_date_norm is None and raw_available_date:
    fallback = _normalize_raw_date(raw_available_date)
    if fallback:
        avail_date_norm = fallback[:32]
```

Intent: preserve un-parseable but date-shaped producer strings ("Late August", "Spring 2026"). Side effect: ships ANY raw producer string through unfiltered.

**`format_loose_date` itself is correct** — `format_loose_date("Longhorn") → None`, `format_loose_date("Only 2 Vacant...") → None`. The leak is at the fallback gate.

**Recommended fix path:**

1. Replace the unconditional fallback with a `looks_date_like(s: str) -> bool` shape predicate. The predicate accepts when ANY of:
   - Contains a month-name (jan/feb/mar/.../december or 3-letter abbrev)
   - Contains digits adjacent to slash/dash (date numerics)
   - Contains a season word (spring/summer/fall/autumn/winter)
   - Contains a date-relative word (early/mid/late) adjacent to a month name
   - Contains `\b(?:now|asap|immediate|today|soon)\b` (already DATE_NOW_TOKENS material)
   - Matches `\bend\s+of\s+(?:the\s+)?(?:month|year|week)\b`
2. Only allow the raw fallback when `looks_date_like(raw)` is True.
3. **Extend `format_loose_date`** to parse the date-shaped strings the current parser misses:
   - `07/24` / `6/15` (m/d without year — back-fill current year, roll forward if past)
   - `Available 07/24` (same shape with producer prefix)
   - `end of month` → last day of current month
   - `Mid June` → 15th, `Late August` → 25th, `Early 2027` → year-only fallback (Jan 1 of year)
   - `Spring 2026` / `Summer 2026` → mid-season estimate (mid-March / mid-July)
4. **Emit telemetry** (`extract.date_unparsed_shape`) with each rejected raw value so we can grow the parser surface from real data.

**Failure modes considered:**
- A real producer value like "Available end of August 2026" should parse; the predicate must not reject it for being long.
- Don't auto-coerce vague strings ("soon", "asap") to today's date without an explicit AVAILABLE-NOW context — currently the parser does this for `DATE_NOW_TOKENS`. Audit which terms should resolve to today vs. None.

### D — `availability_status='WAITLIST'`

**Population:** 21 rows (TIER_4_LLM_DOM) + 1 (AppFolio SSR) + 1 (RentCafe Nestin) = 23 total.

**Root cause:** non-canonical enum value. The canonical set is AVAILABLE/UNAVAILABLE/UNKNOWN.

**Recommended fix path:**

1. Add `_avail_subtype` to the unit dict — a free-text annotation for sub-categories (WAITLIST, COMING_SOON, FUTURE, RESERVED, OFF_MARKET).
2. Canonicalize WAITLIST → `availability_status=UNAVAILABLE`, `_avail_subtype="WAITLIST"`.
3. Apply at every tier's normalization layer, not just LLM_DOM — appfolio/rentcafe nestin also emit WAITLIST occasionally.
4. The LLM prompt (`config/prompts/dom_analysis.txt` / `api_analysis.txt`) should be updated with the **canonical enum + subtype list** so the LLM emits the same shape. Currently the prompts don't constrain status values, which is why WAITLIST leaks through.

**Other status variants worth recognizing in the same change:**
- "WAITLIST", "WAIT LIST", "WAIT_LIST"
- "COMING SOON", "COMING_SOON", "RESERVED"
- "OCCUPIED", "LEASED", "RENTED" (all UNAVAILABLE)
- "PENDING", "ON HOLD"
- "MODEL UNIT" (UNAVAILABLE; subtype=MODEL)
- "DOWN", "MAINTENANCE", "OFF_MARKET"

### E — `unit_id == floor_plan_name`

**Population:** 29 rows. Canonical PIDs: 229986 theadleylife (uids: A1/A2/A3/B2/C1), 254187 residecrosbyhill (uids: SCH1/SCH2/SCH3/SCH4/SCH4.1).

**Root cause:** the LLM emits `unit_id` with the plan code (A1, B2, SCH1) because those are the only identifiers visible on the marketing page. These are plan codes, not per-unit identities.

**Recommended fix path:** post_process gate — when `unit_id` lowercases-equals `floor_plan_name` lowercase, null the unit_id and route the row through `assign_fallback_unit_id` (already handles `inferred_*` SHA256). Emit `extract.unit_id_equals_plan_name` issue (INFO) with the offending value.

**Failure modes considered:** A property with a single unit per plan might legitimately use the plan code as the unit identifier. Rare; the fallback id-hash still attributes the row correctly.

### F — `fpn` long + joined

**Population:** 99 rows.

**Examples:** `"A3 - Wellesley - Lenox Village & Regent"`, `"Hermitage A3 - Retreat at Lenox Village"`, `"VPBA - 2 Bed 1 Bath Upstairs Hybrid"`, `"2 Bed 2 Bath 1156 SqFt (1062 Net)"`.

**Recommended fix path:** TELEMETRY ONLY for the first 2 weeks. Auto-strip-after-first-hyphen risks corrupting legitimate names like `"Garden View - Upstairs"`. Emit `extract.floor_plan_name_long` (INFO) with the offending string. Cluster the issues weekly, then write per-template strip rules.

### G — `beds=0` + no "studio" in fpn

**Population:** 198 rows. Examples: PID 10182 fpn=`"A1"` beds=0, PID 19535 fpn=`"canterbury"` beds=0, PID 21349 fpn=`"Piquin"` beds=0.

**Recommended fix path:** TELEMETRY ONLY. Auto-renormalize from name is risky (`"S3A"` could be Studio Plan A or 3-bedroom Plan A — unclear without context). Emit `extract.beds_zero_no_studio_name` (INFO) and use the data to build per-template inference rules.

### FP — units=[] but floor_plans!=[]

**Population:** 233 properties (17.1%).

**Live-confirmed pattern:** Most are correct — the LLM correctly returned `market_rent_low=None`, the page genuinely does not display rent.

- PID **19558** henryonthepark: 14 rows, all `market_rent_low=None availability_status=UNKNOWN` — pure plan summary
- PID **11112** risewestarlington: 6 rows, all rent=None
- PID **20672** quietwaterslanding: 8 rows, all rent=None AND sqft=None — incomplete but honest

**Recommended fix path:** NO CHANGE NEEDED on the FP path itself. The §8.18 v2-formatter fix is already shipping plan_summaries correctly. But: investigate the 8 entrata-detected fp-only properties (PIDs 11112, 11543, 11727, 19939, 20672, 20770, 20551, 21129) — these are Rise-branded entrata properties where the existing entrata adapter ought to be returning real per-unit data. Add to the entrata adapter audit backlog.

### SR — `available_date` fill at 56.9% (the biggest single gap)

**Population:** 2,964 rows missing available_date (43% of cohort).

**Sub-cause split** (from playbook §19.1 in failed_no_data):
- A_API_FLOORPLANS_ONLY (116): TIER_1_API won; per-unit endpoint not captured
- B_PAGE_NO_DATES (991): zero date signals in any captured HTML
- C_LLM_SECTION_MISSED (70): TIER_4_LLM_DOM won; dates exist in HTML
- D_DOM_ATTRS_IGNORED (1): API won but `data-availability` attrs ignored
- E_AVAILABLE_NOW_NO_FALLBACK (69): "Available Now" text seen, no fallback
- OTHER (213)

The C and E buckets are the ones we can directly improve at the DOM layer. C is the canonical TIER_4_LLM_DOM gap — the LLM picked a tight section without the date column. The §19.1 F7c fix (`_widen_to_include_date_column`) addresses C; E needs the "AVAILABLE_NOW_NO_FALLBACK" path in classify.py.

---

## Phase 2 — What was shipped today (2026-05-23)

Three changes shipped, all live-verified against [livebh-1.html](C:/tmp/llm_dom_samples/livebh-1.html) + [livebh-2.html](C:/tmp/llm_dom_samples/livebh-2.html):

### Shipped 1 — JSON-LD Pass 5: `ApartmentComplex.accommodationFloorPlan[]`

**File:** [`ma_poc/pms/adapters/_html_extract.py`](../pms/adapters/_html_extract.py) — new function `_extract_accommodation_floorplans_as_units` at module level; wired into `extract_jsonld_from_html` after Pass 4.

**Schema shape:**
```json
{"@type": "ApartmentComplex",
 "accommodationFloorPlan": [
   {"@type": "FloorPlan",
    "name": "B1",
    "numberOfBedrooms": "2",
    "numberOfBathroomsTotal": "2",
    "floorSize": "1040",
    "numberOfAvailableAccommodationUnits": "4",
    "url": "https://.../B1"}
 ]}
```

**Cohort:** livebh.com (32 properties) confirmed. Likely covers many RentCafe vanity sites with the same canonical JSON-LD shape. Rent is NOT in this schema (lives in DOM) — Pass 5 emits with `market_rent_low=None`.

### Shipped 2 — Pass 1 phantom-shell fix: reject `ApartmentComplex` siblings with QuantitativeValue-range `numberOfBedrooms`

**File:** [`ma_poc/pms/adapters/_html_extract.py`](../pms/adapters/_html_extract.py) — the inline check at the for-item loop in `extract_jsonld_from_html`.

**Bug closed:** livebh.com homepage lists 24 sibling `ApartmentComplex` nodes (one per related community), each carrying `numberOfBedrooms: {@type: QuantitativeValue, minValue: 1, maxValue: 2}` (a property-LEVEL range, not a per-unit value). Pre-fix, these 24 nodes shipped as 24 phantom "units" with only `floor_plan_name="The Arbors on Forest Ridge"` etc.

**Verified:** before fix livebh-1 returned 26 units (24 phantoms + 3 real); after fix returns 3 real units.

### Shipped 3 — `.floorplan-slide` DOM extractor + plan-text regex

**File:** [`ma_poc/pms/adapters/_html_extract.py`](../pms/adapters/_html_extract.py) — new `_extract_floorplan_slide_card` function + registration in `_COMPACT_ROW_EXTRACTORS` and `_DOM_CONTAINER_SELECTORS`. Also added to `_PRIORITY_LISTING_SELECTORS` in [`ma_poc/pms/adapters/generic.py`](../pms/adapters/generic.py).

**Card text shape:** `"2 Bed | 2 Bath  $1,193 - $1,416  plus fees  1040 sq. ft.  available units: 4"`

**Verified:** livebh-1 → 3 plans extracted with beds/baths/sqft/rent_range/rent_low/rent_high/available_units. livebh-2 → 8 plans.

**Caveat:** `floor_plan_name` comes from page context (property name like "Ashford Apartments") not from the card. The canonical plan name (B1, C1, A1) only lives in the JSON-LD Pass-5 path. A future merge step matching by sqft+beds+baths can join them.

---

## Phase 3 — What was reverted (and why)

### Reverted 1 — `.dmContent` DudaMobile extractor

**Reason:** zero live-verification evidence. The DudaMobile signature came from ONE saved-selector profile out of 66 locally-stored profiles. No cohort survey, no live-fetched HTML showing `.dmContent` on a current production property. Could easily be dead code with maintenance cost.

**Anti-pattern category:** DQ-2 (shipped speculative without live verification).

### Reverted 2 — `[data-selenium-id^='Rent_']` extractor

**Reason:** lincolnatwolfchase.com (the property whose saved profile suggested `[data-selenium-id^='Rent_']`) live-fetches with `mobile_*`/`address_*`/`SID_*`/`click_*` prefixes ONLY. The `Rent_*`/`Sqft_*`/`Bed_*` prefixes live on the SecureCafe portal sub-pages, which production can't reach due to cross-origin CF clearance asymmetry (failed_no_data §20.3).

So the selector would match the SecureCafe portal HTML if we ever rendered it — but we don't render it from production. Dead code from prod perspective.

### Reverted 3 — `[class*='fp-availability']` container

**Reason:** the substring selector matches any class containing `fp-availability` — including things like `fp-availability-disclaimer` or `fp-availability-banner` that aren't unit containers. Saved-profile occurrence count was 2 — too narrow to justify the false-positive risk.

---

## Phase 4 — The roadmap (ranked by impact × confidence)

### T1 — Ship now (after one more live verification per item)

| # | Fix | Defect addressed | LOC est. | Live verification needed |
|---|---|---|---:|---|
| **T1.A** | Fix the `jugnu.py:2037` available_date fallback: add `looks_date_like()` predicate. Reject raw fallback when shape isn't date-like. | C (142 rows) | ~40 | Add 8-10 unit tests covering "Longhorn", "Only 2 Vacant", "Late August", "end of month", etc. |
| **T1.B** | Extend `format_loose_date` for `m/d` (no year, backfill), `end of month`, `Mid/Late/Early month`, `Spring/Summer 2026`. | C (sub-set) + SR (some) | ~80 | Unit tests for each new shape against producer surface |
| **T1.C** | Same-rent guard at post_process: when ≥3 rows share rent AND no real unit_ids AND rent < $1000, null rent + move to plan_summaries. | B (12 props, ~100 rows) | ~60 | Replay one same-rent property (PID 11727) through post_process with the gate active; ensure plan_summaries gets the rows |
| **T1.D** | Canonicalize availability_status: WAITLIST→UNAVAILABLE+`_avail_subtype=WAITLIST`. Apply at every tier's normalization. | D (23 rows across tiers) | ~30 | Test fixtures: WAITLIST/COMING SOON/RESERVED/LEASED/MODEL UNIT |
| **T1.E** | Null unit_id when it equals floor_plan_name (case-insensitive). Route through `assign_fallback_unit_id`. | E (29 rows) | ~15 | Unit test: `unit_id="A1" fpn="A1"` → unit_id=None, `_inferred_id=True` after re-id |
| **T1.F** | Pass 5 enhancement: when `accommodationFloorPlan[i]` has `offers.price`, route through `_build_unit_from_offer` instead of hard-coded `market_rent_low=None`. | Defensive | ~20 | None — defensive enrichment |
| **T1.G** | Update LLM prompt templates (`api_analysis.txt`, `dom_analysis.txt`, `tier4_extraction.txt`) with the canonical status enum + subtype list. Also constrain rent to NOT echo concession_text dollar amounts. | B, D (preventive) | ~10 (prompt edits) | Compare LLM responses before/after on 5 canonical PIDs |

### T2 — Telemetry first, ship rules later

Two-week telemetry campaign to collect real producer surface BEFORE writing rules:

| # | Telemetry | What it measures | Consumer |
|---|---|---|---|
| **T2.A** | `extract.date_unparsed_shape` | Every raw producer string that fails `format_loose_date` AND `looks_date_like` | Weekly cluster analysis → grow `format_loose_date` surface |
| **T2.B** | `extract.floor_plan_name_long` | Every fpn > 35 chars with multiple ` - ` separators | Cluster by template → per-template strip rules |
| **T2.C** | `extract.beds_zero_no_studio` | Every row with beds=0 and no "studio"/"efficiency" in fpn | Cluster by template → identify recoverable cases |
| **T2.D** | `extract.same_rent_property` | Every property with all-same-rent across ≥3 plans, regardless of rent value | Refine the < $1000 floor; identify legitimate uniform-pricing cases |
| **T2.E** | `extract.unit_id_equals_plan_name` | Every row where unit_id lowercases-equals fpn | Confirm the 29-row cohort is uniform; identify exceptions |
| **T2.F** | `extract.concession_to_rent_leak` | Property has rent==dollar-amount-in-concession_text | Validate the "from $675" → rent=675 hallucination hypothesis |
| **T2.G** | `extract.status_non_canonical` | Every status value outside {AVAILABLE, UNAVAILABLE, UNKNOWN} | Build the complete subtype enum from real data |

All telemetry events go to `data/runs/<date>/issues.jsonl` with severity INFO so they don't pollute the error stream but are queryable.

### T3 — Deterministic adapter capture (the LLM-reduction track)

These are the **per-cohort deterministic adapters** that progressively absorb TIER_4_LLM_DOM properties:

| # | Cohort | Property count | Status |
|---|---|---:|---|
| **T3.A** | livebh.com (Splide + RentCafe vanity JSON-LD) | 32 | ✅ Shipped 2026-05-23 (Pass 5 + .floorplan-slide) |
| **T3.B** | weidner.com (RentCafe vanity, 17 props, 12.3 units_avg) | 17 | TODO — investigate template + per-unit data shape |
| **T3.C** | byredwood.com (RentCafe vanity, 14 props, ratio 130:1 in cloud) | 14 | TODO — investigate JS-injected SSR blob |
| **T3.D** | venterraliving.com (sightmap-detected, parent-marketing) | 9 | Tracked in failed_no_data §Bug #4 |
| **T3.E** | equityapartments.com (sightmap-detected) | 8 | Investigate why `.unit-availability-tile` selector isn't catching |
| **T3.F** | princetonmanagement.com (unknown PMS, 7 props) | 7 | TODO — live forensic |
| **T3.G** | hgliving.com (knock-detected, 6 props) | 6 | TODO |
| **T3.H** | encoreskyline_template (41 still in LLM_DOM) | 41 | TODO — adapter cascade gap; sightmap captures 87, LLM_DOM gets 41 |
| **T3.I** | RentCafe `wp_probe` 403 fix (`/wp-json/middleware/v1/getFloorplans/...`) | 100s | TODO — same-origin 403 needs header/identity investigation |
| **T3.J** | Nestin `nu-floor-plan` variant template | 100s | TODO — `nestin_recover` returns `template_matched_no_units` on many props |

Per-cohort live-forensic protocol (per anti-pattern DQ-2): for each cohort, before writing the adapter:
1. Find 5 PIDs in the cohort using the [successes.csv filter](../data/reports/cloud_run_2026-05-22/successes.csv)
2. Live-fetch (with Playwright if SSR-shell-only) each PID and grep the rendered HTML for the proposed selector
3. ONLY proceed if ≥4 of 5 PIDs have the selector with the expected shape
4. Pin a unit test against fixtures from at least 2 of the verified PIDs

---

## Phase 5 — Investigation playbook (for future DQ debugging sessions)

Use this checklist when investigating a new DQ defect class:

### Step 1 — Cohort scope

```python
# Filter successes.csv by terminal_tier + verdict + defect signal
# Count rows AND properties — they're different metrics
# Look at PMS detection + management-company root domain
```

Outputs you need before forming a hypothesis:
- Defect row count and percentage of cohort
- Distinct property count
- Top management-company roots
- Distribution by PMS detection
- Distribution by other defect classes (do defects co-occur?)

### Step 2 — Read 3-5 PIDs at random from the defect cohort

For each:
1. Read [`properties.json`](../../tmp/run-2026-05-22/) — the actual shipped unit/floor_plans data
2. Read [`llm_report/{pid}.json`](../../tmp/run-2026-05-22/) if it exists — the LLM's raw response
3. Read [`events.jsonl`](../../tmp/run-2026-05-22/) for tier sequence
4. Live-fetch the URL (with Playwright if static fetch gives < 50 KB text)
5. Find the actual rendered HTML chunk that produced the defect

### Step 3 — Distinguish LLM hallucination from adapter bug

For each defect row, identify whether the value came from:
- The LLM response (find it in `raw_response` text) — hallucination class
- A non-LLM tier (dom_scan, jsonld, api_*) — adapter bug class
- A post-process step (classify, schema_v2 emit, jugnu v2 formatter) — formatter bug class

The fix path differs:
- **Hallucination** → prompt edit + LLM response post-validation
- **Adapter bug** → adapter code fix + regression test against fixture
- **Formatter bug** → schema_v2 or jugnu.py edit + contract test

### Step 4 — Confirm with a SECOND PID before proposing a fix

The first PID often has confounding factors. Always confirm the root cause hypothesis on a different PID before writing code.

### Step 5 — Failure-mode catalog BEFORE writing code

For every proposed fix, write down at least 3 failure modes:
- Where could this misfire as a false positive?
- What if the producer-side template changes next week?
- What if a different LLM model emits a slightly different shape?

If you can't think of 3 failure modes, you don't understand the fix well enough yet.

---

## Phase 6 — Verification protocols

Before shipping any DQ fix:

1. **Unit test** the parser/validator against fixtures from at least 2 distinct PIDs in the defect cohort
2. **Replay test**: take the property's `_extract_result` from a real cloud run, run it through the new code path, verify the defect is closed AND no new rows are dropped
3. **Live-fetch verification** if the fix references specific selectors / URLs / patterns — fetch at least 5 sample PIDs and confirm the selector matches the expected count
4. **Cold canary** on a 32-PID sample (drawn from yesterday's defect cohort + 4 sentinels)
5. **REGRESSED == 0** is non-negotiable

After shipping:
6. The next day's cloud run analysis MUST show the defect count dropping by at least the expected amount; if not, the fix didn't fire and needs investigation (per failed_no_data Phase 0 anti-pattern "Assuming undeployed when numbers don't move")

---

## Phase 7 — File reference

| What | Where |
|---|---|
| **DQ defect sources** | |
| `available_date` fallback leak | [`scripts/runners/jugnu.py:2037-2042`](../scripts/runners/jugnu.py#L2037-L2042) |
| Date parser | [`extraction/dates.py`](../extraction/dates.py) (`format_loose_date`, `DATE_PREFIX_RE`, `DATE_NOW_TOKENS`, `DATE_ABSENT_TOKENS`) |
| Schema gate date validation | [`validation/schema_gate.py:207-260`](../validation/schema_gate.py#L207-L260) |
| Schema v2 emit | [`core/schema_v2.py:298`](../core/schema_v2.py#L298) (`available_date`), `_format_date` line 483 |
| Post-process classify | [`extraction/classify.py`](../extraction/classify.py) |
| Sanity bounds | [`extraction/sanity.py`](../extraction/sanity.py) (rent ∈ [200, 50000], etc.) |
| **Deterministic adapters shipped 2026-05-23** | |
| JSON-LD Pass 5 `accommodationFloorPlan` | [`pms/adapters/_html_extract.py`](../pms/adapters/_html_extract.py) (`_extract_accommodation_floorplans_as_units`) |
| Pass 1 ApartmentComplex sibling guard | [`pms/adapters/_html_extract.py`](../pms/adapters/_html_extract.py) (inline in `extract_jsonld_from_html`, search "QuantitativeValue range") |
| `.floorplan-slide` DOM extractor | [`pms/adapters/_html_extract.py`](../pms/adapters/_html_extract.py) (`_extract_floorplan_slide_card` + `_COMPACT_ROW_EXTRACTORS` registration) |
| Priority listing selectors | [`pms/adapters/generic.py:471`](../pms/adapters/generic.py#L471) (`_PRIORITY_LISTING_SELECTORS`) |
| DOM container selectors | [`pms/adapters/_html_extract.py:1648`](../pms/adapters/_html_extract.py#L1648) (`_DOM_CONTAINER_SELECTORS`) |
| **Sample data + samples** | |
| Live-fetched DQ samples | [`C:/tmp/dq_samples/`](C:/tmp/dq_samples/) (raw HTML from urllib) |
| Live-fetched LLM_DOM samples | [`C:/tmp/llm_dom_samples/`](C:/tmp/llm_dom_samples/) (raw HTML from urllib) |
| Cloud run 2026-05-22 mirror | [`C:/tmp/run-2026-05-22/`](C:/tmp/run-2026-05-22/) (100 shards, properties.json + events.jsonl + llm_report/ each) |
| Successes / failures CSVs | [`data/reports/cloud_run_2026-05-22/`](../data/reports/cloud_run_2026-05-22/) |
| **Related playbook** | |
| FAILED_NO_DATA debugging | [`docs/failed_no_data_debugging_playbook.md`](failed_no_data_debugging_playbook.md) — anti-patterns, tier-label decoder, cohort diagnostics |

---

## Appendix A — Glossary (DQ-specific)

- **Deposit-leak / fee-leak** — when an application-fee, admin-fee, or first-month-special dollar amount (typically $200-$500) gets routed into `rent_low` instead of the actual market rent. Confirmed cause of 138 sub-$500 rows in run 2026-05-22.
- **Concession-leak** — when a marketing-banner amount like "from $675" gets duplicated by the LLM into BOTH `concession_text` AND `market_rent_low` on every plan. Confirmed on PID 11727 risebedfordlake (13 plans all $675).
- **Phantom-shell row** — a unit row that has only `floor_plan_name` set and no dimensions. Source: pre-2026-05-23 Pass-1 JSON-LD walker emitting `ApartmentComplex` sibling-property metadata as 24 phantom units. Fixed by the QuantitativeValue-range guard.
- **All-same-rent property** — a property where ≥3 distinct floor plans (different beds/baths/sqft) all carry the SAME `rent_low`. Strong signal of concession or fee leak.
- **Inferred unit_id** — a SHA256-derived identity hash (e.g., `inferred_92bce85e603ffb81`) used when the source had no real apt number. 11 of 13 same-rent properties have ONLY inferred unit_ids → they're plan-level rows pretending to be unit-level.
- **fp-only-emit** — a property whose v2 output ships `units=[]` AND `floor_plans` non-empty. Mostly correct behavior (LLM correctly didn't fabricate rent when the page didn't have it); not always a bug.
- **looks_date_like (proposed predicate)** — shape check that gates the `available_date` raw-fallback in jugnu.py. Accepts month-name, slash-date numerics, season words, date-relative words; rejects everything else.
- **`_avail_subtype` (proposed field)** — sibling field to `availability_status` carrying sub-categories (WAITLIST, COMING_SOON, RESERVED, MODEL, etc.) without breaking the canonical 3-value enum.

---

## Appendix B — Quick-reference: today's PID inventory

PIDs cited in this playbook with their defect class. Useful as canary set:

| PID | URL | Defect class | Sample evidence |
|---|---|---|---|
| 226980 | modaatthehill.com | A (deposit-leak) | THE JOPLIN $219 1BR; LLM emitted only THE GRANT $1,452 |
| 11762 | estellecreek.com | A | B2 $250 2BR 2BA 1004sqft |
| 232870 | sorrentoapartmentsfl.com | A + B | 6 rows all at $250 |
| 22187 | mckinley.com (golfsidelake) | A + B | 15 rows all at $250; The Spruce $250 4BR |
| 11727 | risebedfordlake.com | B + FP | 13 plans all $675; concession_text="from $675" confirmed in LLM response |
| 232788 | sussexmanorapts.com | B | 13 rows all at $300 |
| 266792 | bellaire.appianwayapthomes.com | B + E | 3 rows ALL with same inferred unit_id |
| 282648 | apolloridgeapts.com | B (legit) | 3 rows at $720 with REAL unit_ids "4518-C/G/L" — keep |
| 10496 | liveatgraysonpark.com | C | available_date="Longhorn", "Palmwood" |
| 19535 | livewindsorcourt.com | C | available_date="Date: Available" |
| 1973 | rosslynheights.com | C | available_date="Open for Application on ____" |
| 11797 | wraynorthdallas.com | C | available_date="Only 2 Vacant Apartments Left!" |
| 229986 | theadleylife.com | E | 5 rows with unit_id == floor_plan_name (A1/A2/A3/B2/C1) |
| 254187 | residecrosbyhill.com | E | SCH1/SCH2/SCH3/SCH4/SCH4.1 |
| 11112 | risewestarlington.com | FP | 6 plans all rent=None — correctly classified |
| 19558 | henryonthepark.com | FP | 14 plans, all rent=None — pure plan summary |
| livebh.com (32 PIDs) | livebh.com/apartments/* | (cohort) | Pass 5 + .floorplan-slide shipped today |
