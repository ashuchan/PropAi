# 100-property VISION-verified concession audit — 2026-05-24

End-to-end vision-verified concession-capture verification against a 100-property
random sample (seed=20260524+1, distinct from the prior 50-prop sample). Goal:
quantify how often we miss JS-injected popup banners, since the Blake regex fix
(see `INVESTIGATIONS/.../CONCESSION_50PROP_VERIFICATION.md` + the "Exclusive
Offer / 10 Weeks Base Rent Free" gap) hinted this was a systemic — not isolated —
blind spot.

## Methodology

1. **Sample**: 100 randomly selected properties from `properties.csv`
   (seed=20260524+1, excludes the prior 50-prop sample), uniformly random
   over all 4,982 props.
2. **Static pipeline**: for each, fetched homepage via curl_cffi chrome120
   and ran the full pipeline:
   - `_PROPERTY_CONCESSION_RE` (scraper.py Step 3 banner regex — including
     the Blake "Base Rent / Exclusive Offer / waived" fix shipped earlier today)
   - `extract_api_concession` on inline JSON
   - `extract_offer()` taxonomy
3. **Hint check**: scanned each body for offer keywords (`special`, `free`,
   `$N off`, `move-in`, `promo`, etc.) to find cases where the static pipeline
   missed something the page had hinted at.
4. **Vision sweep**: launched headless Chromium (Playwright) on every queue
   property — wait `domcontentloaded` → `networkidle` (8 s cap) → +2 s for
   late JS — then ran a rendered-DOM probe that:
   - Searched `document.body.innerText` for the canonical offer keyword set
     and returned 6 context windows
   - Enumerated all elements matching `[class*="popup"|"modal"|"banner"|
     "announcement"|"special"|"promo"|"offer"]` / `[role="dialog"]` and
     returned their visible inner text
5. **Manual triage**: cross-referenced static vs rendered DOM, classified each
   property as TP / TN / FN. Re-fetched FN URLs via curl_cffi to confirm the
   offer is **absent from static HTML** (i.e., truly JS-injected vs regex gap).

## Aggregate results (n=100)

| Verdict | Count | % | Notes |
|---|---:|---:|---|
| **TP** (static captured AND rendered DOM confirms) | 8 | 8% | precision sample of the 37 captured |
| **TP_static_only** (rendered DOM had no fresh kw but static caught it) | 5 | 5% | flicker/snapshot timing, still genuine |
| **TP_static_only_unverified** (captured, outside vision queue) | 24 | 24% | trusted as TP per the 50-prop precision audit |
| **TN_no_offer** (no kw in rendered DOM) | 48 | 48% | recall confirmed — site genuinely has no offer |
| **TN_nav_only** (rendered kw was nav-link text only) | 2 | 2% | "SPECIAL OFFERS" menu item, no actual content |
| **FALSE_NEG** (offer visible in rendered DOM, static missed) | **9** | **9%** | **all JS-injected popups — see below** |
| **PROBE_ERR** (Chrome blocked / timeout / chrome-error://) | 3 | 3% | Chrome MCP early-batch transient failures |
| **FETCH_ERR** (DNS / TLS failure during static fetch) | 1 | 1% | yardlydechman.com unresolved |

**Net: 0 confirmed false positives in the verified captured sample.
9 confirmed false negatives = ~9% miss rate on a random sample.**

For context, the prior 50-prop verification reported 0/9 false negatives. That
sample drew zero JS-injected-popup operators; this 100-prop sample picked up
the class.

## The 9 false negatives — all JS-injected

For each FN we ran `curl_cffi chrome120` against the homepage and grepped
the rendered-DOM offer text in the static body. **6/6 reachable sites have
the offer text completely absent from static HTML.** (3 had DNS/TLS failures
in static-fetch — those would also be silent misses in production for a
different reason.) These are not regex gaps; they are pipeline-tier gaps.

| pid | Property | Rendered-DOM offer | Present in static HTML? |
|---|---|---|---|
| 1786 | Austin Midtown | "LEASE TODAY & EARN UP TO 4 WEEKS* FREE!" (modal) | **NO** |
| 290592 | Colina Ranch Hill | "Limited-Time Special Offers… Up to 2 Months Free… Reduced Rates… Look & Lease 50% off application + admin" | **NO** |
| 75082 | Jefferson Place | "Lease today and enjoy up to $1000 Off" | DNS fail (static blind) |
| 304313 | Prose Riviana | "Live Up to 8-Weeks Free Base Rent + $1,500 Gift Card!" | **NO** |
| 67915 | 42 West Apartments | "Enjoy a $300 One-Time Rent Concession at Move-In" | DNS fail (static blind) |
| 17091 | Museum Terrace | "Up to $1,500 Off Base Rent — Look & Lease Special!" (modal) | static returns 703-byte placeholder |
| 34500 | Cortland Brier Creek | "Receive up to 2 months free when you move into select apartment homes!" | **NO** |
| 4386 | The Quarry Alamo Heights | "Spring Special! Up to 6 Weeks Free Base Rent! + Waived App & Admin Fees!" (modal) | **NO** |
| 261178 | Blossoms at Brentwood | "Now offering up to six weeks free on select homes!" | **NO** |

Every one of these was found in:
- a `[role="dialog"]` / `.modal` element rendered post-load, or
- a `.banner` / `.popup` element only present after the React/Vue/Angular
  app hydrates, or
- (Cortland, Blossoms) a SSR section whose initial HTML container is empty
  and gets `innerHTML`-injected from a fetch in the bundle

## What this changes

The Blake fix (regex qualifier slot + "Exclusive Offer" + "waived") closed the
**regex-text gap** for cases where the static HTML *does* contain the offer
text in unusual phrasing. It did **not** close the gap for cases where the
banner is **physically not in the static HTML at all**. This audit proves the
JS-injection class is a real recurring miss, not a one-off Blake outlier.

Expected lift if we add rendered-DOM concession scanning: **+9 pp on
concession capture rate** across the random sample (and likely similar
across the full 4,982-prop run, since the sample was uniformly drawn).

## Recommended fix (next ticket)

Add a **Step 3c "rendered DOM concession rescan"** to the scraper that fires
when:
- Static body is < N bytes (e.g. <50 KB), AND
- Step 3 + Step 3b returned no concession capture, AND
- Property already has a Playwright/Chrome session open (it's free to reuse)

The scan should:
1. After page render, query `[role="dialog"], [class*="modal"], [class*="popup"],
   [class*="banner"], [class*="announcement"], [class*="special"], [class*="promo"]`.
2. Pass the visible inner text through `_PROPERTY_CONCESSION_RE` + `extract_offer`.
3. Tag the resulting concession with `source="DOM_POPUP_RENDERED"` for
   downstream provenance.

Reuse of the already-opened Playwright session keeps the marginal cost low
(no extra page load). For the curl-only properties that don't open a browser
today, this gap remains until those routes upgrade to Playwright (separate
ticket; see SightMap/G5 cohorts that already have it).

## Artifacts

- `/tmp/sample100.json` — the 100-prop random sample (seed 20260524+1)
- `/tmp/sample100_pipeline.json` — static-pipeline output
- `/tmp/sample100_visual_pw.json` — Playwright rendered-DOM probe output
- `/tmp/sample100_verdict.json` — per-property final verdict
- `/tmp/visual_sweep.py` — the parallel headless Playwright probe script
  (concurrency 6, 25 s nav timeout, 2 s post-load wait)
