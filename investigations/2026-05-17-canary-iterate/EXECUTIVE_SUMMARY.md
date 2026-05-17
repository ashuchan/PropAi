# Canary Iterate Experiment — Executive Summary (2026-05-17)

## Mission
Scrape all ~5,000 properties incl. failures; promote to Tier-1 unit-level
correct data; iterate fixes via jugnu-scrape-canary; do-or-die.

## Outcome: CODE SUCCESS, INFRA CEILING

### What was built & proven (9 iterations, branch fix/resolver-path-patterns-may13, NOT merged)
- iter1-3: Gemini wired, Entrata→SightMap fix B v2
- iter4: RentCafe **securecafe availableunits.aspx** unit-level probe (1060-pool target)
- iter5: Entrata-engrain→SightMap subpage + unquoted-iframe regex
- iter6: securecafe-portal Pass-1 detector marker (misclassified "other")
- iter7/8: securecafe/engrain base also from captured network log
- iter9: securecafe base via homepage curl_cffi refetch (root-cause fix)
All adapter logic STANDALONE-VALIDATED from a non-GCP IP:
  4/5 b2b4-relift sites → 76 Tier-1 unit-level rows.

### Canary results (5 batches, 2000 property-runs, proxy-less Cloud Run)
- Ledger: 1395 sites | 199 GOAL_MET (Tier-1 unit-level+sane) | 553 any-unit-level
- vs stock prod 2026-05-17 on the 1395 overlap: **+553 IMPROVED, 0 regressed**
  (canary fixes recovered unit-level data on 553 prod-FAILURE sites even
   without a proxy — Knock/G5/AppFolio-vanity/SightMap/JSON-LD/LLM paths)

### The systemic ceiling (DO-OR-DIE root cause)
- SECURECAFE conversions = **0 across all 2000 runs / 9 iterations**,
  despite standalone proof the code works.
- Cause: canary AND prod run **proxy-less** (PROXY_POOL_URLS empty,
  proxy-credentials-production secret EMPTY). Cloud Run egresses GCP
  datacenter IPs; **Cloudflare hard-blocks GCP IPs for securecafe.com**
  (events: fetch.captcha_detected provider=cloudflare, 403 BOT_BLOCKED).
  curl_cffi TLS-impersonation insufficient when source IP is DC-flagged.
- The ~1060 RentCafe-securecafe pool + broader CF-protected failures are
  INFRASTRUCTURALLY unscrapeable from proxy-less Cloud Run — not a code
  defect. (Same reason prod 2026-05-17 had 1900 bot_blocked + 1976 captcha.)

## Recommendation (admin/infra action — unblocks the shipped code)
1. Provision BrightData (or equiv) **residential proxy**; populate
   PROXY_POOL_URLS / proxy-credentials-production (currently empty in
   BOTH prod and canary).
2. Re-run the canary on the iter-9 image (canary-fixtest-9728710) with
   the proxy. Expected: large securecafe + bot_blocked recovery
   (standalone-proven conversion).
3. Then evaluate merge of fix/resolver-path-patterns-may13 to main
   (after the deferred rebase + review).

## Cost: well under the $150 ceiling (Cloud Build x10 + Cloud Run x5 + Gemini).
## Nothing merged to main; nothing touched prod DB/bucket; canary-isolated.
