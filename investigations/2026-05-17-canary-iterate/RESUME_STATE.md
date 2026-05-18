# RESUME STATE — 2026-05-17 canary (read this first after compaction)

Self-contained handoff. Full narrative = `FINDINGS.md`; file index =
`README.md`; eyeball verdicts = `artifacts/eyeball/batch3_verdicts.csv`.

## One-paragraph situation
Canary effort to maximize Tier-1 unit-level extraction on ~4,980 props.
Prod LIVE ≈ **30%** Tier-1; canary stack (proxy+WU+iter15/16) measured
≈ **60%**. iter-16 apts247, iter-17 spherexx-ZRS, iter-18 entrata-WP,
iter-19 sightmap-Step7b all committed + completeness-verified. Systemic
root cause: pipeline doesn't crawl+render per-floorplan DETAIL pages
(units live one level deeper, every platform). "~456 genuine-custom"
eyeball-validated (50-sample, user) = **~86% recoverable / ~14% true
floorplan-only ceiling**. Building a STANDALONE A+B+C module (own
patchright, zero pipeline coupling) to recover them.

## Standalone module — ma_poc/standalone/detail_unit_extractor.py
Self-contained; NOT wired to jugnu (no regression risk). Run:
`PYTHONPATH=<repo> python3 ma_poc/standalone/detail_unit_extractor.py <urls-file>`
Smoke set: `/tmp/known5.txt` (solano=sightmap, chatham=ZRS,
ironhorse=entrata-WP, jaxon=caf_v2-detail, royce=caf_v2-rrac-popup).
STATUS: **4/5 validated** (solano/chatham/ironhorse/jaxon UNIT).
royce (rrac popup) — mechanism CRACKED via Chrome-MCP: trigger =
`a` text "View Details" → async-populates `.rrac_apartment_details_
content` (Bldg#/Unit#/$/avail table). Was timing out (14×10s polls
> 120s cap) → FIXED: capped to 3 View Details + 6s poll.
ROYCE SOLVED (commit ~latest): rrac break was the PARSER not interaction — modal text has tabs+newlines INSIDE rows; fixed _generic_text_rows to normalize whitespace then split on row-terminators. 6-site smoke 5/6 UNIT (royce/ironhorse/jaxon/chatham/solano UNIT; lochraven Phase-D #k= still 0). 18-site rrac-cluster validation running bf47w5239.

[superseded] 8th smoke DONE: 4/5 UNIT (solano/chatham/ironhorse/jaxon), royce=
FLOORPLAN (timeout FIXED — no regression). royce rrac still 0 because
the module scans for "View Details" BEFORE the async rrac widget
renders. **EXACT NEXT FIX (do this first on resume):** in
`_phase_c_interact`, before counting View-Details, add a poll-wait
(~20×500ms) for `document.querySelector('[class*=rrac]')` AND for the
View-Details anchors to exist — mirror the working Chrome-MCP probe
(it only succeeded after that pre-poll). Then re-smoke /tmp/known5.txt
→ expect 5/5. Chrome-MCP-proven recipe is correct; only the pre-render
wait is missing.
Phases: A=sightmap(network capture), B=URL detail discover+render+
proven-parsers+generic-text, C=interaction(rrac popup + generic CTA).

## Background tasks in flight (will notify on completion)
- `b3t02rq1s` — 8th smoke (royce rrac fix). Read /tmp/known5h.out.
- `bukp3oraq` — #2 re-baseline (securecafe/sightmap/apts247/g5/
  appfolio on iter-19 image canary-fixtest-15f08b5). Separate track;
  measure + lock baseline when done (bank gains, sightmap +~200,
  verify g5/appfolio no-regression). Cohort run-dirs:
  gs://jugnu-canary/runs/2026-05-17-rebase-<cluster>/.

## NEXT ACTIONS (in order)
1. Read 8th smoke `/tmp/known5h.out`. If royce=UNIT → 5/5.
2. **Build Phase D — OneSite/Knock #k=-portal-hop** (NOT yet done):
   caf_v2 "Check Availability"/`/online-leasing#k=`/`/content/apply#k=`
   hops to `property.onesite.realpage.com`/`leasing.realpage.com` or
   `doorway.knck.io`. REUSE existing `OneSiteAdapter`/`KnockAdapter`
   logic. Sites: lochraven, lewis (eyeball #31,#36).
3. Run standalone on ALL ~456 (urls: derive from
   artifacts/analysis/probe456_res.json or gc_vendor_res.json keys).
   ~1.5–2.5 hr local. Aggregate UNIT/FLOORPLAN/NONE vs eyeball ~86%.
4. Measure + lock #2 re-baseline (bukp3oraq) — separate.
5. Pending: securecafe source-URL provenance gap; #1 generalizable
   detail-crawl (GATED on #2 clean + user OK — likely superseded by
   the standalone module being the real "all-456 check").

## Eyeball 50-sample — DONE, user-validated (durable)
42U / 8F of 50 = 84% → all-456 ~86% recoverable. Patterns:
Engrain/SightMap (existing adapter, JS-injected) | caf_v2 family
(rrac-popup / detail-page / #k=-OneSite-Knock-portal) | per-unit
`/unit/<slug>` | `availability.html` in-page expand | apts247/
spherexx-ZRS/entrata-WP (proven parsers) | securecafe hop.
USER POLICY: record floorplan-level when no unit-level, but it counts
as F (not a unit-level win). Never conclude "no units" from shallow/
static/regex — check the per-floorplan DETAIL page; eyeball if unsure
(my solo "no-units" calls were 0/7).

## Key durable files (all git-committed)
- FINDINGS.md (narrative), README.md (index), RESUME_STATE.md (this)
- artifacts/eyeball/batch3_verdicts.csv (50 user verdicts)
- artifacts/analysis/ (probe456_res.json, gc_vendor_res.json,
  domllm_ledger.json, cohort_results.jsonl, known5*.out smoke history)
- artifacts/scripts/ (reproducibility)
- ma_poc/standalone/detail_unit_extractor.py (the module)
NOTHING critical lives only in /tmp (re-synced).
