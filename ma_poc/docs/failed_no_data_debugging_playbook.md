# FAILED_NO_DATA Property Debugging Playbook

**Audience:** Claude Code sessions debugging extraction failures after a cloud run.  
**Source authority:** 2026-05-11/12 investigation session + 2026-05-12 wide canary campaign (v1–v7). Every technique below was used live.  
**Working directory for all commands:** `ma_poc/` (the Jugnu app root, not `PropAi/`).

---

## Overview: the investigation loop

```
Cloud run → download artifacts → read per-property report → identify root cause →
implement fix → write targeted test → run local canary with live fetches → confirm fix
```

There are exactly **two distinct questions** to answer for any FAILED_NO_DATA:

1. **Was the data reachable from the URL we fetched?** (L1 fetch + network capture problem)
2. **Was the data parsed/recognised from the body we got?** (L3 extraction problem)

Always answer (1) first. If we never fetched the right page, fixing the extractor is pointless.

---

## Phase 1 — Pull cloud-run artifacts

### 1.1 Download the run

```bash
# Mirror one run from GCS to local
python scripts/diagnostics/analyze_cloud_run.py \
    --run-date 2026-05-11 \
    --local-mirror C:/tmp/run-2026-05-11 \
    --download-only
```

The mirror lands at `C:/tmp/run-YYYY-MM-DD/` with one subdirectory per shard:
```
shard_0/           report.json, events.jsonl, llm_report.json
shard_0_raw/       properties.json   ← only shard_0_raw has this
shard_1/
...
```

### 1.2 Build failed-properties list from events

The `events.jsonl` per shard contains `output.property_emitted` events with `verdict` and `units`. Aggregate across all shards:

```python
import json, os
from pathlib import Path

base = Path(r'C:\tmp\run-2026-05-11')
props = {}
for shard_dir in os.listdir(base):
    if shard_dir.endswith('_raw'): continue
    ep = base / shard_dir / 'events.jsonl'
    if not ep.exists(): continue
    with open(ep, 'r', encoding='utf-8') as f:
        for line in f:
            try: ev = json.loads(line)
            except: continue
            pid = ev.get('property_id')
            kind = ev.get('kind', '')
            if not pid: continue
            rec = props.setdefault(pid, {'pid': pid, 'shard': shard_dir})
            if kind == 'output.property_emitted':
                rec['verdict'] = ev.get('verdict')
                rec['unit_count'] = ev.get('units', 0)
            elif kind == 'fetch.started' and 'url' not in rec:
                rec['url'] = ev.get('url')
            elif kind == 'extract.tier_won':
                rec['tier'] = ev.get('tier_used')

failed = [r for r in props.values() if r.get('verdict') == 'FAILED_NO_DATA']
succeeded = [r for r in props.values() if r.get('verdict') == 'SUCCESS']
print(f'failed: {len(failed)}, succeeded: {len(succeeded)}')

# Save for reuse
json.dump(failed, open(r'C:\tmp\run-2026-05-11_failed_props.json', 'w'), indent=2)
json.dump(succeeded, open(r'C:\tmp\run-2026-05-11_succeeded_props.json', 'w'), indent=2)
```

---

## Phase 2 — Read the per-property report

The most information-dense artifact is the per-property markdown report. For cloud runs, these are in each shard's `events.jsonl` (encoded as events). For canary runs (local), they're written to:

```
data/canary/local_runs/{run-dir}/v2/runs/2026-05-11/property_reports/{property_id}.md
```

### What to read in the report

**Fetch Diagnostic section:**
| Field | What it tells you |
|---|---|
| `Outcome` | OK / BOT_BLOCKED / DEAD_URL / TRANSIENT — if not OK, extraction never ran |
| `Body Bytes` | If < 20KB on a RENDER fetch, Playwright got a shell page — data not loaded yet |
| `Network Log Entries` | How many XHRs fired. 0 = no API data available |
| `CAPTCHA Detected` | True = blocked; residential proxy needed |
| `Error Signature` | `CF_CHALLENGE` = Cloudflare; `ERR_NAME_NOT_RESOLVED` = dead domain |

**PMS Detection Signals section:**
| Field | What it tells you |
|---|---|
| `Meta generator` | Recognises CMS platform (Jonah Digital, WordPress, etc.) |
| `Fingerprints matched` | PMS auto-detection result. `[]` = unknown PMS → generic cascade |
| `Script hosts` | Third-party scripts embedded — often expose PMS (nestiolistings.com, sightmap.com, etc.) |
| `Iframe hosts` | Leasing portals often embedded in iframes |

**Extraction Attempts section (tier ladder):**

Read top-to-bottom. Find the FIRST tier that ran and returned empty. That's the failure point.

| Tier key | What it does | Failure reason to watch for |
|---|---|---|
| `generic:profile_replay` | Deterministic replay of saved mappings | "no saved mappings" = first-time property |
| `generic:api_narrow` | JSON API bodies with ≥2 unit-signal keys | "no items matched unit-signal heuristic" = API captured but body not unit-shaped |
| `generic:embedded_json` | `<script type="application/json">` blobs | "N SSR blob(s) had no unit signals" = JSON config detected but no unit keys |
| `generic:embedded_portal_detected` | Portal URLs in embedded JSON (SightMap, etc.) | Should appear if Jonah/portal config present |
| `generic:floorplan_subpages_detected` | Floor-plan sub-page links from index page | Should appear if on a floor-plan index |
| `generic:dom_scan` | CSS selector cascade on rendered DOM | "no DOM container matched" = page rendered but selectors don't match |
| `generic:realpage_cws` | RealPage CWS credential probe — extracts `propertyId`+`apiKey` from RPFP_config and calls the units API directly | Only fires when `rpfp_config` in HTML. "RPFP_config found but API returned no units" = property has no available units |
| `generic:securecafe_portal_detected` | SecureCafe URL synthesised from `/onlineleasing/{slug}` href in rendered HTML | Adds securecafe URL to portal hints at score 10000 for link-hop |
| `generic:llm_dom_targeted` | LLM on tightest rent-containing section | Latency visible; if ran but empty, LLM couldn't find data |
| `generic:llm` | Monolithic LLM on full page | "llm_monolithic budget exhausted" = earlier tiers used the budget |

**Phase 5 LLM-Assisted API Analysis (if present):**  
Shows which API URLs were analyzed and classified as NOISE. If the data-bearing API is in here and classified as NOISE, the unit-signal keys need updating.

---

## Phase 3 — Root-cause taxonomy

There are **7 failure classes** for FAILED_NO_DATA. Diagnose in this order:

### Class 1: Cloudflare/Anti-bot blocked (L1 failure)
**Signal:** `Error Signature: CF_CHALLENGE` OR `Outcome: BOT_BLOCKED` OR body = 0 bytes.  
**Fix:** needs residential proxy pool (cloud production uses this; local canary cannot bypass).  
**Examples:** `274198` (legacyridgestgeorge.com) — always fails locally.  
**Action:** mark as INFRA_BLOCKED in canary; verify passes in cloud via contract test.

### Class 2: Dead URL / domain gone (L1 failure)
**Signal:** `Outcome: DEAD_URL` with `HTTP_404` or `ERR_NAME_NOT_RESOLVED`.  
**Fix:** mark property as DEAD_URL_CANDIDATE in the manifest. Check the redirect chain — sometimes 302→404 is mis-classified.  
**Examples:** `40640` (dawnhomes.com) — 302 redirect to dead page.

### Class 3: JavaScript-deferred content not loaded (L1 timing)
**Signal:** `Body Bytes` small (< 100KB on a large site) OR `Rent signals: 0` with dom_scan empty despite page being known-good.  
**Root cause:** IntersectionObserver or lazy-load gates content behind scroll event; our 2s settle doesn't trigger it.  
**Fix:** `fetch.scroll_trigger` in `ma_poc/fetch/fetcher.py` fires when `body > 50KB AND no $ signs` — scrolls to bottom, waits 1.5s, re-reads DOM + network_log.  
**Log line to look for:** `fetch.scroll_trigger url=... body_before=... body_after=... grew=True rent_appeared=True`  
**Examples:** `11611` (skylineatkessler.com / Jonah Digital) — scroll triggers _fp-renderable XHR.

### Class 4: Link-hop not reaching the right page (L2 navigation)
**Signal:** `Internal links discovered: 0` AND `Links Explored: 0` on a SPA with no in-page unit data.  
**Root cause:** homepage is a marketing shell; unit data is on `/floorplans/`, `/conventional/`, etc.  
**Diagnostic query:** fetch the homepage manually, look for nav links and their hrefs.  
**Fix options:**
- PMS priors (`_PMS_SUB_PATH_PRIORS` in `scraper.py`) — try standard sub-paths for the detected PMS
- Universal priors (`_UNIVERSAL_SUB_PATH_PRIORS`) — fires for unknown PMS
- Anchor keyword scoring (`_LINK_ANCHOR_KEYWORDS`, `_LINK_PATH_KEYWORDS`) — discovers CTAs like "Find Your Home", "Pick Your Home", `/conventional/`
- `<form action>` parsing — some Entrata sites put the floor-plan URL in a form's action, not an `<a href>`
- `prospectportal.com` in `_LINK_HOST_KEYWORDS` — follows Entrata custom-domain cross-site links

### Class 5: Unit data behind embedded leasing portal (L3 extraction)
**Signal:** `generic:embedded_json` finds SSR blob (e.g., Jonah config) but "no unit signals". `generic:embedded_portal_detected` should have fired.  
**Root cause:** the page contains config JSON pointing at SightMap/RealPage OLL/etc., but we didn't follow it.  
**Fix:** `detect_embedded_portal_urls()` in `_html_extract.py` walks the blob, surfaces portal URLs → `_embedded_portal_hints` on the adapter result → link-hop queues them.  
**Examples:** Jonah Digital sites (Skyline at Kessler) embed `{"embed_url": "https://sightmap.com/embed/..."}`.

### Class 6: Unit data on floor-plan sub-pages (L2 navigation depth)
**Signal:** `generic:dom_scan ran_units=5` on a floor-plan INDEX page, but total property has 20+ units.  
**Root cause:** the index page shows available highlights; full unit list per plan lives at `/floorplans/{plan-slug}/`.  
**Fix:** HTML sub-page link discovery in `_try_link_hop` — when dom_scan finds units on an index page, run `_rank_internal_links` on the rendered HTML, queue same-prefix links scoring ≥ 88, accumulate units.  
**Filtering rules:** skip `/floorplans/unit-{hex32+}/` (detail pages), skip portal-domain links that aren't floor-plan sub-paths.

### Class 7: API captured but gate rejects it (L3 extraction)
**Signal:** `generic:api_narrow: ran_empty "no items matched unit-signal heuristic"` AND the API IS in the network log (confirmed via Phase 5 LLM or per-property log).  
**Root cause:** `has_unit_signals()` requires ≥2 keys from `_UNIT_SIGNAL_KEYS` per item. API uses non-standard key names.  
**Diagnostic:** look at the API response body in Phase 5 LLM-Assisted section. Check which keys the items have against `_UNIT_SIGNAL_KEYS` in `_merge_fns.py`.  
**Fix:** add the missing key names to `_UNIT_SIGNAL_KEYS`. Examples: `"available_on"` (SightMap REST API), `"price"` (generic e-commerce style), `"area"` (SightMap geometry).  
**Also check body cap:** if API body > 256KB, JSON is truncated → `json.loads` fails → treated as string → gate rejects. Fix: raise `_body_cap` in fetcher.py for `application/json` content-type (currently 512KB).

### Class 8: Parked / expired domain (L1 — 200 with junk content)
**Signal:** `Outcome: DEAD_URL` with `PARKED_DOMAIN` error signature. Property has zero failures in prior runs but now always returns 0 units.  
**Root cause:** domain registrar serves HTTP 200 with "This domain is for sale" / "domain is available" content. Standard HTTP-status classification misses it (200 looks like success).  
**Detected by:** `response_classifier.py:_is_parked_domain()` — checks first 4KB of body against known registrar phrases (GoDaddy, Sedo, Namecheap, "searchhound", etc.) and emits `DEAD_URL / PARKED_DOMAIN` instead of `OK`.  
**Action:** verdict becomes `DEAD_URL` which is excluded from the success-rate denominator. Route to re-discovery queue.  
**Examples (2026-05-12):** pid=11961 (searchhounds.com redirect), pid=12557, pid=18941 — all domains up for sale.

### Class 9: JavaScript widget with embedded API credentials (L1 auth)
**Signal:** All tiers empty despite the property having a known RealPage integration. `generic:embedded_json` shows SSR blobs but no unit signals. Fetch body is large (100–150KB) but contains only a marketing shell with `<div id="rpfloorplans"></div>`.  
**Root cause:** RealPage LeaseStar CWS sites serve a JS widget that reads `RPFP_config` from the page HTML and calls `api.ws.realpage.com/v2/property/{id}/units` with `x-ws-authkey: {apiKey}`. Without the header the API returns 401. The HTML contains both the `propertyId` and `apiKey` — they are **not secret** (they're there to authenticate the browser widget).  
**Detected by:** `generic:realpage_cws` sub-tier in `generic.py` — searches for `RPFP_config` in HTML, extracts credentials with regex, fires the API directly.  
**Key patterns to recognise:**
```html
<script>var propertyId = '7582398'; var propertyKey = '90775823';</script>
<!-- and in RPFP_config: -->
apiKey: 'c2f6b6be-c513-44bd-96e6-1f69efb66cbc',
```
**Response envelope:** `data["response"]["units"]` → list with `unitNumber`, `numberOfBeds`, `numberOfBaths`, `squareFeet`, `rent`, `totalRent`, `internalAvailableDate`.  
**Examples (2026-05-12):** pid=11159 (hunterscourtapts.com) — 5 units extracted deterministically, zero LLM cost.

### Class 10: React SPA with lazy-loaded navigation (L1 render timing)
**Signal:** Entry page body is small (< 40KB) AND anchor link count < 20. The LLM nav_hint points to a leasing portal URL (e.g., securecafe.com, onlineleasing path) but the hop either HARD_FAILs or gets a matching JS shell.  
**Root cause:** React Router uses code-splitting. The navigation component containing the leasing portal link lives in a separate JS chunk that loads AFTER `networkidle` / `domcontentloaded` fires. Playwright snapshots the HTML before React Router finishes mounting the route component.  
**Two sub-patterns:**

| Sub-pattern | Symptom | Fix |
|---|---|---|
| **Shape A** — direct securecafe href in rendered HTML | Appears in some runs (75KB body), absent in others (19KB) | Anchor stability gate makes it deterministic |
| **Shape B** — local `/onlineleasing/{slug}` href | Always present, but hop to that path returns identical 19KB SPA shell | `detect_securecafe_portal_url()` synthesises the securecafe URL from href slug + domain |

**Anchor stability gate** (`fetch/fetcher.py`): when body < 40KB and `<a href>` count < 20, polls anchor count every 1.5s (up to 4 rounds = 6s max) until stable. Once stable, re-reads `page.content()`. This gives React Router time to load the navigation chunk.  
**Log line:** `fetch.anchor_stable url=... links=N stable=True body=XXXXX`  

**SecureCafe URL synthesis** (`pms/adapters/_html_extract.py:detect_securecafe_portal_url()`): when HTML contains `href="/onlineleasing/{path-slug}"`, constructs `https://{domain-slug}.securecafe.com/onlineleasing/{path-slug}/floorplans.aspx`. The domain slug is the property hostname with TLD stripped; the path slug comes directly from the href (SecureCafe always matches them).  
**Examples (2026-05-12):** pid=272521 (villageatthegateway.com → 8 units), pid=25964 (carrolltoncrossingapt.com → 1 unit).

### Class 11: Securecafe slug derived from wrong domain after cross-host redirect (L2 navigation)

**Signal:** The synthesised securecafe URL uses the ORIGINAL entry domain slug (e.g., `affinity56.securecafe.com`) even though the entry page redirected to a different host (e.g., `elevation56.com`). The hop fetches a generic rentcafe.com page or a wrong-tenant securecafe page — body is large (400KB+) but `rent_signal_count=0` on the target and `final_url` shows the generic domain.

**Root cause:** `detect_securecafe_portal_url()` derives the securecafe subdomain slug from `ctx.base_url` (the original entry URL), not from `fetch_result.final_url` (the post-redirect URL). When a property domain has been aliased or rebranded (e.g., affinity56.com → elevation56.com), the slug is wrong.

**Fixed in:** `pms/adapters/generic.py` — when calling `detect_securecafe_portal_url()`, the code now reads `ctx.fetch_result.final_url` and compares its hostname against `ctx.base_url`. If the redirect crossed to a **different host**, `final_url` is used as the slug base; same-host redirects (path changes only) keep `ctx.base_url`.

**Diagnosis:** Look at `fetch.completed` for the entry page — if `final_url` hostname differs from the original URL hostname, and then securecafe hops return a generic or wrong-tenant page, this is the cause. Check the slug in `extract.tier_attempted: generic:securecafe_portal_detected` — if it uses the OLD hostname, the fix applies.

**Example (2026-05-12 canary):** pid=256537 (affinity56.com → elevation56.com) — securecafe hop returned rentcafe.com homepage. After fix: 6 units extracted from `elevation56.securecafe.com`.

### Class 12: Infra/media API URL saved as `profile:winning_page_url` (Profile bug)

**Signal:** First hop candidate is `profile:winning_page_url` but it immediately hard-fails (HTTP 400/401/0 bytes). The URL is an AWS Lambda endpoint (`execute-api.*.amazonaws.com`), a media/CDN API (`matterport.com`, `omappapi.com`, `theconversioncloud.com`, `nestiolistings.com/api/`), or another third-party backend — not a property leasing page.

**Root cause:** A previous run captured one of these infra API responses as the "winning" page because it returned unit-shaped JSON (e.g., neighbourhood data, config blobs). `profile_updater` saved it without the infra-URL guard applying to `winning_page_url`. On the next run, hop #1 hard-fails immediately, consuming the budget slot before the actual floor-plans page is tried.

**Fixed in:** 
- `services/profile_updater.py` — `_INFRA_API_DOMAINS` extended with `execute-api.`, `matterport.com`, `omappapi.com`, `theconversioncloud.com`, `nestiolistings.com/api/`, `s3.amazonaws.com`, `cloudfront.net`
- `pms/scraper.py` — `_try_link_hop` now checks `_is_infra_api_url(wpu)` before injecting `winning_page_url` into the hop queue; infra URLs are silently skipped at queue-build time
- `services/profile_updater.py` — invalidation path: when the `profile:winning_page_url` hop returns non-OK, the URL is cleared from the profile and maturity is reset to COLD

**Domains to recognise:** `nestiolistings.com`, `api.omappapi.com`, `api.theconversioncloud.com`, `*.execute-api.*.amazonaws.com`, `my.matterport.com`, `*.cloudfront.net`, `*.supabase.co`, `browse.search.hereapi.com`.

**Examples (2026-05-12):** pid=29995 (canoanvillageapts.com): `nestiolistings.com/api/v2/neighborhoods` was WPU — blocked, property succeeded. pid=277774 (byredwood.com): `api.omappapi.com` was WPU — blocked, correct rentatredwood.com URL found instead.

### Class 13: RC3 monolithic LLM deferred to hop, then re-deferred on hop page (cascading deferral)

**Signal:** On the entry page, `generic:llm` shows `skipped, reason="rc3_defer_monolithic_to_hop"`. The hop reaches a valid sub-page (e.g., `/floorplans`, 699KB, 15+ rent signals). But on THAT hop page, `generic:llm` ALSO shows `skipped, reason="rc3_defer_monolithic_to_hop"` — deferring to the securecafe portal, which is already BOT_BLOCKED. The monolithic LLM never runs on either page.

**Root cause:** `ActionDecider.decide()` had no `hop_depth == 0` guard. RC3 Rule 2 fires identically on the entry page and every hop page. `DecisionContext.hop_depth` was populated but never read inside `decide()`.

**Fixed in:** `pms/signal_engine/decider.py` — Rule 2 now requires `ctx.hop_depth == 0`. The monolithic LLM is only deferred from the **entry page**; on hop pages it runs immediately if the budget allows.

**Diagnosis:** If `generic:llm` shows `rc3_defer_monolithic_to_hop` on a hop page (not the entry page), this was the bug. Check `hop_index` in `extract.link_hop_fetched` — if it's ≥ 1, the LLM should not be deferred.

### Class 14: Silent homepage redirect consuming hop budget (L2 navigation)

**Signal:** A hop path (e.g., `/availability`) returns HTTP 200 but `final_url` equals the homepage URL. The scraper runs the full extraction cascade (including expensive LLM calls) on what is functionally the same homepage body, consuming both a hop slot and LLM budget before the correct sub-page (e.g., `/floorplans/`) is reached.

**Root cause:** The scraper treated any HTTP 200 as a valid hop result. Some sites redirect `/availability` → `/` (homepage) with a 200 status, not a 301/302. The scraper had no mechanism to detect same-host+same-path redirects.

**Fixed in:** `pms/scraper.py:_try_link_hop` — after each successful hop fetch, compares `fetch_result.final_url` host+path against `entry_url`. If they match (same host, root path or identical path), the hop is skipped with `redirect_to_homepage` and the loop continues to the next candidate without consuming LLM budget.

**Example (2026-05-12):** pid=217930 (reserveatcityplace.com) — `/availability` → homepage (200 OK). Hop budget consumed on homepage re-extraction before `/floorplans/` (which had real data) could be reached.

### Class 15: CSV entry URL is dead or wrong domain — data unreachable (Data quality)

**Signal:** Entry page returns HTTP 404 with a small body (< 10KB), `outcome=DEAD_URL`. The actual property data is on a completely different domain that was never discovered. No hops attempted (property killed on fetch failure).

**Root cause:** The URL in `properties.csv` is stale — the property management company has rebranded, moved domains, or the specific sub-page path no longer exists. The scraper cannot recover because no redirect exists to the new domain.

**Action:** Update the CSV entry URL directly. Do NOT attempt code fixes for these — the problem is data, not code.

**Confirmed examples (2026-05-12):**

| PID | Stale URL (404) | Correct URL |
|---|---|---|
| 277774 | `byredwood.com/apartments/mi/superior-township/` | `rentatredwood.com/apartments/mi/superior-township/redwood-superior-township/floorplans/` |
| 2166 | `judwin.com/apartments/tx/houston/reserve-at-creekside/` | `reserveatcreekbend.com/` |
| 40989 | `savoyeaddison.com/` (marketing wrapper, no data) | `udr.com/dallas-apartments/addison/savoye/` |
| 220156 | `amli.com/apartments/dallas/las-colinas-apartments/` (neighborhood listing) | `amli.com/apartments/dallas/las-colinas-apartments/amli-at-escena/` |

**Production SQL to apply corrections:**
```sql
UPDATE properties SET website = 'https://rentatredwood.com/apartments/mi/superior-township/redwood-superior-township/floorplans/' WHERE property_id = '277774';
UPDATE properties SET website = 'https://www.reserveatcreekbend.com/' WHERE property_id = '2166';
UPDATE properties SET website = 'https://www.udr.com/dallas-apartments/addison/savoye/' WHERE property_id = '40989';
UPDATE properties SET website = 'https://www.amli.com/apartments/dallas/las-colinas-apartments/amli-at-escena/' WHERE property_id = '220156';
```

---

## Phase 3b — Profile-level bugs (WARM/HOT regressions)

WARM and HOT profiles sometimes cause MORE failures than COLD ones because corrupted or stale profile data actively misdirects the scraper. If a property was succeeding in cloud but fails in a canary that includes production profiles, check these before touching extraction code.

### Profile bug 1: explored_skip filtering winning_page_url

**Signal:** Property has `profile:winning_page_url` in its profile navigation, but `HOP_START` shows 0 candidates OR candidates do not include that URL despite it being in `navigation.explored_links`.

**Root cause:** `_try_link_hop` in `scraper.py` builds `explored_skip` from `profile.navigation.explored_links`. After the first successful run, the `winning_page_url` IS added to `explored_links` (it was explored). On the next run, `explored_skip` filters it out of the hop queue — the very page that worked last time is silently skipped.

**Fixed in:** `pms/scraper.py` — after building `explored_skip`, the code now strips `winning_page_url` and all `availability_links` back out:
```python
_priority_urls = {wpu} | set(availability_links)
explored_skip -= _priority_urls  # never skip known-good pages
```

**Diagnosis:** check `profile.navigation.explored_links` for the property — if `winning_page_url` is in that list, this bug caused the failure.

### Profile bug 2: infra API URL saved as winning_page_url

**Signal:** Profile's `winning_page_url` is a Supabase REST endpoint (`supabase.co/rest/v1/...`) or HERE maps API (`browse.search.hereapi.com/v1/browse?...`). The hop tries it → HARD_FAIL (401) or gets 30 location POIs misidentified as units.

**Root cause:** A previous run captured a backend API response that had unit-shaped JSON (Supabase returns property data; HERE maps returns POI address objects). The profile_updater saved it as `winning_page_url`. On subsequent runs, the hop tries it directly without browser session cookies → fails every time.

**Fixed in:** `services/profile_updater.py:_is_infra_api_url()` — blocks `supabase.co`, `hereapi.com`, `googleapis.com`, `firebaseio.com`, `amazonaws.com/` from being saved as `winning_page_url` or `known_endpoints`.

**Temporary fix for affected profiles:** clear `navigation.winning_page_url` in the profile JSON, or run one cold pass to let the infra filter prevent re-persistence.

### Profile bug 3: Rule 2 CSS cache bypassed on WARM/HOT profiles

**Signal:** Floor-plan accumulation is working (multiple sub-pages visited) but each sub-page calls `llm_dom_targeted` separately despite prior sub-pages already discovering CSS selectors.

**Root cause:** The Rule 2 replay guard was `if _fp_css_hint and not _profile_dom_field_selectors`. When the profile had saved DOM selectors (WARM/HOT), `_profile_dom_field_selectors` was non-None → Rule 2 was skipped entirely, even if the profile selectors returned 0 units on the sub-page.

**Fixed in:** `pms/adapters/generic.py` — guard changed to `if _fp_css_hint and not dom_units` (try cached selectors whenever DOM extraction produced nothing, regardless of whether profile selectors were attempted).

### Profile bug 4: LLM DOM win drops Tier 3 units

**Signal:** Tier 3 DOM scan finds N units, planner escalates (doesn't STOP), then LLM DOM also finds M units — but final result has only M units, not N+M.

**Root cause:** `result.units = dom_units` at the LLM DOM win path **replaced** `result.units` instead of merging. Tier 3 units that were already in `result.units` were silently discarded.

**Fixed in:** `pms/adapters/generic.py` — changed to `result.units = _merge_into_result_units(result.units, dom_units, ...)`.

---

## Phase 3c — Anchor keyword and scoring issues

### Anchor scoring: exact vs semantic

`_rank_internal_links` uses **two layers** of keyword matching:

1. **Layer 1 (initial filter) — exact substring:** `if kw in anchor`. Handles most cases because `"floor plan"` is a substring of `"view floor plans"`, `"floor-plan"`, etc. Links scoring 0 here are dropped before Layer 2.

2. **Layer 2 (SourceRanker re-ranking) — `rapidfuzz.fuzz.partial_ratio`:** Applied after Layer 1 via `pms/signal_engine/ranker.py`. Threshold=80 by default. Catches near-misses that exact matching would miss (e.g., `"floorplan pricing"` vs keyword `"pricing"`).

**Key host keywords** (score 120, treated as known portals — links passing the same-site filter):
```
.rentcafe.com, .appfolio.com, .onlineleasing.realpage.com, sightmap.com,
.entrata.com, prospectportal.com, securecafe.com, knockrentals.com, leasehawk.com
```

**Key path keywords** (scored in Layer 1):
```
/floor-plan (95), /availability (95), /floorplans (90), /units (85),
/models (85), /find-your-home (88), /conventional (85)
```

**Key anchor keywords** (scored in Layer 1 with fuzzy boost in Layer 2):
```
"view availability" (88), "floor plan" (90), "find your home" (88),
"check availability" (88), "view floor plan" (88)
```

**Diagnosing a missed link:** if a property URL is NOT appearing in hop candidates, run:
```python
from ma_poc.pms.scraper import _rank_internal_links
ranked = _rank_internal_links(html, base_url, limit=20)
for url, score, anchor in ranked:
    print(f'{score:>6}  {anchor[:40]:<40}  {url[:80]}')
```
If the URL IS in the HTML but scores 0, check: (a) is it filtered by `_LINK_SKIP_PATTERNS`? (b) does its host pass `is_same_site or is_portal`? (c) does it score > 0 for any keyword?

---

## Phase 4 — Agentic flows for live investigation

### Quick live-site inspection (Agent: general-purpose)

Use this to fetch a property page and inspect what's actually there before writing code:

```
Task for Agent:
Fetch https://www.example.com/floorplans/ with a Chrome User-Agent.
Show me:
1. All <script type="application/json"> blocks — size, top-level keys
2. Any sightmap.com, realpage.com, rentcafe.com, entrata.com URLs anywhere in the HTML
3. All <a href> and <form action> attributes that contain "floorplan", "availability", "conventional", "units"
4. $ price signals — count and sample
5. Anchor tags with text matching: floor plan, availability, find home, pick home, apply
```

### Multi-property failure clustering (Agent: general-purpose)

Use this to understand WHAT is failing before deciding where to fix:

```
Task for Agent:
Read C:\tmp\run-2026-05-11_failed_props.json (1877 properties).
For each property in a sample of 30, fetch the homepage with urllib and check:
- Meta generator tag (identifies CMS)
- Script src hosts (identifies PMS)
- <a href> and <form action> for known leasing portals

Cluster the 30 into categories: [Jonah Digital, Entrata custom-domain, pure SPA, Cloudflare, dead URL, other]
Show 3 examples per category.
```

### API response format investigation (Agent: general-purpose)

When a known API URL is present but extraction fails, inspect the raw response:

```
Task for Agent:
Fetch https://sightmap.com/app/api/v1/{key}/sightmaps/{id} with a Chrome User-Agent.
Show:
- Content-Type and response size (compressed vs decompressed)
- Top-level JSON structure (keys, nested path to unit list)
- First 2 unit objects — all field names and types
- Do ANY fields match our _UNIT_SIGNAL_KEYS? Check against:
  rent, minRent, maxRent, price, bedrooms, beds, sqft, squareFeet, area,
  unitNumber, unit_number, floorPlanName, availableDate, available_date, available_on
```

### Canary manifest selection (Python script)

Pick a varied set of failures for canary testing:

```python
import json
from urllib.parse import urlparse

failed = json.load(open(r'C:\tmp\run-2026-05-11_failed_props.json'))
succeeded = json.load(open(r'C:\tmp\run-2026-05-11_succeeded_props.json'))

# Pick failures not in prior canaries
used_pids = {'7950', '52116', ...}  # from prior runs
fresh_failures = [r for r in failed if r.get('pid') not in used_pids]

# Pick varied by domain
seen_domains = set()
varied_failures = []
for r in fresh_failures:
    domain = '.'.join((urlparse(r.get('url','')).hostname or '').split('.')[-2:])
    if domain not in seen_domains:
        seen_domains.add(domain)
        varied_failures.append(r)
    if len(varied_failures) == 3: break

# Pick regression sentinels from successes (5-50 units, varied domains)
seen_domains = set()
sentinels = []
for r in sorted(succeeded, key=lambda x: -(x.get('unit_count') or 0)):
    cnt = r.get('unit_count') or 0
    if not (5 <= cnt <= 60): continue
    domain = '.'.join((urlparse(r.get('url','')).hostname or '').split('.')[-2:])
    if domain in seen_domains or 'livebh.com' in r.get('url',''): continue
    seen_domains.add(domain)
    sentinels.append(r)
    if len(sentinels) == 3: break
```

---

## Phase 5 — Local canary (live fetches)

### Build the manifest CSV

```csv
property_id,url,verdict,terminal_tier,pms_detected,domain,basket,cloud_units
11611,https://www.skylineatkessler.com/,FAILED_NO_DATA,,unknown,skylineatkessler.com,FAILURE,0
12322,http://www.parkerplano.com/,SUCCESS,,unknown,parkerplano.com,REGRESSION_SENTINEL,17
```

Baskets:
- `FAILURE` — was failing in cloud; we're testing the fix
- `REGRESSION_SENTINEL` — was succeeding in cloud; we're checking we didn't break it

### Seed production profiles (critical for true canary)

Running without production profiles gives COLD-start behaviour. WARM/HOT profiles expose a completely different failure class (profile-level bugs, explored_skip issues, infra API URLs). Always seed from prod for any wide canary.

**Export profiles from production DB to CSV:**
```bash
# On prod or from a studio_results download (CSV with canonical_id + profile_json columns)
python C:/tmp/seed_all_profiles.py
# → seeds local postgres 'proppy' DB from studio_results_YYYYMMDD_HHMM.csv
```

`C:/tmp/seed_all_profiles.py` (see comments in file for format). Requires `pg8000` driver (not psycopg — no ARM64 wheel).

**Run canary with postgres profiles:**
```bash
# DATA_PROVIDER=postgres is set in ma_poc/.env
# The canary reads profiles from postgres automatically
cd ma_poc
python scripts/diagnostics/local_canary.py \
    --from-run 2026-05-12 \
    --properties-csv data/canary/local_runs/{run-dir}/canary_input.csv \
    --out-dir data/canary/local_runs/YYYY-MM-DD_description
```

The canary detects `DATA_PROVIDER=postgres` and reads scrape profiles from the local proppy DB. No `--seed-from-prod` flag needed — that flag requires a `PROD_DATABASE_URL` pointing to the live cloud DB.

**Iterative canary strategy for large failure sets (proven 2026-05-12):**

1. Seed all 30+ production profiles into local postgres
2. Run wide canary v1 with all properties → identify regression vs v1 baseline
3. Apply fixes → run v2 → compare
4. **Exclude properties that succeeded in BOTH v2 AND v3** from subsequent runs — they are stable
5. Next canary only runs the remaining failures + first-time successes that need confirmation
6. Continue until persistent failures are understood root-causes, not code bugs

**Building the reduced canary CSV:**
```python
import csv, json

def get_results(events_path):
    r = {}
    with open(events_path, errors='ignore') as f:
        for line in f:
            try:
                ev = json.loads(line)
                if ev.get('kind') == 'output.property_emitted':
                    pid = str(ev.get('property_id',''))
                    if pid not in r: r[pid] = ev.get('verdict')
            except: pass
    return r

prev = get_results('data/canary/local_runs/prev/v2/runs/.../events.jsonl')
curr = get_results('data/canary/local_runs/curr/v2/runs/.../events.jsonl')

# Keep only properties NOT consistently succeeding in both
stable = {pid for pid in prev if prev[pid]=='SUCCESS' and curr.get(pid)=='SUCCESS'}
# Write next canary input excluding stable
with open('data/canary/local_runs/next/canary_input.csv', 'w', newline='') as f:
    rows_filtered = [r for r in all_rows if r['property_id'] not in stable]
    # ... write rows_filtered
```

### Run the canary

```bash
cd ma_poc
PYTHONIOENCODING=utf-8 python scripts/diagnostics/local_canary.py \
    --from-run 2026-05-11 \
    --properties-csv data/canary/manifest_YYYY_MM_DD_description.csv \
    --limit 10 \
    --regression-basket-size 0 \
    --keep \
    --verbose \
    --out-dir data/canary/local_runs/YYYY-MM-DD_description
```

Key flags:
- `--keep` — preserves the canary SQLite DB for forensics
- `--regression-basket-size 0` — don't auto-select sentinels (we provide them in CSV)
- `--out-dir` — human-readable name describing what fix is being tested

**Important caveat:** local canary uses direct network (no residential proxy). Some properties (Cloudflare, Entrata on certain CDNs) will always fail locally. These are ENV_MISMATCH, not code regressions.

### Read the results

The canary tool reads from `runs/{date}/events.jsonl` but Jugnu writes to `v2/runs/{date}/events.jsonl`. The canary summary table will show all as TIMEOUT. **Read the actual output directly:**

```python
import json
p = 'data/canary/local_runs/YYYY-MM-DD_desc/v2/runs/2026-05-11/properties.json'
with open(p) as f: data = json.load(f)
props = data if isinstance(data, list) else data.get('properties', [])
for p in props:
    meta = p.get('_meta') or {}
    pid = p.get('apartment_id') or meta.get('canonical_id')
    url = p.get('website')
    verdict = meta.get('verdict')
    units = len(p.get('units') or [])
    tier = (p.get('_extract_result') or {}).get('tier_used')
    print(pid, url, verdict, units, tier)
```

Per-property diagnostic reports:
```
data/canary/local_runs/.../v2/runs/2026-05-11/property_reports/{pid}.md
```

Per-property event traces (check tier cascade, link-hops, API captures):
```python
import json
events = open('data/canary/local_runs/.../v2/runs/2026-05-11/events.jsonl').readlines()
for line in events:
    ev = json.loads(line)
    if ev.get('property_id') == '11611':
        print(ev.get('ts','')[11:19], ev.get('kind'), 
              {k:str(v)[:80] for k,v in ev.items() 
               if k in ('verdict','units','outcome','body_bytes','url','tier_key','reason','hop_index')})
```

**Scroll trigger log** — shows whether the L1 fetcher triggered scroll for each URL:
```bash
grep "scroll_trigger" data/canary/local_runs/.../jugnu.log
# Output: fetch.scroll_trigger url=... body_before=... body_after=... grew=True rent_appeared=True
```

### Classifying canary outcomes

| Outcome | Meaning |
|---|---|
| IMPROVED (cloud FAILED → canary SUCCESS) | Fix works |
| UNCHANGED_OK (cloud SUCCESS → canary SUCCESS, similar units) | No regression |
| UNCHANGED_FAIL (cloud FAILED → canary FAILED) | Fix didn't close this gap |
| REGRESSED (cloud SUCCESS → canary FAILED) | Code regression — **stop and investigate** |
| ENV_MISMATCH | Cloud HOT profile + proxy vs cold canary + direct network — not a code regression |

**ENV_MISMATCH pattern:** cloud got 17 units via `profile:winning_page_url`, canary got 0 via cold cascade. Check if cloud events show `anchor: 'profile:winning_page_url'` — that means cloud knew the right page from a prior run but canary is starting cold.

**Pre-deploy gate:** REGRESSED == 0 (excluding confirmed ENV_MISMATCH).

---

## Phase 6 — Debugging specific layer failures

### L1 — Nothing captured in network_log

**Steps:**
1. Check `fetch.scroll_trigger` log — did scroll fire? Did `rent_appeared=True` after?
2. Check `fetch.anchor_stable` log — did the anchor stability gate fire? If not, the page had > 40KB body or > 20 anchor links already. If yes, check `links=N stable=True/False` — False means the DOM was still growing at timeout.
3. Check body_bytes on the entry-page fetch. If < 40KB, Playwright got a React shell — links were lazy-loaded. The anchor stability gate should handle this; if it didn't, check if RENDER mode was used.
4. Look at `Network Log Entries` in the per-property report. If 0 or very low, the XHR didn't fire.
5. For deferred APIs (SightMap, Entrata widgets): check if the API fires only after scroll (IntersectionObserver) or after 3-5s (SPA init). Add the host to `_LATE_RENDER_HOSTS` or the portal late-render list in `fetcher.py`.

**Non-deterministic SPA rendering:** the same property can produce 19KB (React shell) one run and 75KB (fully hydrated) the next. If you see intermittent SUCCESS/FAIL on the same URL across canary runs, this is the cause. The anchor stability gate makes it deterministic for < 40KB pages by polling anchor link count instead of using a fixed sleep.

**Quick local check:**

```python
import urllib.request, re
url = 'https://www.property.com/floorplans/'
req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'})
with urllib.request.urlopen(req, timeout=30) as r:
    html = r.read().decode('utf-8', errors='replace')
print(f'size: {len(html)}, $ signs: {len(re.findall(r"\$\s*\d{3,4}", html))}')
# Find all <script type="application/json"> blocks
for m in re.finditer(r'<script[^>]*type=["\']application/json["\'][^>]*>([\s\S]*?)</script>', html, re.IGNORECASE):
    print('  JSON block:', len(m.group(1)), 'chars, keys:', list(json.loads(m.group(1).strip() or '{}').keys())[:10])
```

### L2 — Link-hop not finding the right page

**Steps:**
1. Check the `Extraction Attempts` section — is `generic:floorplan_subpages_detected` or `generic:embedded_portal_detected` present?
2. Check the link-hop events: `extract.link_hop_started` shows the `candidates` list with scores and anchors.
3. If the right URL is NOT in candidates: fetch the homepage manually and look for the URL in `<a href>`, `<form action>`, and `href` attributes of nav elements.
4. Score it manually against `_LINK_ANCHOR_KEYWORDS` and `_LINK_PATH_KEYWORDS`.

**Common missed URL patterns and fixes:**

| Pattern | Root cause | Fix |
|---|---|---|
| `/montclair/alister-montclair/conventional/` | Entrata custom-domain URL; anchor "Pick Your Home" (not "Floor Plans") | Add "pick your home" to anchor keywords; add `/conventional/` to path keywords; parse `<form action>` |
| `https://property.prospectportal.com/...` | Cross-site link filtered by same-host check | Add `prospectportal.com` to `_LINK_HOST_KEYWORDS` |
| `/floorplans/the-edgefield/` | Floor-plan index sub-page; empty anchor text (JS-rendered) | HTML sub-page link discovery; filter by path prefix + score ≥ 88 |
| `https://xxxxxx.onlineleasing.realpage.com/` | RealPage OLL cross-domain | Already in `_LINK_HOST_KEYWORDS` |

### L3 — API captured but extracted wrong data

**Steps:**
1. Check the per-property report Phase 5 section — which API URLs were analyzed and what classification did they get?
2. For NOISE classification: is it legitimately noise (chatbot, analytics) or a false negative?
3. For missed APIs (not in Phase 5): look at `_raw_api_responses` in the result dict — check what was in the network_log.
4. Check `has_unit_signals` manually: does the first item in the API response have ≥ 2 keys from `_UNIT_SIGNAL_KEYS` in `_merge_fns.py`?

**Common unit-signal misses:**

| Platform | Keys used | Missing from `_UNIT_SIGNAL_KEYS` |
|---|---|---|
| SightMap REST API | unit_number, price, area, available_on | `available_on` (now added) |
| Custom CMS | amount, sq_footage, move_in | not generic enough — check and add |

---

## Phase 7 — Tests to write before shipping

Every fix needs:

1. **Unit test** for the helper function (e.g., `detect_embedded_portal_urls`, `_rank_internal_links`).
2. **Integration test** through the generic adapter — verify the hint propagates to the result dict.
3. **Link-hop contract test** — verify the hint reaches `_try_link_hop` and a sub-fetch is queued.
4. **Canary run** with FAILURE + REGRESSION_SENTINEL properties.

Test location convention:
- Unit + integration tests: `tests/pms/adapters/test_{feature}.py`
- Link-hop contracts: `tests/pms/test_link_hop_helpers.py` or `tests/integration/contracts/`
- Full-chain tests: `tests/pms/adapters/test_embedded_portal_detection.py`

Run before canary:
```bash
pytest tests/pms tests/fetch -q --tb=line  # ~60s
```

---

## Quick-reference: key file locations

| What | Where |
|---|---|
| Link-hop candidate scoring | `pms/scraper.py:_LINK_ANCHOR_KEYWORDS`, `_LINK_PATH_KEYWORDS`, `_LINK_HOST_KEYWORDS` |
| PMS sub-path priors | `pms/scraper.py:_PMS_SUB_PATH_PRIORS` |
| Unit signal gate | `pms/adapters/_merge_fns.py:_UNIT_SIGNAL_KEYS`, `has_unit_signals()` |
| Signal engine scoring constants | `pms/signal_engine/defaults.py` — `DEFAULT_KIND_BASE_SCORES`, `DEFAULT_ANCHOR_KEYWORDS`, `DEFAULT_HOST_KEYWORDS`, `DEFAULT_PATH_KEYWORDS` |
| SourceQualifier field combos | `pms/signal_engine/defaults.py:create_default_qualifier()` |
| L1 scroll trigger | `fetch/fetcher.py` — search `scroll_trigger` |
| L1 anchor stability gate | `fetch/fetcher.py` — search `anchor_stable` |
| L1 body cap | `fetch/fetcher.py:_on_response` — search `_body_cap` |
| Portal late-render wait | `fetch/fetcher.py` — search `portal_match` list |
| Parked domain detection | `fetch/response_classifier.py:_is_parked_domain()`, `_PARKED_DOMAIN_PHRASES` |
| Portal URL detection (embedded JSON) | `pms/adapters/_html_extract.py:detect_embedded_portal_urls()` |
| SecureCafe URL synthesis (SPA hrefs) | `pms/adapters/_html_extract.py:detect_securecafe_portal_url()` |
| RealPage CWS credential probe | `pms/adapters/generic.py:_probe_realpage_cws()`, `generic:realpage_cws` tier |
| Floor-plan sub-page detection | `pms/adapters/_html_extract.py:detect_floorplan_subpage_urls()` |
| Form action parsing | `pms/scraper.py:_rank_internal_links → _href_anchor_pairs()` |
| Floor-plan accumulation loop | `pms/scraper.py:_try_link_hop` — search `_in_floorplan_accumulation` |
| explored_skip priority URL stripping | `pms/scraper.py:_try_link_hop` — search `_priority_urls` |
| Profile winning_page_url update | `services/profile_updater.py` — search `winning_url` |
| Infra API URL filter | `services/profile_updater.py:_is_infra_api_url()`, `_INFRA_API_DOMAINS` |
| Per-property report generator | `scripts/reports/per_property.py` |

---

## 2026-05-12 wide canary campaign: summary of findings

**Setup:** 30 production properties with prod scrape profiles seeded from studio_results CSV.  
**Baseline (v2, no fixes, with prod profiles):** 7/30 SUCCESS (23%) — catastrophic regression from v1 (17/30) caused entirely by explored_skip bug.

| Canary | Success | Key fix validated |
|--------|---------|-------------------|
| v2 | 7/30 (23%) | Baseline with prod profiles — explored_skip regression discovered |
| v3 | 19/30 (63%) | explored_skip fix — strips winning_page_url from explored_skip set |
| v4 | 13/24 (54%) | Parked domain detection, securecafe host keyword, /models path keyword, infra API filter, Rule 2 merge |
| v5 (targeted) | 3/4 | RPFP_config probe (11159: 5u), /models keyword (56166: 8u), securecafe anchor stability (25964: 1u) |
| v7 (securecafe) | 2/2 | Anchor stability gate (272521: 8u, 25964: 1u) |

**Persistent failures after all fixes:**

| pid | Root cause | Status |
|-----|-----------|--------|
| 11961, 12557, 18941 | Domain not available (parked) | Emit DEAD_URL now — excluded from success denominator |
| 25964, 272521 | SecureCafe SPA — NOW FIXED with anchor stability gate | Fixed in v7 |
| 56166 | HERE maps as winning_page_url, /models page — NOW FIXED | Fixed in v5 with /models keyword |
| 11159 | RealPage CWS — NOW FIXED with RPFP_config probe | Fixed in v5 |

**All 10 non-parked failures from original v2 canary are now resolved or correctly classified as DEAD_URL.**

---

## Canary manifests by fix history

| Date | Run dir | Fix tested | Outcome |
|---|---|---|---|
| 2026-05-12 | `manifest_2026_05_12.csv` | Universal PMS priors | 5 IMPROVED, 0 REGRESSED |
| 2026-05-12 | `manifest_2026_05_12_portal_detect.csv` | Portal detection from embedded JSON | 2 IMPROVED, 0 REGRESSED |
| 2026-05-12 | `manifest_2026_05_12_portal_detect_v2.csv` | Fallback portal-hints promotion | 2 IMPROVED, 0 REGRESSED |
| 2026-05-12 | `manifest_2026_05_12_floorplan_subpage.csv` | Scroll + Entrata /conventional/ prior | 11611 SUCCESS 5u, 228073 SUCCESS 1u |
| 2026-05-12 | `manifest_2026_05_12_floorplan_and_sightmap.csv` | SightMap 512KB cap + floor-plan HTML discovery | 11611 SUCCESS 10u |
| 2026-05-12 | `2026-05-12_wide_v2_all_profiles` | Prod profiles baseline | 7/30 — explored_skip regression |
| 2026-05-12 | `2026-05-12_wide_v3_explored_fix` | explored_skip + Rule 2 fixes | 19/30 |
| 2026-05-12 | `2026-05-12_wide_v4_fixes` | Parked domain, securecafe keyword, /models, infra API filter, Rule 2 merge | 13/24 |
| 2026-05-12 | `2026-05-12_targeted_v5_rpfp` | RPFP_config probe, /models keyword, securecafe from LLM nav_hint | 3/4 (11159, 56166, 25964) |
| 2026-05-12 | `2026-05-12_securecafe_v7_anchor_stable` | Anchor-link DOM stability gate | 2/2 (272521, 25964) |

All canary outputs: `data/canary/local_runs/`

---

## 2026-05-12 production run analysis (100 shards, 4982 properties)

**Run result:** 3780/4982 succeeded (75.87%). LLM cost $15.05. All 100 shards breached the 95% SLO.

### Top failure tiers

| Terminal tier | Count | Root cause |
|---|---|---|
| `TIER_1_API` (generic, no PMS) | 453 failures | Cluster by management-company domain; P3 pattern |
| `TIER_1_API_ENTRATA` | 241 failures | Entrata adapter bug or CF-blocked |
| `TIER_1_API_RENTCAFE_SHAPE_REJECTED` | **190 new failures** | SecureCafe.com CF-blocked on all hops (no proxy); or wrong securecafe slug after redirect |
| `TIER_1_API_ONESITE` | 46 failures | OneSite adapter zero |
| `TIER_1_API_SIGHTMAP_SHAPE_REJECTED` | **26 new failures** | equityapartments.com uses SightMap as display widget only; RC3 cascading deferral bug |
| `GENERIC_VALIDITY_REJECTED` | **21 new failures** | api_broad finding dimensionless CMS rows; infra WPU consuming hop #1 |
| `FAILED` (timeout kill) | **187 no-emit** | Unbounded `llm_dom_targeted` calls on per-floor-plan sub-pages (3s–421s each) |

### Key regressions vs 2026-05-11

- **TIER_1_API_RENTCAFE_SHAPE_REJECTED (+190)**: RC3 cascading deferral (fixed: `hop_depth == 0` guard) + securecafe CF-blocked without proxy
- **Timeout kills (+183)**: LLM DOM now the dominant tier (1811 calls/run). Individual calls of 7–40 minutes wedge properties. Partial recovery now implemented.
- **TIER_MERGED_CROSS_PAGE −617**: Fewer cross-page merges due to RC3 deferral bug

### Key improvements vs 2026-05-11

- FAILED_NO_DATA: 869 today vs 1877 yesterday (-1008)
- 1141 recoveries (properties that previously failed now succeed via TIER_4_LLM_DOM)
- LLM cost: $15.05 vs $26.46 yesterday

### Persistence health alert

`profile_replay_hit_rate = 7.4%` (SLO threshold: 30%). Root cause: `TIER_4_LLM_DOM` (dominant at 1811 calls) structurally never saves a replayable `LlmFieldMapping` — it saves CSS selectors to `dom_hints.field_selectors` via a separate channel. `generic:profile_replay` only replays API URL + JSON-path mappings from `TIER_4_LLM_API`. The 30% SLO is inappropriate for DOM-heavy property mixes; the correct SLO is CSS selector replay rate from `generic:llm_dom_targeted` with `fp_css_hint_replay` reason.

### Code fixes shipped from this run

| Fix | File | Impact |
|---|---|---|
| RC3 `hop_depth == 0` guard — prevents cascading deferral from hop pages | `pms/signal_engine/decider.py` | Fixes ~30 regressions per run |
| `_is_infra_api_url()` extended + checked at hop-queue injection | `services/profile_updater.py`, `pms/scraper.py` | Fixes nestiolistings/omappapi/matterport WPU pollution |
| `winning_page_url` invalidation on hop failure (clear + reset to COLD) | `services/profile_updater.py` | Prevents stale WPU from blocking hop #1 every run |
| Securecafe slug from `final_url` on cross-host redirect | `pms/adapters/generic.py` | Fixes alias-domain properties (affinity56→elevation56) |
| `_session_blocked_urls` — no re-synthesis of already-BOT_BLOCKED URLs | `pms/scraper.py`, `pms/adapters/generic.py` | Stops ~4-8s wasted securecafe re-synthesis on every hop page |
| Silent homepage redirect detection — skip hop if `final_url == entry_url` | `pms/scraper.py` | Preserves hop budget and LLM budget for real sub-pages |
| Anchor links ranked above PMS/universal priors (score 5100–5600) | `pms/scraper.py:_rank_internal_links` | Page-discovered floor-plan CTAs tried before guessed template paths |
| Property sub-path priors on deep hop URLs (/floorplans appended to 3+ segment hops) | `pms/scraper.py:_try_link_hop` | Fixes AMLI-style sites where PMS priors pointed to wrong base URL |
| `api_broad` pre-filter: dimensionless rows rejected before planner sees them | `pms/adapters/generic.py` | Stops false `ESCALATE_LINK_HOP` on CMS config rows |
| JSON-LD filter: `floor_plan_name + beds/baths` accepted as valid partial record | `pms/adapters/generic.py` | Allows AMLI-style floor plans with dynamic pricing to pass through |
| `numberOfBedrooms`/`numberOfBathroomsTotal` extracted from JSON-LD | `pms/adapters/_html_extract.py` | Schema.org Apartment nodes now populate beds/baths correctly |
| Partial result recovery on timeout (units accumulated in hop are persisted) | `scripts/runners/jugnu.py`, `pms/scraper.py` | Saves 7–31 units that were previously discarded on 600s kill |
| `_detect_url_extension` (.aspx → rentcafe) confidence lowered to 0.40 | `pms/detector.py` | Requires HTML corroboration; prevents yottareal.com misrouting |
| `_analyzed_api_urls` dedup before LLM calls | `pms/adapters/generic.py` | Prevents same API being analyzed by LLM on every hop page |

---

## 2026-05-12 targeted canary results (fixes_v1 + fixes_v2)

**Setup:** 24 properties — 5 per failure cluster (C1 RentCafe, C3 SightMap, C4 Validity, C2 Timeout) + 5 regression sentinels. Production profiles seeded into local postgres from studio_results CSV.

| Canary | IMPROVED | UNCHANGED_OK | UNCHANGED_FAIL | REGRESSED | Key validation |
|---|---|---|---|---|---|
| fixes_v1 (original bugs) | 6 | 5 | 13 | 0 | RC3, infra WPU, api_broad, sentinels |
| fixes_v2 (+ URL corrections) | **9** | **5** | **7** | **0** | CSV corrections validated |

**Notable fixes_v2 improvements over fixes_v1:**

| PID | Domain | Result | Fix |
|---|---|---|---|
| 256537 | affinity56.com | **6 units** | Securecafe slug from `final_url` after cross-host redirect (Class 11) |
| 277774 | rentatredwood.com | **5 units** | CSV URL corrected (was byredwood.com/404) |
| 40989 | udr.com | **60 units** | CSV URL corrected (was savoyeaddison.com marketing wrapper) |
| 2166 | reserveatcreekbend.com | **6 units** | CSV URL corrected (was judwin.com/404) |

**Persistent failures confirmed as ENV_MISMATCH or architectural limits:**
- equityapartments.com (C3): Angular SPA — data requires browser JS execution + proxy. No code fix applicable.
- thecurtisapts.com, ascentfitzsimons.com: Data behind dynamic widget (Fiona/ProspectPortal). Needs XHR capture on hop pages.
- AMLI 220156, reserveatcityplace 217930: ProspectPortal iframe + LLM wedging. 120s local limit insufficient; production (600s) may succeed.

**Pre-deploy gate: REGRESSED=0 on both canary runs. Safe to push.**

### How to seed production profiles for a canary

```sql
-- Export from Cloud SQL
SELECT canonical_id, payload, updated_at FROM scrape_profiles WHERE canonical_id IN (...);
```

Save the output as a CSV (`canonical_id, payload, updated_at`), then seed into local postgres:
```bash
python C:/tmp/seed_all_profiles.py  # reads studio_results_YYYYMMDD_HHMM.csv
```
The canary writes profiles to `canary.sqlite` (isolated), not proppy postgres. Improvements from cold-start are therefore genuine code improvements, not profile-assisted wins.
