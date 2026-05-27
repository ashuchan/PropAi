# Bucket-B grind — findings (2026-05-22)

186 true-extraction-gap properties (fetched clean, extracted 0, report-21 got
them non-LLM). Probed in two passes.

## Pass 1 — page-shape clustering (from pipeline html_characterized)

| shape | n |
|---|---|
| high-SPA, no framework | 59 |
| has JSON-LD, low rent | 54 |
| SSR with rent signals | 36 |
| ssr low-signal | 17 |
| Wix/Angular/Squarespace/Next | 18 |

## Pass 2 — what each cohort actually needs

**JSON-LD cohort (54): DEAD END.** Bulk-fetched + parsed all 54. 52/54 carry
property-level schema only (ApartmentComplex + AggregateRating + geo) — no
floorplan rent/sqft. A JSON-LD parser fix would help 2 properties. Not viable.

**SSR-with-rent cohort (36): a parser bug, not a selector/adapter gap.**
Bulk-fetched all 36, analysed the DOM around every `$rent` node. The shallow
class-signature clustering suggested ~8 template clusters — but verifying the
candidate container selectors against real HTML collapsed that to ~2 genuinely
clean repeating containers (`.apartment-info-block`, `.floor_plan`). And even
those, when fed to `extract_units_from_dom`, produced GARBAGE units:

  - redoak `.apartment-info-block`: container text is clean —
    "Price Range $1785 ~ $1987 BR 2 ... SqFt 830 ... Avail 5/22/2026" —
    but `_container_yields_unit` emitted `bedrooms: 1987`. Root cause:
    `_BEDS_PATTERN` (number-before-"BR") matched the rent `$1987` because the
    text reads "$1987 BR 2"; the real "BR 2" (number-AFTER) is not matched.
  - creekview `.floor_plan`: container text "Rent: $808 Deposit: $300" —
    parser grabbed both → `rent_range: 300-808`. The $300 deposit contaminates
    rent because there is no Deposit-label exclusion.

## Conclusion — the real bucket-B fix

Bucket B's SSR cohort is **not** a missing-selector or missing-adapter problem.
The containers ARE found and ARE clean. `_container_yields_unit` mis-parses
three common text formats:

1. `$X ~ $Y` tilde rent ranges — and `$Y BR` makes the beds regex eat the rent
2. label-first `BR N` / `SqFt N` / `Beds N` — not matched at all (regex is
   number-first only)
3. `Rent: $X  Deposit: $Y` — deposit contaminates rent (no label exclusion)

Fixing `_container_yields_unit` for these 3 formats is a single, deterministic,
high-leverage change — it helps every dom_scan extraction, not just bucket B.
It is a hot shared function, so the fix needs the full dom_scan regression
suite green before shipping.

Fixtures saved for the fix + its tests:
  - ma_poc/tests/fixtures/bucketb/apartment_info_block_redoak.html (7 plans)
  - ma_poc/tests/fixtures/bucketb/floor_plan_creekview.html (7 plans)

## Remaining cohorts (not yet worked)

- 59 high-SPA — data JS-rendered; need RENDER-mode probing
- 17 ssr-low-signal + 18 framework — per-case
- The `.suite` (RentManager SSR) — needs a plan/unit-context parser, not dom_scan

## Pass 3 — the 59 high-SPA cohort (2026-05-22)

- **All 59 WERE rendered** by our pipeline (>=1 RENDER fetch each) — so it is
  NOT a render-routing gap. We rendered and dom_scan still got nothing.
- Bulk-probed conventional deep URLs: **6/59** have an SSR sub-page (curl gets
  rent+struct) — those ride the dom_scan parser fix once link-hop reaches them
  (`/floor-plans`, `/floorplans`, `/apartments`).
- **53/59 genuinely need rendered-DOM extraction.** Chrome-probed a sample:
  heterogeneous custom JS / WordPress sites — iframe widgets
  (`rentpro.rpa5.com/availibility/avapage.a5w`), WP-plugin sites (mmc-gallery /
  Greystar), conventional `/floorplans` paths 404. **No dominant cluster.**

### CORRECTED verdict — the SPA cohort is known-PMS-widget sites

The iframe-only probe was too shallow (iframes are just googletagmanager/maps).
Checking SCRIPT/LINK hosts across all 59 reveals heavy known-PMS-widget
presence — the widgets load via <script>, not <iframe>:

  19  cs-cdn.realpage.com          (RealPage CWS widget)
  15  commoncf.entrata.com         (Entrata widget)
  14  onlineleasing.realpage.com   (RealPage OLL)
  12  repli360.com / app.repli360  (Repli360)
   8  integrations.funnelleasing   (Funnel)
   8  widgets.g5dxm.com            (G5)
   8  siteassets.parastorage.com   (Wix)

So the 59 SPA cohort is NOT a heterogeneous long tail — it is mostly sites
embedding KNOWN PMS widgets, and we ALREADY have adapters for every one of
those (realpage_cws, realpage_oll, entrata, repli360, _funnel, g5).

The gap is therefore routing/extraction, not missing adapters:
  - 15 Entrata-widget sites -> the Web Unlocker fix (499cf3f + b22ca6a,
    verified 17/20) very likely cracks these — commoncf.entrata.com is the
    same CF-walled surface.
  - RealPage/Repli360/Funnel/G5 widget sites -> detector must fire on the
    widget SCRIPT host, then the existing adapter must handle the
    widget-embed variant. Per-family verification needed.

Next step: re-run the full 5K with the shipped fixes (Entrata-widget cohort
should self-resolve), then the SPA residue is a per-widget-family routing
check against existing adapters — NOT new long-tail adapter work.
