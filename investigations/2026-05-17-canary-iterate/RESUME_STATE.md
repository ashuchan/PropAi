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
ROYCE SOLVED: rrac PARSER fix (normalize whitespace, split on row-
terminators) + dash-date fix (MM-DD-YYYY in _AVAIL/_AVAIL_LINE) +
rrac-timing. Commits 3abad9c (dash+timing), ea9247d (Cloud Run shard
wrapper ma_poc/scripts/runners/standalone_shard.py). 18-site cluster:
9/18 → **10/18** after dash-date.

**CLUSTER FINAL: 10/18 (cluster2, restored & committed f025b9e).**
cluster3 (pre-poll settle+22) REGRESSED to 9/4/4/1DEAD — reverted.
The 8 non-UNIT are ALL user-validated U (eyeball batch3), NOT
floorplan-only. **ROOT CAUSE (Chrome-MCP proven, durable):** the
module's EXACT rrac-detection JS works on live independence — returns
6 "VIEW DETAILS" anchors by 500ms, rrac=60, stays 6 for 12s. Logic +
timing are CORRECT in a foreground browser. The module miss is NOT
parser/timing — it is that the RealPage rrac embed never loads under
**concurrency-6 headless + no-proxy** (datacenter/headless-trust
starvation). MCP tab = foreground/single/residential-IP → loads <1s.
⇒ The independence-class fix is **residential proxy + stealth**, which
the GCP run provides (PROXY_POOL_URLS support added f025b9e). The
all-456 GCP-with-proxy run is the real test of this class, not a
parser tweak. independence rrac lives at /floor-plans (HYPHEN;
/floorplans no-hyphen → rrac=0); modal text "50/102 $2,215
05-23-2026, 40/102 $2,215 Available Now" parses fine already.

## #2 RE-BASELINE — LOCKED (durable: artifacts/analysis/rebaseline2_results.jsonl)
securecafe 1097/1400=78% (986 RentCafe-SC+53 Knock+28 SightMap) |
g5 94/124=76% NO-REGRESSION ✓ | appfolio 74/107=69% NO-REGRESSION ✓
| apts247 99/223=44% | sightmap 29/421=7% (224 NONE+33 SHAPE_REJECTED
— unrealized gain, standalone Phase-A target, NOT a regression).

## 456 GCP RUN — user OK'd "run 456 AND iterate in parallel"
- Image: canary-fixtest-f025b9e (REBUILDING bg bdieevfaa — has proxy
  support + cluster2 logic + shard wrapper). MUST use f025b9e (NOT
  ea9247d — that lacks proxy → datacenter IP bot-blocked → garbage).
- URLs: gs://jugnu-canary/property-list/all456_urls.txt (456)
- Wrapper: ma_poc/scripts/runners/standalone_shard.py — env URLS_GCS_URI,
  BUCKET_NAME=jugnu-canary, CONCURRENCY=6, RESULT_PREFIX=standalone456,
  RUN_DATE=2026-05-17. Cloud Run job ≈24 tasks for <30min.
- PROXY secret: proxy-credentials-production (key=latest) →
  PROXY_POOL_URLS. SA jugnu-worker-production@jugnu-494013.iam.
  gserviceaccount.com. region us-central1, cpu2/4Gi, timeout 3600,
  maxRetries 0. No VPC connector.
- Output: gs://jugnu-canary/runs/2026-05-17-standalone456/shard_*/results.jsonl
- Aggregate: artifacts/scripts/agg_standalone456.py 2026-05-17-standalone456
- EXEC: jugnu-standalone456-47cfn (region us-central1), watcher bg
  bly5und4a notifies on completion.

## Phase-D / lochraven — Chrome-MCP finding (durable)
lochraven = **RentVision** site (NOT royce-rrac). Floorplans at
/floorplans (no hyphen); per-FP detail /floorplans/<bed>/<slug> are
**floorplan-level only** ("Prices Starting At $1,240", "Available on
May 21, 2026", "Sign Waitlist" — NO unit#/bldg). Unit-level (user
eyeball=U) is behind "Check Availability" → /content/apply#k=57256
which **auto-loads a RealPage OneSite leasing wizard** in a
cross-origin iframe **id=rp-leasing-widget** (the Knock doorway chat
bubble is a SEPARATE element, not the unit source). USER-CONFIRMED U.

**PHASE-D MECHANISM FULLY CRACKED (Chrome-MCP screenshot):** wizard
steps 1.Floor Plans → 2.Apartment → 3.Lease Terms → 4.Quote. Step 1
renders floorplan cards each with an **"(N) Available"** button (live
available-unit count); clicking it advances to step 2 "Apartment"
which lists the individual units (unit#/rent/avail). #k= hash rotates
(26888→38186…) but the widget loads ALL floorplans regardless.
propertyId=2021993, Pusher ws, Knockbot GA. BUILD PLAN for
_phase_d_portal_hop: nav /content/apply (or follow Check-Availability)
→ wait for iframe#rp-leasing-widget → patchright
frame_locator('#rp-leasing-widget') / iterate page.frames() for the
RealPage onesite frame → click each "(N) Available" → read step-2
frame innerText → parse via _generic_text_rows. ~26-site class
(jaxon/marion/lochraven). Cross-origin frame + multi-step = real
work; do AFTER 456 agg quantifies class size.

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
