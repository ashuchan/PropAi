# FAILED_NO_DATA Property Debugging Playbook

**Audience:** Claude Code sessions debugging extraction failures after a cloud run.  
**Source authority:** 2026-05-11/12 investigation session — every technique below was used live.  
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
2. Check body_bytes on the entry-page fetch. If < 50KB, Playwright got a shell.
3. Look at `Network Log Entries` in the per-property report. If 0 or very low, the XHR didn't fire.
4. For deferred APIs (SightMap, Entrata widgets): check if the API fires only after scroll (IntersectionObserver) or after 3-5s (SPA init). Add the host to `_LATE_RENDER_HOSTS` or the portal late-render list in `fetcher.py`.

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
| L1 scroll trigger | `fetch/fetcher.py:_do_render` — search `scroll_trigger` |
| L1 body cap | `fetch/fetcher.py:_on_response` — search `_body_cap` |
| Portal late-render wait | `fetch/fetcher.py` — search `portal_match` list |
| Portal URL detection | `pms/adapters/_html_extract.py:detect_embedded_portal_urls()` |
| Floor-plan sub-page detection | `pms/adapters/_html_extract.py:detect_floorplan_subpage_urls()` |
| Form action parsing | `pms/scraper.py:_rank_internal_links → _href_anchor_pairs()` |
| Floor-plan accumulation loop | `pms/scraper.py:_try_link_hop` — search `_in_floorplan_accumulation` |
| Per-property report generator | `scripts/reports/per_property.py` |

---

## Canary manifests by fix history

| Date | Manifest | Fix tested | Outcome |
|---|---|---|---|
| 2026-05-12 | `manifest_2026_05_12.csv` | Universal PMS priors | 5 IMPROVED, 0 REGRESSED |
| 2026-05-12 | `manifest_2026_05_12_portal_detect.csv` | Portal detection from embedded JSON | 2 IMPROVED, 0 REGRESSED |
| 2026-05-12 | `manifest_2026_05_12_portal_detect_v2.csv` | Fallback portal-hints promotion | 2 IMPROVED, 0 REGRESSED |
| 2026-05-12 | `manifest_2026_05_12_floorplan_subpage.csv` | Scroll + Entrata /conventional/ prior | 11611 SUCCESS 5u, 228073 SUCCESS 1u |
| 2026-05-12 | `manifest_2026_05_12_floorplan_and_sightmap.csv` | SightMap 512KB cap + floor-plan HTML discovery | 11611 SUCCESS 10u |

All manifests and canary outputs: `data/canary/local_runs/`  
Final reports (when written): `data/canary/local_runs/{dir}/FINAL_REPORT.md`
