# 1000-property Step 3c lift validation — 2026-05-24

End-to-end scale validation of the **Step 3c rendered-DOM concession
rescan** (shipped earlier today, see `ma_poc/core/rendered_dom_concession.py`
and `scraper.py` Step 3c). Goal: confirm the 9 pp lift seen in the
100-prop vision audit holds — and isn't a small-sample artefact — at
10× scale.

## Methodology

- Sample: **1,000 properties** drawn uniformly at random from the
  4,604 prop CSV (seed `202605241000`, distinct from the 50- and
  100-prop samples).
- For each property, run BOTH paths in parallel:
  1. **STATIC** — `curl_cffi chrome120` fetch → strip `<script>`/
     `<style>` → apply `_PROPERTY_CONCESSION_RE` → sentence-window
     extraction. Mirrors scraper.py Step 3 verbatim.
  2. **RENDERED** — headless-Chromium navigate → wait `domcontentloaded`
     → wait `networkidle` (8 s cap) → +2 s settling → `page.evaluate(
     RENDERED_DOM_PROBE_JS)` → `find_concession_in_blocks` with the
     SAME `_PROPERTY_CONCESSION_RE`. Mirrors Step 3c verbatim.
- Concurrency 8. Total wall: ~17 min.
- Record both `static_text` / `rendered_text` per property so the
  Step 3c lift is the count where `rendered_text != None AND
  static_text is None`.

## Aggregate results (n = 1,000)

| Metric | Value |
|---|---:|
| Static-only captured | **309** (30.9 %) |
| Rendered-only captured | **452** (45.2 %) |
| **Captured by BOTH** | 288 |
| **Step 3c LIFT** (rendered ∧ ¬ static) | **164** |
| Static only (rendered missed) | 21 |
| Neither captured | 519 |
| Error in static fetch | 10 |
| Error in render | 8 |

| Coverage | Before Step 3c | After Step 3c | Delta |
|---|---:|---:|---:|
| Capture rate | **30.9 %** | **47.3 %** | **+16.4 pp** |
| Net new captures | 309 | 473 (309 + 164) | **+53 %** relative |

## Precision (dual-captured agreement)

288 properties were captured by BOTH paths. Comparing the resulting
`offer_type` taxonomy from `extract_offer()`:

- **offer_type agreement: 259 / 288 = 90 %**
- The 29 disagreements are mostly cases where the static and rendered
  texts overlap but differ in length, so the offer-classifier sees a
  slightly different anchor (e.g. static caught the headline
  "Limited Time Offer" while rendered caught the body "Get 1 month
  free"). Both are correct; just classified into different buckets.

## The 164 Step 3c wins — sample

These are all properties where the offer text was **physically absent
from the static HTML curl_cffi fetched**, but visible after the page
hydrated:

| pid | Property | Offer (from rendered DOM) |
|---|---|---|
| 71141 | Cathedral Park | "SIX WEEKS FREE, YOU CAN'T AFFORD TO MISS OUT…" |
| 261093 | The Launch | "Now offering 6 weeks free base rent on select homes" |
| 281679 | The Retreat at Eastlake | "Lock in $500 OFF 1st month + REDUCED RATES as low as $1350/m" |
| 42743 | Indigo | "LEASING SPECIAL — One month free base rent" |
| 61721 | Villages of Magnolia | "ONE MONTH FREE WHEN YOU LOOK AND LEASE!*" |
| 296984 | Aspire Naples | "Now offering two months free, call today!" |
| 78956 | Redwood Tipp City | "GET $750 OFF AT MOVE-IN!" |
| 11495 | The Palisades at Bear Creek | "Enjoy $500 off your first 1 full month!" |
| 265767 | Journee | "$500 Off if Applying Same Day as Tour! One Month…" |
| 76504 | Ashton at Harding | "1 month FREE on select homes!" |
| 271348 | Alta Rochelle | "RENT SPECIAL! Up To 3 Months Free base rent!" |
| 62782 | Cortland Alameda Station | "2 Months Free + 2 Months Free Parking!" |
| 16634 | Residence at Barrington | "$250.00 OFF MOVE-IN + REDUCED DEPOSIT" |
| 62958 | Madison at Westinghouse | "MOVE-IN SPECIAL" |
| 39333 | Hollow Tree Park | (hero/banner text) |

(All 164 logged in `/tmp/sweep1000_results.jsonl`.)

## The 21 Step 3c misses — what static caught but rendered didn't

Not a regression (we use OR — static OR rendered — so these are still
captured by the static path), but worth understanding:

- Banner closed by a JS handler before our 2 s settling window
- Banner outside our queried popup/modal/banner class set (rare —
  most likely root cause)
- Render hit a timeout / DOM-content-loaded race (8 of 21 had an
  explicit render error)

The 13 non-erroring misses have shapes like `"MOVE IN SPECIAL"`,
`"Limited-time offer"` — all real concessions, all caught statically.
Step 3c is **additive**, not a replacement; both layers fire.

## Offer type distribution (extract_offer taxonomy)

| Offer type | Static | Rendered |
|---|---:|---:|
| free_rent | 173 | **256** |
| dollar_off | 58 | **79** |
| look_and_lease | 7 | **36** |
| waived_fee | 8 | **13** |
| reduced_rate | 5 | 4 |
| percent_off | 3 | 1 |
| reduced_deposit | 1 | 2 |

The rendered path surfaces dramatically more `look_and_lease` (7 →
36) — operators love to bundle "lease today and get X" into a popup
banner that mounts post-load.

## Conclusion

The 100-prop vision audit projected 9 pp lift. At 10× scale the
realised lift is **16.4 pp** — meaningfully higher because the
100-prop sample under-counted long-tail JS-injected operators. Step
3c is doing real work: 164 properties' worth of concession capture
that the static pipeline was systematically missing.

Combined with the **AppFolio SSR unit_number fix** shipped today
(audit row resolution), this is the largest single-day quality
movement of the grind.

## Artifacts

- `/tmp/sweep1000_results.jsonl` — per-property record (static_text,
  rendered_text, offer_type both ways, errors)
- `/tmp/sweep1000_summary.json` — aggregate
- `/tmp/sweep_1000.py` — the parallel runner
- `ma_poc/core/rendered_dom_concession.py` — Step 3c module
- `ma_poc/pms/scraper.py` (Step 3c section) — wiring
- `ma_poc/tests/core/test_rendered_dom_concession.py` — 28 tests
