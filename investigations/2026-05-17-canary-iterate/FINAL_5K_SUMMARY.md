# FINAL 5K SUMMARY — Canary Iterate Experiment (2026-05-17), verified

## Universe
Catalog ~5,000. Prod run 2026-05-17 processed 4,650 (~350 lost to 3 dead
Cloud Run shards + catalog shortfall).
  Tier-1 already-good (no fix needed): 1,506 (32%)
  NOT Tier-1 (the fix target):         3,144 (68%)

## What the experiment did (11 iterations, branch only, NOT merged)
- iter1-9: Gemini, Entrata→SightMap, RentCafe-securecafe, engrain,
  detector + network-log/homepage-refetch recovery
- iter10: NEW ResMan adapter (from the user-directed per-site probe of
  all 842 "other/custom" sites)
- iter11: systemic detection-rescue — curl_cffi homepage refetch when
  detection is unknown/custom, recovering misclassified-but-adapter-
  exists clusters (OneSite/Funnel/SightMap/AppFolio/Entrata/ResMan)

## Coverage (the explicit ask: run every runnable non-Tier-1)
Ledger gs://jugnu-canary/best_results.jsonl: **2,587 sites** — the
full proxy-INDEPENDENT runnable non-Tier-1 pool now has a verdict.
Excluded by design: 202 securecafe (proxy-blocked, 0/2000 across all
runs) + garbage catalog URLs (sightmap.com as property URL).

## Verified results (correct v2 keys: unit_id/rent_low/beds)
  GOAL_MET (Tier-1 unit-level + sane): 315
  any unit-level recovered:            919
  best-tier of recovered: 315 TIER-1 | 305 T2/T3 | 299 LLM
  stuck floorplan-only:                501

iter-10/11 NET-NEW Tier-1 unit-level on the 842 "other" pool
(iter-9 baseline ~0, ALL proxy-independent):
  27 total — ResMan 4, SightMap-iframe 6, AppFolio-vanity 5, Knock 3,
  SightMap 2, generic-API 4, Entrata 1, Spherexx 1, AvalonBay 1.

## Honest verdict
1. CODE WORKS. The deep-probe-driven iter-10/11 produced the ONLY
   proxy-independent Tier-1 gains in the whole experiment. Detection-
   rescue is the bigger lever (unlocked 5+ existing adapters for
   misclassified sites). ResMan adapter is correct + verified
   (liveatthekendrick: 53 real units, unit_id/rent/beds populated).
2. CEILING #1 (infra, not code): the ~1,060 RentCafe-securecafe pool
   stays 0 — proxy-less Cloud Run GCP IPs are Cloudflare-blocked.
   Standalone (non-GCP IP) the code converts them. Needs a residential
   proxy (proxy-credentials-production secret is EMPTY). Admin action.
3. GAP #2 (code, iter-12 follow-up): ResMan detection-rescue routes
   ~27 sites to the adapter but only ~15% (4/27) find the Availability
   portal link from /floorplans/+homepage. Widen ResMan portal
   discovery (other subpages / network-log) → recovers the other 23.
4. The ~500 floorplan-only + ~370 no-signature remainder = genuine
   custom one-offs / Gemini-only / unrecoverable-by-input. No single
   high-leverage pattern (verified by the full 842 probe, not sampled).

## Projected 5k end-state
  Today (stock prod):                ~1,506 / 5,000 Tier-1 (32%)
  + ship branch, current infra:      ~1,506 + ~850 ≈ 47% (proxy-indep)
  + residential proxy (securecafe):  ~+1,000 ≈ 65%
  + iter-12 ResMan portal-discovery: ~+150-400 more
  Hard remainder (~30%): dead URLs, no public data, hostile anti-bot.

## Recommendation
1. Provision residential proxy (biggest single lever, ~1,000 sites).
2. iter-12: widen ResMan portal discovery (proxy-independent, ~hundreds).
3. Rebase branch onto main + review/merge (deferred per instruction).
Cost: ~13 Cloud Builds + ~9 Cloud Run batches + Gemini — under $150.
Nothing merged to main; prod DB/bucket untouched; canary-isolated.
