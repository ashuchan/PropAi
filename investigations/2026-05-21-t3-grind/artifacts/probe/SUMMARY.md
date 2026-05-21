# T3_No_extraction — deep-grind validation (n=239)

Date: 2026-05-21
Probe method: curl_cffi chrome131 → fetch homepage → discover floor-plan anchor → fetch target → score for unit/floor-plan signals → re-classify with strict block criteria.

## Headline (validated by deep grind, not sample extrapolation)

| | Properties | % |
|---|---:|---:|
| **Recoverable by homepage-anchor + existing adapter** | **120** | **50.2%** |
| Blocked by Cloudflare WAF at target (Entrata vanity domains) | 109 | 45.6% |
| No floor-plan anchor on homepage | 9 | 3.8% |
| No signal at target | 1 | 0.4% |

The earlier 73% sample-extrapolated estimate was **too optimistic** — half the Entrata cohort is actually CF-protected at the `/conventional/` target. The sample didn't probe deep enough to catch this.

## Strict re-classification — what each target returned

| Verdict | Count | % | Recoverable by path-discovery alone? |
|---|---:|---:|:---:|
| `blocked` (CF IUAM, status 403) | 109 | 45.6% | ✗ needs Playwright/HAR |
| `partial_unit_signal` (1+ rent OR 3+ unit/sqft) | 50 | 20.9% | ✓ |
| `floor_plan_only` (beds/plan titles, no unit rents) | 37 | 15.5% | ✓ (with per-plan expansion) |
| `unit_level_data` (3+ rents AND sqft/unit) | 33 | 13.8% | ✓ |
| `None` (no anchor found) | 9 | 3.8% | ✗ |
| `no_signal` | 1 | 0.4% | ✗ |

## PMS detected on target page (after following discovered anchor)

The recoverability per PMS is the critical view — it shows which adapters already work end-to-end, and which are gated by something other than path resolution:

| PMS at target | Total | Recoverable | % Recoverable |
|---|---:|---:|---:|
| **entrata** | **148** | **39** | **26.4%** |
| sightmap | 25 | 25 | 100% |
| realpage | 15 | 15 | 100% |
| rentcafe | 13 | 13 | 100% |
| funnel | 8 | 8 | 100% |
| engrain | 4 | 4 | 100% |
| apts247 | 3 | 3 | 100% |
| knock | 2 | 2 | 100% |
| g5 / yardi | 1 / 1 | 1 / 1 | 100% |

**Every non-Entrata PMS is 100% recoverable** once the router gets there. **Entrata is only 26% recoverable** because Cloudflare IUAM blocks curl_cffi on ~3 of every 4 vanity domains.

## The Entrata-CF cohort (109 properties, 45.6% of T3)

These all:
1. Have Entrata PMS markers on homepage (route detection works)
2. Have a discoverable `/conventional/` anchor on homepage (path discovery would work)
3. Return HTTP 403 with `<title>Just a moment...</title>` at the target URL (Cloudflare IUAM challenge — curl_cffi can't solve the JS challenge)

Sample sequential re-probe of 15 blocked Entrata with 3-second delays: **14/15 stayed blocked**. So this is real CF protection on those specific subdomains, not rate-limit aftermath. The 39 Entrata that returned data on the concurrent probe got past CF — either CF had cached a recent bypass for those subdomains or they aren't protected.

This cohort is **not solvable by the homepage-anchor fix alone**. It needs:
- Playwright (real browser to solve JS challenge), or
- HAR-replay (capture via browser once, replay statically), or
- An out-of-band mechanism (existing project memory: anti-bot research thread)

## Fix breakdown — what each engineering action actually buys

| Action | Properties recovered | % of T3 |
|---|---:|---:|
| Homepage-anchor discovery + label-variant support | 81 (all non-Entrata + 39 Entrata that pass CF) | 33.9% |
| In-page hash-anchor scroll/wait | ~7 (subset of above) | ~3% |
| Input-URL filtering (skip stale subpaths) | ~13 fetch_errors (likely overlap with above) | ~5% |
| **Playwright fallback for CF IUAM at target** | **109 (all blocked Entrata)** | **45.6%** |
| Manual inspection / per-property work | 10 (no anchor / login / no signal) | 4.2% |

## Revised engineering recipe

Your draft action items are all directionally correct, but the population numbers prove the **Playwright-fallback item is the biggest lever**, not the path-discovery item.

1. **Homepage-anchor path discovery** (you nailed the recipe — Floor Plans / Pricing / Availability label variants, in-page hash anchors). **Recovers 33.9%.**
2. **Cloudflare IUAM bypass for the Entrata `/conventional/` path** — this is what unlocks the remaining 45.6%. Options in priority order:
   - HAR-replay if a property's tokens are stable (cheapest, no per-run browser cost)
   - Playwright with stealth context (per-property browser cost, but always works on IUAM)
   - Note: residential proxy made DataDome WORSE per prior investigation (project memory `still_no_sig_proxy_disproven`), so probably the same for CF — don't reach for that.
3. **Input filtering** — skip `/contact`, `/amenities.html`, `/index.php`-style deep paths in the input list. **Recovers ~5%.**
4. **Detector signal strengthening** for the 53 non-Entrata PMS markers detectable on target pages but not homepage. Resolver-may13 already ships some of these.

## Artifacts

- `worklist.json` — 239 input properties
- `results.jsonl` — one line per property with all probe signals (raw)
- `results_reclass.jsonl` — same but with strict-block verdicts
- `/tmp/t3_classification.xlsx` — per-row spreadsheet for manual review
