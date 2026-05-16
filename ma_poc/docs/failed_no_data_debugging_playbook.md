# FAILED_NO_DATA Debugging Playbook

**Working directory for every command:** `ma_poc/`
**Audience:** Claude Code (or any engineer) debugging extraction failures after a cloud run.
**Updated:** 2026-05-15 (afternoon). Last campaign reviewed: 2026-05-15 cloud run.
**This update:** added §0 anti-patterns 11-13 (STUB-without-frames, redirect-host-anchor, fp_signal-without-structure); added Q10-Q12 to §3 diagnostic; new §8.9-§8.17 extraction gaps (redirect landed_url, listing-structure gate, JSON-LD Product+additionalProperty, generic inline-JS PMS parser, AppFolio slug synth, vendor-iframe widgets, open-by-default discovery, decider rule conflation, Wix fuzzy normalize); §11 STUB classifier updated with frame-enumeration requirement; §14 added Playwright frame-enumeration snippet; §15 file reference appended with all 2026-05-15 additions.

---

## TL;DR — the 5-minute first pass

```
1. Pull artifacts        scripts/diagnostics/analyze_cloud_run.py --date YYYY-MM-DD --compare-date <yesterday>
2. Open                  data/reports/cloud_run_<date>/{summary.md, comparison_with_<yesterday>.md, failures.csv}
3. Read the top-N        terminal_tier histogram from summary.md
4. For each top bucket   sample 3 PIDs; run the §3 9-question checklist on ONE before forming a hypothesis
5. Decide               (a) code fix, (b) data fix (CSV stale URL), (c) infra fix (CF/proxy)
```

**If a fix is "supposed to be deployed but the number didn't move":** do not assume it didn't deploy. Investigate whether the fix actually fires by tracing events.jsonl for ONE specific PID. The fix may be a stub against a missing upstream field — see §4 Verification protocols and §10 Architecture invariants.

---

## Phase 0 — Anti-patterns I keep falling into

Be explicit with yourself when you catch yourself doing any of these. Most "obvious" first-pass answers below were wrong in the last 3 days of investigation.

| Anti-pattern | What I did wrong | What to do instead |
|---|---|---|
| **Trusting the tier label** | Claimed `TIER_1_API_RENTCAFE_SHAPE_REJECTED` meant an API was captured but malformed. Reality: 0 RentCafe JSON XHRs were ever captured; the label fires whenever ANY response (incl. HTML page itself) is buffered. | Always verify the tier label against the actual `network_log` content. Decode each label per §5 *Tier-label decoder*. |
| **Assuming undeployed when numbers don't move** | "Entrata 306 → 306 means today's fixes didn't deploy." Reality: fixes deployed but a deeper bug (missing `ctx.hop_depth` field) silently nullified them. | Pick ONE PID in the unchanged cluster; trace its events.jsonl for the new tier_attempted reasons the fix should have produced. If those reasons are absent, the fix didn't fire — figure out *why*. |
| **Trusting agent code claims** | Believed an agent's "`_probe_known_endpoints()` uses `ctx.base_url`" without grepping. Reality: code used `page.url`. The proposed fix would have changed nothing. | After any agent code claim, `grep -n "<function>" <file>` and read the actual implementation before forming a fix. |
| **Trusting agent URL claims** | Believed agent's "bowmanstation has `dnn506yrbagrg.cloudfront.net` CDN". Live fetch found `g5-c-` classes and `g5-assets-cld-res.cloudinary.com` — completely different. | When an analysis names URLs/classes/domains, live-fetch and verify before changing code. `python -c "import urllib.request..."` takes 5 seconds. |
| **Skipping body/text ratio** | Said "8181medcenter homepage has no unit data, only contact form dropdowns." Reality: 678 units in a 1.02 MB embedded JSON state blob. | If `body_bytes / text_bytes > 20`, the page is a heavy SSR state. WALK every `<script type="application/json">` block before declaring no data. See §8 *Embedded-JSON walker*. |
| **Generic clustering without sample PIDs** | "AppFolio cluster needs a fix" with no specific evidence. | Always sample 2 IMPROVED + 2 UNCHANGED_FAIL in the same cluster and diff their candidate lists + portal URLs. The difference IS the gap. See §9. |
| **Stopping at the first plausible cause** | For 29washington, blamed `rcLoadContent.ashx` AJAX gap exclusively. Real cause stacked: `rc3_defer` ran on hop pages because `ctx.hop_depth` was always 0. | Don't stop after one plausible explanation. If the page is genuinely reachable and has signals, also check tier-cascade behaviour for hop_depth, gate misfires, label leaks. |
| **Delegating live fetches to agents** | Agent got blocked on Bash perms; I waited and re-launched 3 times. | Run urllib live fetches in your own Bash. 30 seconds vs 3 minutes. |
| **Surface diff reading** | "17 LLM_DOM regressions" with no per-PID divergence point. | Diff events.jsonl for ONE PID between yesterday and today; the divergence appears immediately. |
| **Treating FAILED_NO_DATA as homogeneous** | "676 failures" without splitting by terminal_tier. | Always cluster by `terminal_tier` first, then by `fetch_outcome` to separate ENV_MISMATCH (CF-blocked locally) from real extraction misses. |
| **Declaring STUB without enumerating frames** | Called 3 Wix sites (46179 / 118965 / 292955) "STUB sites" after a Playwright probe that only checked top-level `document.body.innerText`. Reality: each had real unit data in cross-origin iframes (`wix-visual-data.appspot.com/index?pageId=...&compId=...` with the table; `yourcrossstreet.com/property/...` with availability). The static HTML and even top-level rendered text had nothing — but `page.frames` enumeration would have surfaced the data. | Before declaring STUB, enumerate `page.frames` (Playwright) AND grep the rendered HTML for iframe `src=` patterns. See §11 *STUB classifier* and §14 *Frame enumeration snippet*. |
| **Trusting a CSV URL when the redirect host owns the inventory** | For 53592 (1701arch.com) and 119144 (windsorburnet.com), `_rank_internal_links` filtered out the actual unit-page anchors because they pointed at `livethearch.com` / `windsorcommunities.com` (the redirect target), not the CSV host. Same-host filter dropped the only correct links. | When `fetch_result.final_url` host ≠ `base_url` host, ALSO accept anchors on the landed host as "same-site". PMS-priors should be synthesised against the landed host too. See §8.9 *Redirect-aware landed_url*. |
| **Trusting fp_signal count without listing-structure check** | PID 119144 had fp_signals=3 from `priceRange: "$1490 - $3824"` (LocalBusiness JSON-LD aggregate) + marketing copy ("Choose from our 1, 2, 3-bedroom apartments") + amenityFeature description. LLM_DOM ran and correctly returned 0 units — but $0.005 wasted and the page was misclassified as "extractable but extraction failed" rather than STUB_AGGREGATE_COPY. | When fp_signals ≥ 2 AND `has_listing_structure(html) == False` → STUB_AGGREGATE_COPY. The structural check looks for ≥2 `<tr>` rows with rent / ≥2 unit-card class markers / ≥2 Offer-array entries / ≥2 PropertyValue dimension entries. See §8.10 *fp_signal listing-structure gate*. |

---

## Phase 1 — Pull artifacts

```bash
# Local-only when data is already mirrored at c:/tmp/run-<date>/
python scripts/diagnostics/analyze_cloud_run.py --date 2026-05-14 --compare-date 2026-05-13

# Force-pull from GCS first (requires gcloud auth + cloud-sql-proxy not needed)
python scripts/diagnostics/analyze_cloud_run.py --date 2026-05-14 --pull --expected-shards 100
```

If `gcloud storage rsync` fails with "Reauthentication failed" but ADC is healthy, mirror via the storage REST API instead — sample script at [C:/tmp/mirror_2026_05_14.py](C:/tmp/mirror_2026_05_14.py) (uses `google.cloud.storage` with default credentials, downloads only `report.json + events.jsonl + properties.json + llm_report.json + issues.jsonl` per shard).

Output: `data/reports/cloud_run_<date>/{summary.md, comparison_with_<prev>.md, failures.csv, successes.csv, summary.json}`.

---

## Phase 2 — Cluster failures (read summary.md and failures.csv)

Top of [summary.md](ma_poc/data/reports/) lists terminal-tier counts. The biggest bucket is usually 200-300 failures. Don't try to fix the whole bucket — pick 3-5 representative PIDs.

```python
import csv
from collections import defaultdict
rows = list(csv.DictReader(open('data/reports/cloud_run_2026-05-14/failures.csv', encoding='utf-8')))
buckets = defaultdict(list)
for r in rows:
    if r['verdict'] == 'FAILED_NO_DATA':
        buckets[r['terminal_tier']].append(r)
for t, lst in sorted(buckets.items(), key=lambda x: -len(x[1])):
    print(f'  {len(lst):>4}  {t}')
```

**Always also split by fetch_outcome.** A `TIER_1_API_RENTCAFE_SHAPE_REJECTED` failure with `fetch_outcome=OK` is an extraction bug; with `fetch_outcome=BOT_BLOCKED` it's a label leak — the entry page never loaded.

---

## Phase 3 — Per-PID 9-question diagnostic checklist

For EVERY FAILED_NO_DATA PID you investigate, answer all 9 in order before forming a hypothesis. Each takes <2 minutes from `events.jsonl`.

| # | Question | Where to look | What different answers tell you |
|---|---|---|---|
| **Q1** | Entry-page `fetch.completed` outcome | first `fetch.completed` for this PID | OK → L3 problem; BOT_BLOCKED / CF_CHALLENGE → infra; DEAD_URL → CSV data quality |
| **Q2** | `body_bytes / text_bytes` ratio | `extract.html_characterized` event | ratio > 20 → heavy SSR state blob, walk embedded JSON (§8); ratio 2-5 → normal HTML; text_bytes < 1000 → React shell, anchor-stability gate may not have fired |
| **Q3** | `floor_plan_signal_count` | `extract.html_characterized` event | 0 → genuinely no unit signals on this page (probably STUB_URL — see §11); 1 → at threshold, marginal; ≥2 → unit data is present, extraction missed it |
| **Q4** | `jsonld_types` | `extract.html_characterized` event | Only `ApartmentComplex`/`PostalAddress`/`ImageObject` → property-level metadata, no unit Offers; presence of `Apartment`/`FloorPlan`/`Offer` → extractable JSON-LD |
| **Q5** | Candidate list size + composition | `extract.link_hop_started.candidates` | 1 candidate at score 10001 (profile:winning_page_url) → §6 self-fetch suppression eligibility; 0 → discovery failed entirely; ALL 5 at score ≥5000 with same host → no real anchors found, only PMS priors |
| **Q6** | Tier sequence | series of `extract.tier_attempted` | `generic:llm` with `reason=rc3_defer_monolithic_to_hop` on HOP page (`hop_index ≥ 1`) → §10 hop_depth bug; `generic:embedded_json ran_empty "1 SSR blob(s) had no unit signals"` AND body/text ratio > 20 → §8 walker not catching vendor path |
| **Q7** | Terminal label vs reality | last `extract.tier_won` or terminal in CSV | SHAPE_REJECTED tier without ANY captured rentcafe-host JSON → §5 label leak |
| **Q8** | Profile state | prod DB query (§6) | `wpu` matches infra-URL pattern → profile-poisoning; `wpu == entry_url` → §6 self-fetch eligible; `explored_links > 10` and includes a PMS prior → §6 explored_skip eligible |
| **Q9** | Portal URLs anywhere in HTML | live fetch + grep | `sightmap.com/embed/`, `*.appfolio.com/listings`, `*.onlineleasing.realpage.com` present but NOT in candidate list → §8 portal scan miss |
| **Q10** | Cross-origin iframes rendered into the DOM | Playwright `page.frames` + Playwright `page.content()` after scroll/wait | Any frame URL not on a known infra host (Google analytics, maps, parastorage CDN, social) with unit-shaped innerText (`\d+ bed`, `$NNN`, `sqft`) → §8.11/§8.15 vendor-iframe widget. Common offenders: `wix-visual-data.appspot.com`, `yourcrossstreet.com/property/`, `embed.fortresstech.io`, `my.hy.ly`. Static-HTML grep can MISS these — Wix injects the iframe post-hydration. |
| **Q11** | Entry-page redirect host vs CSV host | `fetch_result.final_url` vs `base_url` | When hosts differ, anchors on the page point at the LANDED host (cross-domain). The CSV-host filter in `_rank_internal_links` drops them. → §8.9 redirect-aware landed_url. Diagnostic command: live-fetch the CSV URL with `urllib.request.urlopen(...)`; if `resp.geturl()` returns a different host, that host owns the inventory. |
| **Q12** | Listing structure vs fp_signals | `has_listing_structure(html)` against entry/hop HTML | fp_signals ≥ 2 BUT `count_listing_structural_signals == 0` → marketing aggregate copy (priceRange in LocalBusiness JSON-LD, "Choose from 1, 2, 3-bedroom"), not real listings. Classify as STUB_AGGREGATE_COPY rather than chasing extraction. → §8.10. |

When you finish Q1–Q12, the root cause is almost always one of: § ENV_MISMATCH (CF-blocked locally only), §6 profile poisoning, §8 extraction gap, §10 architectural invariant violation, §11 STUB_URL.

---

## Phase 4 — Verification protocols (never trust without verifying)

Apply before every code change:

| Claim type | Verification command | Example |
|---|---|---|
| "Function `_X()` does Y" | `grep -n "def _X\b" pms/**/*.py` then read the actual code | Agent claimed `_probe_known_endpoints()` reads `ctx.base_url`; code reads `page.url`. |
| "URL `https://X` returns Y" | `python -c "import urllib.request; ..."` 5-second live fetch | Agent claimed mark-taylor has SightMap CDN `dnn506yrbagrg.cloudfront.net`; live fetch found `g5-c-` Vue components. |
| "Tier W fires under condition Z" | grep events.jsonl for the specific PID; match against tier sequence | The 2026-05-13 `hop_depth == 0` decider guard "fired" but events showed the same `rc3_defer` on hop pages — gate was reading a missing ctx field. |
| "Fix deployed on date D" | `git log --since=D --until=D+1 --pretty=oneline` in `ma_poc/` | Always confirm the commit landed; "should have deployed" ≠ "deployed". |
| "Profile contains X" | Run §6 SQL summary query, NOT the export query | Don't load the full 305 KB profile_json blob just to check a maturity tag. |
| "Cluster of N failures has cause Y" | `python C:/tmp/trace_clusters.py` against 2 fail + 2 success PIDs in the same cluster | "AppFolio fails because adapter X" became "the AppFolio iframe URL pattern wasn't in `_PORTAL_URL_PATTERNS`" only after the diff. |

---

## Phase 5 — Tier-label decoder

The terminal_tier in `failures.csv` is NOT a description of what happened. It's a label assigned by the last adapter that ran. Common label-vs-reality leaks:

| Label | Misleading interpretation | What it actually means | How to verify |
|---|---|---|---|
| `TIER_1_API_RENTCAFE_SHAPE_REJECTED` | "RentCafe API captured but malformed" | At least one response was buffered in `_api_responses` AND none passed `_is_rentcafe_response`. The response might be the HTML page itself or a third-party tracker JSON. | grep events.jsonl for `rentcafeapi.com\|widgets.rentcafe.com\|securecafe.com\|yardi.com` — if no hits, NO RentCafe JSON was captured. The 2026-05-15 fix tightened this to require json content-type AND a rentcafe-family host. |
| `TIER_1_API_ENTRATA` | "Entrata adapter captured an API" | Adapter ran (PMS detected as entrata); 0 units emerged from any tier. Most often 0 XHRs captured because the widget XHR is sub-page-only and the hop probe didn't fire (`page=None` bug — see §10). | grep for `/Apartments/module/widgets/`; if absent, the probe never fired. |
| `TIER_1_API_SIGHTMAP_SHAPE_REJECTED` | "SightMap API returned wrong shape" | Sometimes correct. Often: the page has a SightMap iframe (`sightmap.com/embed/...`) but the L1 fetcher never navigated into it. The adapter sees zero sightmap-shape responses → SHAPE_REJECTED. | Live-fetch the entry HTML; grep for `sightmap.com/embed/`. Presence + 0 captured sightmap responses → §8 portal scan missed it. |
| `FAILED_NO_DATA` (verdict, not tier) | "Page exists but extraction failed" | Entry-page `fetch.completed` was OK; aggregate extraction produced 0 units. Doesn't say WHERE in the cascade. | Q1-Q9 checklist. |
| `FAILED_UNREACHABLE` (verdict) | "Site down" | Entry-page fetch_outcome ≠ OK. Locally vs cloud distinction matters: CF-blocked locally → ENV_MISMATCH; CF-blocked in cloud too → real infra problem. | Compare local canary `fetch.completed` to cloud's. |

---

## Phase 6 — Profile-state inspection (DO THIS BEFORE READING ANY CODE)

A poisoned profile can cause MORE failures than any code bug. Run this query against prod first.

### 6.1 Summary query (no payload — fast)

```sql
SELECT canonical_id,
       version,
       updated_by,
       updated_at::date AS last_updated,
       payload->'confidence'->>'maturity' AS maturity,
       payload->'navigation'->>'winning_page_url' AS winning_page_url,
       jsonb_array_length(coalesce(payload->'navigation'->'explored_links', '[]'::jsonb)) AS explored_count,
       jsonb_array_length(coalesce(payload->'navigation'->'availability_links', '[]'::jsonb)) AS avail_count
FROM scrape_profiles
WHERE canonical_id IN (<pids>)
ORDER BY maturity DESC NULLS LAST, canonical_id;
```

### 6.2 What to flag immediately

| Pattern in `winning_page_url` | What it means | Source bug |
|---|---|---|
| `*.execute-api.*.amazonaws.com` | Lambda backend got persisted as the unit-data URL | `_is_infra_api_url` filter missing `execute-api` pattern |
| `*.theconversioncloud.com`, `*.omappapi.com`, `*.matterport.com`, `*.nestiolistings.com`, `*.supabase.co`, `*.hereapi.com`, `*.firebaseio.com` | Third-party tracker/CMS API; widely observed | `_is_infra_api_url` blocks these — but old profiles still have them. Cold-profile retry recovers (§10). |
| `.../?id=X/floorplans` (corrupted query+path) | The 2026-05-14 URL composition bug captured this as wpu | Cold-profile retry (§10) is the only recovery |
| `winning_page_url == entry_url` (after `_normalize_url`) | Self-fetch — homepage was incorrectly tagged as the unit page | Fix G (self-fetch suppression, 2026-05-15) — wpu no longer injected as candidate |

`explored_count > 10` typically means accumulated empty-extraction URLs poisoning the skip-list. The 2026-05-15 read-side carve-out lets high-score URLs (PMS priors, anchor-discovered links) bypass `explored_skip`. The writer-side fix only persists URLs when fetch_outcome != OK.

### 6.3 Export profile_json for canary seeding

```sql
\copy (
  SELECT canonical_id,
         json_build_object(
           'version',        version,
           'schema_version', schema_version,
           'created_at',     to_char(created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
           'updated_at',     to_char(updated_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
           'updated_by',     updated_by,
           'payload',        payload
         ) AS profile_json
  FROM scrape_profiles
  WHERE canonical_id IN (<pids>)
) TO 'C:/tmp/canary_profiles_<date>.csv' WITH CSV HEADER;
```

Seed with [C:/tmp/seed_canary_profiles.py](C:/tmp/seed_canary_profiles.py) which upserts into local proppy without nuking existing stubs. The local canary then auto-detects `DATA_PROVIDER=postgres` and reads them.

---

## Phase 7 — Cluster-diff investigation (when N PIDs share a failure)

When a bucket has 30+ failures and you can't fix all of them at once: pick 2 IMPROVED + 2 UNCHANGED_FAIL from the same `terminal_tier`. Diff their attributes:

```python
# Quick cluster trace — pick PIDs from successes.csv (IMPROVED) + failures.csv (UNCHANGED_FAIL)
# in the same terminal_tier bucket, then diff:
import json
from pathlib import Path
ev = Path(r'C:\tmp\run-<date>\events.jsonl')  # or canary events
for pid in IMPROVED_PIDS + FAILED_PIDS:
    print(f'=== {pid} ===')
    for line in ev.read_text(encoding='utf-8').splitlines():
        e = json.loads(line)
        if str(e.get('property_id') or '') != pid: continue
        if e.get('kind') == 'extract.link_hop_started':
            for c in e.get('candidates', []):
                print(f'  score={c.get("score"):>6} {c.get("anchor","")[:30]:<30} {c.get("url","")[:80]}')
        if e.get('kind') == 'extract.html_characterized':
            print(f'  signals: fp={e.get("floor_plan_signal_count")} jsonld={e.get("jsonld_types")[:3]} script_count={e.get("script_count")}')
```

The **first different attribute between IMPROVED and UNCHANGED_FAIL** is almost always the gap.

Example (2026-05-15 AppFolio investigation):
- 77734 (SUCCESS): candidate list contained `appfolio.com/connect?a=cw` AND `/vacancies` at score 5100 → walker found the AppFolio iframe path.
- 219388 (FAILED): candidate list had only `/our-communities/*` siblings and 404-ing PMS priors. The `franklin.appfolio.com/listings` iframe in the HTML was never queued.
- Diff revealed: `_PORTAL_URL_PATTERNS` only matched `apartments.appfolio.com` and `widgets.appfolio.com`, missing `{slug}.appfolio.com/listings`. One-line fix.

---

## Phase 8 — Extraction-gap reference

The 8 extraction gaps observed in the last 3 days, with verified fixes (each links to the actual landed code).

### 8.1 Embedded-JSON walker — for SSR inventory blobs (Razz, Wix Studio, custom CMS)

**Signal:** `body_bytes >> text_bytes` (ratio > 20). Page is 99% inline state.
**Cause:** CMS serialises inventory into a `<script type="application/json">` block at a vendor-specific key path. `extract_embedded_blobs_from_html` finds the block but `_find_list` is a 2-level walker — vendor paths are 4+ deep.
**Fix:** `find_unit_arrays(blob, min_signals=2)` in [pms/adapters/_api_parser.py](ma_poc/pms/adapters/_api_parser.py) (shipped 2026-05-15). Recursive DFS; finds any list whose items have ≥2 canonical unit-signal keys (after `normalize_field_key` vendor-variant collapse). Picks the longest list (avoids 5-row plan-summary picks beating a 678-unit inventory).
**Tested vendor key paths:**

| Vendor | Path to unit array |
|---|---|
| Razz / MyRazz | `requestedScreen → screenStoreState → initialStoreState → $inventory → units` |
| Next.js | `__NEXT_DATA__.props.pageProps.*` (any list) |
| Nuxt | `__NUXT__.data[0].*` |
| Wix Studio | `viewModel.* → items[]` |

**Size cap:** raised 1 MB → 4 MB in [_html_extract.py:719-728](ma_poc/pms/adapters/_html_extract.py#L719-L728) on 2026-05-15 because the Razz blob is 1.02 MB.

### 8.2 Portal URL 3-pass scan — iframe + anchor + quoted-URL

**Signal:** Live HTML grep shows `sightmap.com/embed/`, `*.appfolio.com/listings`, `*.onlineleasing.realpage.com` but the URL is NOT in `extract.link_hop_started.candidates`.
**Cause:** Pre-2026-05-15 `_extract_portal_iframe_hints` only scanned `<iframe src>`. Real-world portal URLs appear in 3 places:
1. `<iframe src="...">` (AppFolio embed)
2. `<a href="...">` (OneSite "Apply Now" CTA)
3. `"yardi_apply_now_link":"https://9026050.onlineleasing.realpage.com/..."` inside inline JS (mark-taylor.com)

**Fix:** 3-pass scan in [pms/scraper.py:_extract_portal_iframe_hints](ma_poc/pms/scraper.py) — iframe → anchor → quoted-URL-anywhere. Add new portal hosts to `_PORTAL_URL_PATTERNS` at [pms/adapters/_html_extract.py:891-913](ma_poc/pms/adapters/_html_extract.py#L891-L913).

**Currently recognised portal patterns:**
```
sightmap.com/embed/, embed.engrain.com         → sightmap
onlineleasing.realpage.com, myleasingoffice.com → realpage_oll
rentcafe.com/apartments/, rentcafe.com/onlineleasing → rentcafe
funnelleasing.com/embed                         → funnel
apartments.appfolio.com, widgets.appfolio.com,
.appfolio.com/listings, .appfolio.com/connect  → appfolio
myresman.com/portal/*, resman.com/portal/      → resman
.yardi.com                                     → yardi
```

### 8.3 JSON-LD vendor-key fuzzy match

**Signal:** `extract.html_characterized.jsonld_types` includes `ApartmentComplex` but `generic:jsonld -> ran_empty: "no Apartment/Offer schema in HTML"`.
**Cause:** `_jsonld_item_has_unit_signal` checked Schema.org canonical keys (`offers.price`, `numberOfRooms`, `floorSize`) but vendor data uses camelCase variants (`monthlyRent`, `numberOfBedrooms`, `bedroomCount`, `squareFootage`). Added a second-pass check that normalises each key via `FIELD_ALIASES`.
**Fix:** [pms/adapters/_api_parser.py:_jsonld_item_has_unit_signal](ma_poc/pms/adapters/_api_parser.py) (2026-05-15).
**Added alias:** `floorsize → sqft` for Schema.org `floorSize` after lowercase normalisation.

### 8.4 RentCafe content-type gate

**Signal:** `terminal_tier=TIER_1_API_RENTCAFE_SHAPE_REJECTED` with `fetch_outcome=OK` and entry-page body is the property's own marketing site (not a securecafe portal).
**Cause:** `ctx._api_responses` includes EVERY response with content-type containing `json|xml|html|text` — so the HTML page itself made `api_responses` non-empty, and the SHAPE_REJECTED classifier fired even when zero rentcafe-host JSON XHRs were captured.
**Fix:** [pms/adapters/rentcafe.py:_classify_rentcafe_failure](ma_poc/pms/adapters/rentcafe.py) (2026-05-15). A response only counts as a RentCafe candidate when **content-type ∈ json AND host ∈ {rentcafe.com, securecafe.com, yardi.com}**. Third-party trackers (callrail, gtm, osano) no longer trigger SHAPE_REJECTED.

### 8.5 Subpath URL composition

**Signal:** Many hops to URLs of shape `?id=XXX/floorplans`, `?id=XXX/pricing`, `?id=XXX/floor-plans` — the SPA returns 200 with the same homepage shell every time.
**Cause:** `_psp_url = sub_url.rstrip("/") + _psp` — naive concatenation when `sub_url` contains a query string produces a path-segment after the query.
**Fix:** [pms/scraper.py:1958-1972](ma_poc/pms/scraper.py#L1958-L1972) — `urlparse` + `urlunparse` composition that preserves the query string. Stops the spiral.

### 8.6 Stripped-text dedup hash

**Signal:** SPA tab-switcher pages (`#floor-plans`, `#pricing`, `#amenities`) all return distinct raw bytes (timestamps, CSRF, chunk URLs differ) but the same rendered text.
**Cause:** Body-hash dedup was SHA256 of raw bytes. SPA pages had 26 KB of inline-state variance defeating the hash.
**Fix:** [pms/scraper.py:1094-1117](ma_poc/pms/scraper.py#L1094-L1117) — `_strip_html_for_dedup` removes script/style/tags, collapses whitespace, hash the result.

### 8.7 SightMap-vs-Entrata detector tiebreaker

**Signal:** PMS detected as `entrata` but unit data lives in a SightMap iframe on the same page (chaseknollsapts.com — 2026-05-15).
**Cause:** Detector Pass 1 STRONG marker (`commoncf.entrata.com`) beat SightMap (Pass 3 WEAK). Entrata adapter ran, found nothing.
**Fix:** [pms/detector.py:365-394](ma_poc/pms/detector.py#L365-L394) — when BOTH `sightmap.com/embed/` AND any Entrata STRONG marker are present, route to `sightmap` at 0.90.

### 8.8 FP-signal gate on LLM_DOM

**Signal:** `generic:llm_dom_targeted ran_empty` on pages with `floor_plan_signal_count == 0` (SPA shells, marketing pages with no inventory).
**Cause:** `_extract_rent_dom_section` falls back to `body[:cap]` when no structural container exists, so the LLM gets a non-empty input. Burns ~$0.01 per property × thousands of properties.
**Fix:** [pms/adapters/generic.py:2356-2375](ma_poc/pms/adapters/generic.py#L2356-L2375) — `if html and _has_fp_signals(html, SIGNAL_THRESHOLD_ANY)` gate before invoking the DOM-LLM. Emits visible `skipped, reason="no floor-plan signals in body — skipping LLM DOM"` event.

### 8.9 Redirect-aware `landed_url` for cross-domain anchor admit (2026-05-15)

**Signal:** Entry URL is `https://X.com/`; after fetch `final_url` lands on `https://Y.com/properties/X/`. Page has anchors pointing at `Y.com/properties/X/floorplans/` ("View Availability" link) and `Y.com/properties/X/find-apartments/`. None of those make it into the `extract.link_hop_started.candidates` list — only Y-host PMS-priors at score 5095 appear. PIDs 53592 (1701arch.com → livethearch.com) and 119144 (windsorburnet.com → windsorcommunities.com) are canonical.

**Cause:** `_rank_internal_links` at [pms/scraper.py:1487-1495](ma_poc/pms/scraper.py#L1487-L1495) had a `is_same_site = link_host == base_host …` check. With `base_host = "windsorburnet.com"` and `link_host = "windsorcommunities.com"`, the check fails and the anchor is dropped. Even relative anchors like `href="floorplans/"` resolve against `base_url` (CSV's pre-redirect host), so the resolved URL lands on the wrong host. PMS-prior synthesis has the same bug.

**Fix:** [pms/scraper.py:1401-1556](ma_poc/pms/scraper.py#L1401-L1556) — `_rank_internal_links` now takes an optional `landed_url` parameter. Accepts BOTH `base_host` and `landed_host` as same-site. Uses `landed_url` as the `urljoin` base when entry redirected to a different host. [pms/scraper.py:2790+](ma_poc/pms/scraper.py#L2790) — caller extracts `fetch_result.final_url` and passes as `landed_url` to `_try_link_hop`, which propagates into the ranker AND uses it as the PMS-prior synthesis base when present.

### 8.10 fp_signal listing-structure gate (STUB_AGGREGATE_COPY) (2026-05-15)

**Signal:** `floor_plan_signal_count >= 2` triggers extraction tiers but every tier returns 0 units. Live-grep of the page shows the patterns came from marketing copy ("Choose from our 1, 2, or 3-bedroom apartments") + a `LocalBusiness` JSON-LD `priceRange: "$1490 - $3824"` aggregate + amenity descriptions ("793 square feet fitness center"). NO actual per-unit table or Offer array exists. PID 119144 (windsorburnet.com) is canonical.

**Cause:** `count_floor_plan_signals` matches raw text patterns. It can't distinguish a unit listing from marketing aggregate copy that happens to mention bedrooms/baths/sqft.

**Fix:** new `count_listing_structural_signals(html)` + `has_listing_structure(html)` in [pms/signal_engine/floor_plan_signals.py:391-490](ma_poc/pms/signal_engine/floor_plan_signals.py#L391-L490) that checks for:
- ≥2 `<tr>` rows containing a rent shape ($NNN) — covers Entrata/RentCafe/RealPage pricing tables
- ≥2 DOM nodes with unit-card / floorplan-card / listing-card class markers
- ≥2 JSON-LD `@type: Apartment|FloorPlan|Offer|Product` declarations
- ≥2 JSON-LD `PropertyValue` entries naming a dimension (covers Squarespace `Product + additionalProperty`)

Gate is plumbed into the LLM_DOM tier at [pms/adapters/generic.py:2367-2389](ma_poc/pms/adapters/generic.py#L2367-L2389) — when fp_signals fire but `has_listing_structure == False`, the LLM_DOM call is skipped with reason `stub_aggregate_copy — fp_signals present but no listing structure`. Saves ~$0.005/property × N marketing-aggregate sites.

### 8.11 JSON-LD `Product + additionalProperty: [PropertyValue]` (Squarespace e-commerce) (2026-05-15)

**Signal:** `extract.tier_attempted generic:jsonld ran_empty "no Apartment/Offer schema in HTML"` despite the page having structured product data. JSON-LD `@graph` contains multiple sibling `Product` nodes each with `category: "Apartment Floor Plan"`, a SINGLE `offers` dict (not a list), and `additionalProperty: [{name: "Bedrooms", value: 1}, {name: "Bathrooms", value: 1}, {name: "Square Footage", value: 830}]`. The original parser only knew `numberOfBedrooms` / `numberOfBathroomsTotal` / `floorSize` as direct keys. PID 61950 (250high.com) is canonical — 5 floor plans, $1,723-$2,447.

**Cause:** Pass 2 `_extract_offers_as_units` requires `offers: [...]` with len ≥ 2; the per-Product single-offer dict fails the check. Pass 3 `_extract_standalone_offers` walks bare Offers nested inside AggregateOffer dicts but loses the parent Product context, so `additionalProperty` is never read.

**Fix:** new `_read_additional_property(item)` helper + new `_extract_product_floorplans_as_units` Pass 4 at [pms/adapters/_html_extract.py:445-525](ma_poc/pms/adapters/_html_extract.py#L445-L525). Pass 4 collects Products with `apartment` / `floor plan` / `rental` category hints, wraps each Product's single `offers` dict + sets `itemOffered = product`, then calls `_build_unit_from_offer`. The unit builder at [_html_extract.py:232-310](ma_poc/pms/adapters/_html_extract.py#L232-L310) reads `additionalProperty` and fills missing `bedrooms` / `bathrooms` / `sqft` from PropertyValue entries. **Pass 4 must run BEFORE Pass 3** otherwise the bare-Offer walker eats the nested per-Product offers with no parent context.

### 8.12 Generic inline-JS PMS init parser (JS-injected iframes) (2026-05-15)

**Signal:** Live HTML grep finds `SightMap.init({sightmap_id: "..."})` or `<div data-sightmap-id="...">` or fortresstech UUID embedded in inline JS, but NO `<iframe src="sightmap.com/embed/...">` in static HTML. The 3-pass iframe / anchor / quoted-URL scan returns empty for these portals because the iframe is JS-injected at runtime.

**Cause:** SightMap (and several other portals) ship a script loader (`sightmap.com/embed/api.js`) that hydrates the iframe at runtime. The static HTML the L1 fetcher saved BEFORE hydration only contains the loader tag. PID 20959 dovevalleyapts.com is canonical.

**Fix:** new `_scan_inline_js_pms_init(html)` 4th pass in `_extract_portal_iframe_hints` at [pms/scraper.py:1218-1330](ma_poc/pms/scraper.py#L1218-L1330). Pattern library (`_INLINE_JS_INIT_PATTERNS`) covers SightMap, AppFolio `data-tenant`, RealPage OLL `clientId`, FortressTech UUID, Hyly `propertyId`, FunnelLeasing GUID. Each pattern captures the property-specific key; `url_synth_fn(key)` returns the canonical data URL.

### 8.13 AppFolio slug-to-listings synthesis (2026-05-15)

**Signal:** Marketing site embeds `{slug}.appfolio.com/connect/users/sign_in` iframes (Pay-Rent / Tenant-Portal CTAs) but NO `/listings` iframe. After RC #3b removed `.appfolio.com/connect` from `_PORTAL_URL_PATTERNS`, no AppFolio URL gets queued at all → all hops are host-root 404s. PIDs 259733 (ekoliving.life), 11399 (ironridge-capital.com), 50178 (leescrossingapartments.net) canonical.

**Cause:** The fix removed the wrong-target match but didn't add affirmative listings-URL discovery.

**Fix:** 5th pass in `_extract_portal_iframe_hints` at [pms/scraper.py:1232-1262](ma_poc/pms/scraper.py#L1232-L1262). Uses [`_APPFOLIO_SUBDOMAIN_RE`](ma_poc/pms/detector.py#L173) to detect the CMS slug from any `{slug}.appfolio.com` occurrence in HTML; when no `/listings` URL is already queued, prepends `https://{slug}.appfolio.com/listings` at portal score 10000. AppFolio adapter's existing 4 extraction modes (XHR capture, SSR DOM parse, /detail page, offboarded-tenant detection) handle the rest. PID 259733 ekoliving.life recovered 122 units this way.

### 8.14 Vendor-iframe widgets — Wix Visual Data + Cross Street + Fortress (2026-05-15)

**Signal:** PIDs 46179 hayloft / 118965 16bennett / 292955 3140clybourn / 1713 brooklaneapts. Static HTML / urllib fetch shows no inventory text. Live Playwright fetch enumerates `page.frames` and finds a cross-origin frame holding the actual data — host varies per vendor:
- `wix-visual-data.appspot.com/index?pageId={page}&compId={comp}&siteRevision={n}` — Wix Visual Data widget; AngularJS shell renders a Wix Collection as a table
- `yourcrossstreet.com/property/{slug}/?floorplan={id}` — Cross Street leasing widget; React SPA  
- `embed.fortresstech.io/unit-availability/{guid}` + `portal.fortresstech.io/{guid}/` — Funnel/FortressTech React SPA

**Fix:** added to `_PORTAL_URL_PATTERNS` at [pms/adapters/_html_extract.py:891-1135](ma_poc/pms/adapters/_html_extract.py#L891-L1135) + late-render whitelist at [fetch/fetcher.py:866-895](ma_poc/fetch/fetcher.py#L866-L895). 292955 (yourcrossstreet) recovered. 46179 / 118965 still fail because the Wix Visual Data iframe URL is JS-injected with query params that come from the Wix component config — static HTML scan only sees the BASE URL without `pageId` / `compId`. URL synthesis from `wix-viewer-model` blob is deferred to a follow-up.

### 8.15 Open-by-default unknown-portal discovery + telemetry (2026-05-15)

**Signal:** A new vendor's iframe widget hosts inventory; every property on that vendor fails until someone hand-edits `_PORTAL_URL_PATTERNS`. No runtime learning.

**Fix:** added 6th pass in `_extract_portal_iframe_hints` at [pms/scraper.py:1264-1303](ma_poc/pms/scraper.py#L1264-L1303). Walks every `<iframe src>` in entry HTML; skips known portals (already queued), same-origin URLs, and any host on `_PORTAL_INFRA_BLACKLIST` ([pms/adapters/_html_extract.py:1135-1240](ma_poc/pms/adapters/_html_extract.py#L1135-L1240) — analytics, maps, chat, social, parastorage CDN, Wix internal services). Remaining hosts are queued at `_UNKNOWN_PORTAL_SCORE = 9_000` with anchor `unknown:{host}`, capped at 3 per property.

Each unknown emits `embedded_portal.unknown_host_seen` event ([observability/events.py](ma_poc/observability/events.py)) so cross-run aggregation can identify trending vendors. Full design doc at [docs/generic_portal_discovery.md](ma_poc/docs/generic_portal_discovery.md). Phase 3 (profile-persisted learned hosts) + Phase 4 (cross-run promotion to `_PORTAL_URL_PATTERNS`) are planned but not yet shipped.

### 8.16 Decider rule conflation — Rule 2 vs Rule 4 both return HOP_TO_URL (2026-05-15)

**Signal:** `extract.tier_attempted reason=rc3_defer_monolithic_to_hop` fires on a hop page (`hop_index ≥ 1`) even AFTER the 2026-05-15 `hop_depth` wiring fix landed in production. 53.7% of FAILED_NO_DATA had this — meaning the monolithic LLM was suppressed on every hop, never running anywhere.

**Cause:** the `ActionDecider` ([pms/signal_engine/decider.py](ma_poc/pms/signal_engine/decider.py)) has TWO rules that return `ActionType.HOP_TO_URL`:
- Rule 2 (RC3 deferral) — rationale `dom_analysis_defer_monolithic_to_hop`, gated on `hop_depth == 0`
- Rule 4 (top-signal dispatch via `_map_to_action`) — rationale `top_signal:{...}`, NOT gated; returns HOP_TO_URL for LLM_HINT / PROFILE_WINNING / PMS_PRIOR signals

The caller in [pms/adapters/generic.py:2655](ma_poc/pms/adapters/generic.py#L2655) only checked `action_type == HOP_TO_URL` and set `_rc3_deferred = True`. On hop pages Rule 2 correctly skipped (hop_depth=1), but Rule 4 still returned HOP_TO_URL because the LLM_HINT signal was in the ranker → caller mislabeled it as rc3_defer and suppressed the LLM.

**Fix:** [pms/adapters/generic.py:2655-2670](ma_poc/pms/adapters/generic.py#L2655-L2670) — check rationale, not just action_type: `if _decision.action_type == HOP_TO_URL and _decision.rationale == "dom_analysis_defer_monolithic_to_hop"`. **Architectural lesson** (now in §10): when reading an `ActionDecider` decision, the action_type alone is ambiguous — read the rationale for which rule fired.

### 8.17 Wix vendor-key fuzzy normalization (2026-05-15)

**Signal:** SSR JSON blobs contain unit-shaped items (e.g. `{numBedrooms: 1, numBathrooms: 1, floorSize: 750, priceText: "$1,450"}`), but `find_unit_arrays` walker reports `0 SSR blob(s) had no unit signals`. `_item_has_unit_signals` runs each key through `normalize_field_key`, but Wix camelCase variants weren't in the alias table.

**Cause:** `FIELD_ALIASES` had `bedroomcount` / `bedroom_count` / `numberofbedrooms` but NOT `numbedrooms` / `bedroomsnumber` / `pricetext` (Wix wix-data collection vendor spellings).

**Fix:** [pms/signal_engine/floor_plan_signals.py:151-280](ma_poc/pms/signal_engine/floor_plan_signals.py#L151-L280) — added exact aliases for the observed Wix keys, AND added a `_fuzzy_normalize` helper with prefix patterns (`num<X>` / `number<X>` / `total<X>`) and suffix patterns (`<X>Count` / `<X>Number` / `<X>Total`) that maps to canonical when the stem is in `_FUZZY_STEM_CANONICAL`. Critically: `_PROTECTED_CANONICAL_KEYS` guards `min_rent` / `max_rent` etc. so the `min<X>` / `max<X>` semantic prefixes survive normalisation.

---

## Phase 9 — Profile-state and recovery mechanisms

Two distinct mechanisms to recover from poisoned WARM/HOT profiles:

### 9.1 Self-fetch suppression (Fix G, 2026-05-15)

**Trigger:** `profile.navigation.winning_page_url == entry_url` (path-normalised match — handles trailing-slash + http/https variants).
**Effect:** `winning_page_url` is NOT injected as a hop candidate. Real anchor-discovered links (`/Marketing/FloorPlans` etc.) take the top slot.
**Why safe:** Entry-page extraction runs BEFORE `_try_link_hop`. Adding the entry URL as a hop just re-fetches the same body. LLM budgets are per-property; entry already consumed them.
**Code:** [pms/scraper.py:1520-1561](ma_poc/pms/scraper.py#L1520-L1561).

### 9.2 Cold-profile retry (Fix I, 2026-05-15)

**Trigger:** First `scrape_jugnu` returned 0 units AND profile maturity is WARM/HOT.
**Effect:** Second `scrape_jugnu` call with `force_cold=True`. The function clones the profile in-memory and clears `winning_page_url`, `availability_links`, `explored_links`, `dead_links`, `dom_hints.field_selectors`. Persisted profile is NOT mutated. One retry per property max.
**Why useful:** PMS providers change without notice; a property can migrate from RentCafe to Entrata between runs. The stale wpu / cached selectors actively misdirect the scraper.
**Code:** [scripts/runners/jugnu.py:687-727](ma_poc/scripts/runners/jugnu.py#L687-L727), [pms/scraper.py:2402-2459](ma_poc/pms/scraper.py#L2402-L2459).

### 9.3 LLM_DOM retry-on-empty when prior winner

**Trigger:** `generic:llm_dom_targeted` returned 0 AND `profile.confidence.last_success_tier == 4`.
**Effect:** Second LLM-DOM call with a `_retry_hint=prior_llm_dom_win_empty_today` in property_context. Mitigates non-determinism on OpenRouter when same HTML yields different completions across runs.
**Code:** [pms/adapters/generic.py:2407-2452](ma_poc/pms/adapters/generic.py#L2407-L2452).

---

## Phase 10 — Architecture invariants you must verify when changing gates

A gate that depends on a missing context field is silently always-True (or always-False). Every gate change must verify the data flow from upstream.

### 10.1 `AdapterContext` field inventory

The fields every gate may read — and the upstream sites that populate them:

| Field | Set at | Read by | What goes wrong if missing |
|---|---|---|---|
| `hop_depth` | `pms/scraper.py` `scrape()` from kwarg; recursive `scrape()` call from `_try_link_hop` passes `hop_depth=1` | `decider.py:137` (RC3 monolithic deferral), `generic.py:2644` | RC3 defers LLM on every hop — entire LLM budget wasted. (The 2026-05-13 fix added the gate but missed the upstream wiring. Fixed 2026-05-15.) |
| `floor_plan_signal_count` | `pms/scraper.py:599` from `extract.html_characterized` | `decider.py:649-652` (RC3 suppression on content-rich entry) | RC3 always defers even when entry has real unit data |
| `adapter_unit_count` | `pms/scraper.py` after PMS-specific adapter run | `generic.py` LLM skip-gate | LLM stays skipped even after Entrata/RentCafe returned 0 |
| `_api_responses` | `pms/scraper.py` from network_log | Every adapter probe + LLM_API_RESCUE | Adapter sees zero responses; SHAPE_REJECTED label leaks |
| `profile` | `pms/scraper.py:587` | DOM-hints replay, LLM_DOM retry, cold-retry trigger | Replay / retry mechanisms inert |

### 10.2 Verification rule

When adding a new gate that reads `ctx.X`:
1. `grep -n "X\s*=\|X:" pms/adapters/base.py` — confirm the field exists.
2. `grep -n "AdapterContext(" pms/scraper.py` — confirm every construction site sets it.
3. Write a unit test that exercises the gate via a real `AdapterContext` instance, not a Mock — Mocks silently grant any `getattr()`.

### 10.3 Profile-dependent gates

Same rule: every gate that reads `profile.X` must have `X` populated by `profile_updater.py`. Common slots:

| `profile.X` | Set by | Read by |
|---|---|---|
| `navigation.winning_page_url` | `profile_updater.py:_update_winning_url` after SUCCESS | `pms/scraper.py:_try_link_hop` profile_top builder |
| `navigation.explored_links` | `profile_updater.py:record_explored_link(had_data=False)` | `pms/scraper.py:_try_link_hop` explored_skip builder |
| `confidence.last_success_tier` | `profile_updater.py:_update_confidence` on SUCCESS | `generic.py:2407` LLM_DOM retry gate |
| `dom_hints.field_selectors` + `.field_selectors_quality` | `profile_updater.py` after LLM_DOM success | `generic.py:1818-1856` DOM-cascade replay |
| `confidence.maturity` (COLD/WARM/HOT) | `profile_updater.py:_promote_or_demote` | `services/source_planner.py:compute_budget`, runner cold-retry trigger |

---

## Phase 11 — STUB_URL classifier (genuine no-data with high confidence)

Some properties have a perfectly-loaded entry page with ZERO unit data — they're marketing wrappers. Distinguishing these from "extraction missed data that exists" lets us route data-quality issues to the CSV maintainer instead of into the extraction-bug triage queue.

**Verdict `STUB_URL` (high-confidence genuine empty) fires when, aggregated across entry + every hopped sub-page, ALL of these are zero/empty:**

| Signal | Source |
|---|---|
| Inline rent markers (`$NNN`, "price", "rent") in visible text | live fetch + regex |
| Floor-plan structural signals (bed/bath/sqft/studio) | `floor_plan_signal_count` |
| JSON-LD `Apartment`/`FloorPlan`/`Offer` nodes | `jsonld_types` |
| Unit-shaped arrays in `<script type="application/json">` | walker in §8.1 returned empty |
| Portal URLs anywhere in HTML (iframe/anchor/quoted) | 3-pass scan in §8.2 |
| Captured API JSON responses with ≥2 unit-signal keys | `_api_responses` count |
| Internal anchors matching floor-plan keywords that loaded OK | `extract.link_hop_fetched` outcomes |
| **Cross-origin iframe frames** (Playwright `page.frames`) carrying unit-shaped innerText | Playwright frame enumeration |
| **Listing structure** (`<tr>` + rent, unit-card classes, Offer/Product arrays, PropertyValue dims) | `count_listing_structural_signals(html)` ≥ 1 |

**Critical precondition before declaring STUB:** ENUMERATE FRAMES. The 2026-05-15 false-STUB call on 46179 / 118965 / 292955 happened because I only checked top-level `document.body.innerText`. Each had real inventory inside a cross-origin iframe (`wix-visual-data.appspot.com`, `yourcrossstreet.com/property/`). Static HTML grep would have missed it too — Wix injects the iframe post-hydration. Per §14 *Frame enumeration snippet*: open with Playwright, scroll, then iterate `page.frames` and read each frame's `document.body.innerText`. ONLY declare STUB when EVERY frame returns marketing-only text.

**Two distinct STUB classes:**
- **STUB_URL** — no rent / bed / sqft anywhere in HTML or any frame. Truly no data.
- **STUB_AGGREGATE_COPY** — `fp_signals ≥ 2` BUT `has_listing_structure(html) == 0`. The signals come from marketing copy ("Choose from 1, 2, 3-bedroom") + a LocalBusiness JSON-LD `priceRange` aggregate. PID 119144 windsorburnet.com canonical. Use `has_listing_structure` from [pms/signal_engine/floor_plan_signals.py](ma_poc/pms/signal_engine/floor_plan_signals.py) to discriminate.

**Examples confirmed STUB (2026-05-15):**
- PID 267183 thesiennaapartments.com — 3 application/json blocks, all are Squarespace form configs (`formFields`, `submissionTextAlignment`); 0 `$NNN`, 0 bed, 0 sqft. `/floorplans` and `/floor-plans` both 404.

**False-STUB war stories (don't repeat these):**
- 2026-05-15: 46179 hayloftapartmenthomes.com declared STUB after `body.innerText = 1442 chars` marketing-only check. Reality: `wix-visual-data.appspot.com/index?pageId=q0o07&compId=ja77je2z` iframe held 5 floor plans + rent + sqft. User correction caught this.
- 2026-05-15: 118965 16bennett.com same pattern — wix-visual-data iframe had 30+ per-unit table rows.
- 2026-05-15: 292955 3140clybourn.com same pattern — `yourcrossstreet.com/property/the-clybourn-2/?floorplan=4` iframe had 6 floor plans.

**Status:** STUB_URL / STUB_AGGREGATE_COPY classifier not yet emitted as a distinct verdict. The `has_listing_structure` gate is in place at the LLM_DOM tier (cost savings) but properties still emit `FAILED_NO_DATA` externally. Verdict-level classification (separate from FAILED_NO_DATA in metrics) is P3 work — requires changes to `reporting/verdict.py`, `slo_watcher.py`, `analyze_cloud_run.py`, frontend schema, DB.

---

## Phase 12 — Local canary workflow (proven 3-day cadence)

### 12.1 Build manifest from yesterday's failures

```python
# Sample 3-4 PIDs per terminal_tier bucket + force-include any PIDs you specifically diagnosed
# + 4 sentinels with varied PMS types (8-80 units, varied domains)
```

Reference implementation patterns shipped in [data/canary/local_runs/](ma_poc/data/canary/local_runs/) folder names. Key flags for the canary tool:
- `--regression-basket-size 0` — don't auto-select sentinels (provided in manifest)
- `--keep` — preserve sqlite DB for forensics
- `--timeout-per-property 240` — accommodates LLM-DOM retries
- `--from-run YYYY-MM-DD` — yesterday's run for the diff baseline

### 12.2 Cold vs profile-seeded — always run both

| Run type | What it exercises | What it MISSES |
|---|---|---|
| Cold (default) | Adapter extraction, embedded JSON walker, portal scan, hop_depth, FP-signal gates | Profile-dependent fixes: Fix G self-fetch, B3 carve-out, LLM_DOM retry, cold-retry trigger |
| With prod profiles seeded (§6.3 + seed_canary_profiles.py) | Everything above PLUS profile-dependent fixes | Real Cloudflare bypass (still local-direct fetch) |

### 12.3 Cloud SQL proxy gotchas

```bash
# Port 5433 often in use after a crashed session — use 5434 fallback
"C:\Users\ashus\bin\cloud-sql-proxy.exe" --port 5434 jugnu-494013:us-central1:jugnu-db-production

# If "409 invalidState" — Cloud SQL instance is under maintenance.
# Either wait or have a teammate run the export query through gcloud sql connect / Cloud SQL Studio.
```

### 12.4 Output paths under SCHEMA_VERSION=v2

```
data/canary/local_runs/<out-dir>/
├── canary_input.csv                   ← manifest
├── canary.sqlite                      ← canary DB (--keep)
├── report.md                          ← summary + per-property delta table
├── jugnu.log                          ← full runner log
└── v2/runs/<run-date>/                ← actual jugnu output (note v2/ prefix!)
    ├── events.jsonl
    ├── properties.json
    └── property_reports/{pid}.md
```

The canary tool reads `events.jsonl` from `v2/runs/<date>/` when `SCHEMA_VERSION=v2`. If it reports "TIMEOUT × N" for every property, it's looking at the wrong path — verify both `runs/` and `v2/runs/`.

### 12.5 Verdict interpretation

| Outcome | What it means | Deploy gate |
|---|---|---|
| IMPROVED (failure → success) | Fix worked | Counts positive |
| UNCHANGED_OK (sentinel still passes) | No regression | Required |
| UNCHANGED_FAIL (failure didn't recover) | Fix doesn't cover this PID; classify residue per §3 Q1-Q9 | Acceptable |
| REGRESSED (sentinel now fails) | **STOP — deploy blocked** | Must be 0 |
| ENV_MISMATCH (CF/bot-block locally; works in cloud) | Not a code regression | Acceptable, document |

A pass rate of 40-50% IMPROVED on a representative bucket sample is good. 100% is unrealistic — some properties are genuinely STUB_URL or behind anti-bot infra.

---

## Phase 13 — Agentic investigation patterns

When to dispatch parallel agents vs do it yourself.

### Dispatch agents when…

- You need to read 6+ files of code for one investigation (offload context)
- You're running 5+ live fetches against 5+ different sites
- Multiple independent hypotheses need triangulation

### Do it yourself when…

- Single PID deep-dive on events.jsonl (faster in your own bash)
- Code change that needs 3-5 file reads (faster in your own session)
- Anything requiring Bash permissions that may be denied (agents get blocked on `python -c "..."` calls; you don't)

### Briefing template that works

Every agent prompt that produced useful results in the past 3 days included:
1. **The specific question** (verbatim from user when possible)
2. **Concrete inputs** — exact file paths, exact PIDs, exact event types
3. **What I've already ruled out** — prevents the agent re-walking dead ends
4. **Required output format** — "Quote events verbatim; cite file:line; no speculation."
5. **A length cap** — "under 600 words"

Bad briefing example (produces shallow output):
> "Investigate why AppFolio properties are failing"

Good briefing example (produces deep output):
> "PID 219388 liveatfranklin.com terminal=TIER_1_API_APPFOLIO. Events at C:\tmp\run-2026-05-14\shard_70\events.jsonl. Find: (1) what URLs were in the candidate list, (2) what URLs appeared in <iframe src> on the entry HTML, (3) whether _PORTAL_URL_PATTERNS matches any of them. Output: per-PID table with quoted event excerpts. Under 400 words."

### Anti-pattern: trusting the agent's synthesis

Agents are excellent at reading and triangulating but can confabulate. ALWAYS:
- Verify a quoted file:line by reading that line yourself
- Verify a quoted URL by live-fetching it yourself
- Verify a quoted event by grepping events.jsonl yourself
- One inconsistency → re-verify everything in that section

---

## Phase 14 — Tool reference (commands cheat-sheet)

### Quick event trace for a PID

```python
import json
from pathlib import Path

base = Path(r'C:\tmp\run-2026-05-14')
pid = '290347'
# Find the right shard from failures.csv first, OR grep across all shards:
for shard_dir in sorted(base.iterdir()):
    ev = shard_dir / 'events.jsonl'
    if not ev.exists(): continue
    text = ev.read_text(encoding='utf-8', errors='ignore')
    if f'"property_id":"{pid}"' not in text and f'"property_id": "{pid}"' not in text:
        continue
    print(f'=== {pid} in {shard_dir.name} ===')
    for line in text.splitlines():
        try: e = json.loads(line)
        except: continue
        if str(e.get('property_id') or '') != pid: continue
        k = e.get('kind','')
        if k in ('fetch.completed','extract.tier_attempted','extract.link_hop_started','extract.link_hop_fetched','extract.html_characterized','output.property_emitted'):
            keep = {kk: str(vv)[:120] for kk, vv in e.items()
                    if kk in ('url','tier_key','outcome','reason','body_bytes','units','verdict','tier_used','hop_index','final_url','candidates','floor_plan_signal_count','jsonld_types','script_count','text_bytes')}
            print(f'  {k[:34]:<34}', keep)
    break
```

### Live page inspection

```python
import urllib.request, re, json
url = 'https://www.example.com/floorplans'
req = urllib.request.Request(url, headers={'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0'})
try:
    html = urllib.request.urlopen(req, timeout=30).read().decode('utf-8', 'replace')
except Exception as e:
    print('FAIL:', e); raise SystemExit
print(f'len={len(html)}')

# Application/json blocks
for i, m in enumerate(re.finditer(r'<script[^>]*type=["\']application/json["\'][^>]*>([\s\S]*?)</script>', html, re.IGNORECASE)):
    body = m.group(1).strip()
    try:
        d = json.loads(body)
        keys = list(d.keys())[:8] if isinstance(d, dict) else type(d).__name__
    except Exception:
        keys = '<parse-fail>'
    print(f'  [json block {i}] len={len(body)} top_keys={keys}')

# Portal URLs anywhere (iframe / anchor / quoted)
for pat, label in [
    (r'<iframe[^>]+src=["\']([^"\']+)["\']',  'iframe'),
    (r'<a[^>]+href=["\']([^"\']+)["\']',       'anchor'),
    (r'["\'](https?://[^"\'\\s<>]+)["\']',     'quoted'),
]:
    for m in re.finditer(pat, html, re.IGNORECASE):
        u = m.group(1).lower()
        for kw in ('sightmap.com/embed','onlineleasing.realpage','rentcafe.com','securecafe.com','.appfolio.com','myresman.com'):
            if kw in u:
                print(f'  {label:<7} {kw:<28} {m.group(1)[:90]}')
                break

# Rent / FP / unit pattern counts
print('  $NNN:',    len(re.findall(r'\$\s?\d{2,5}(?:,\d{3})?', html)))
print('  bed/br:',  len(re.findall(r'\b\d+\s?(?:br|bed|bedroom)', html, re.IGNORECASE)))
print('  sqft:',    len(re.findall(r'\b\d{2,5}\s?(?:sqft|sq\.?\s?ft|square\s?feet)', html, re.IGNORECASE)))
print('  studio:',  len(re.findall(r'\bstudio\b', html, re.IGNORECASE)))
```

### Frame enumeration — required before declaring STUB (2026-05-15)

The `urllib` live-fetch above only sees the static HTML. Many sites (Wix Visual Data, FortressTech, AppFolio iframes, SightMap JS-injected embeds) inject the data-bearing iframe AFTER hydration. Walk every Playwright frame before concluding "no data anywhere".

```python
import asyncio, re
from playwright.async_api import async_playwright

async def enumerate_data_frames(url: str) -> None:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0',
            viewport={'width': 1920, 'height': 1080},
        )
        page = await ctx.new_page()
        await page.goto(url, wait_until='domcontentloaded', timeout=45000)
        await asyncio.sleep(3)
        # Scroll slowly to trigger lazy / on-scroll iframe injection
        for y in range(0, 4500, 400):
            await page.evaluate(f'window.scrollTo(0, {y})')
            await asyncio.sleep(0.8)
        await asyncio.sleep(4)
        # Walk every frame
        for fr in page.frames:
            if not fr.url or 'about:blank' in fr.url or 'data:' in fr.url:
                continue
            # Skip CDN-only / asset frames
            if any(s in fr.url for s in (
                'parastorage.com', 'wixstatic.com', 'images.squarespace-cdn',
                'googleapis.com/maps', 'wix-instantsearchplus', 'filesusr.com',
            )):
                continue
            try:
                text = await fr.evaluate('document.body ? document.body.innerText : ""')
            except Exception:
                continue
            if len(text) < 50:
                continue
            # Unit-data heuristic — same logic as STUB classifier
            has_unit_data = bool(re.search(
                r'\d+\s*(?:bed|bath|sqft|sq\.?\s?ft|square)|\$\s?\d{3,5}',
                text, re.IGNORECASE
            ))
            if has_unit_data:
                print(f'*** FRAME WITH DATA: {fr.url[:200]}')
                print(f'    text first 800 chars: {text[:800]!r}')
            else:
                print(f'    frame (no data): {fr.url[:160]}')
        await browser.close()

# Example: confirm a "STUB" claim
# asyncio.run(enumerate_data_frames('https://www.16bennett.com/'))
```

When `*** FRAME WITH DATA` prints with `wix-visual-data.appspot.com/index?pageId=...` or `yourcrossstreet.com/property/...` or `embed.fortresstech.io/...`, the property is NOT a stub — the parent scraper just didn't hop into that iframe. Check `_PORTAL_URL_PATTERNS` + `_PORTAL_INFRA_BLACKLIST` semantics before recommending classification.

### Profile inspection via local proppy

```python
import pg8000.dbapi as pg
conn = pg.connect(host='localhost', port=5432, user='postgres', password='Ashu@007saxe', database='proppy')
cur = conn.cursor()
cur.execute("""
  SELECT canonical_id,
         payload->'confidence'->>'maturity' AS maturity,
         payload->'navigation'->>'winning_page_url' AS wpu,
         jsonb_array_length(coalesce(payload->'navigation'->'explored_links', '[]'::jsonb)) AS exp_n
  FROM scrape_profiles
  WHERE canonical_id = ANY(%s)
""", ([pid1, pid2, ...],))
for row in cur.fetchall(): print(row)
```

### Run pytest before any push

```bash
pytest tests/pms tests/fetch -q --tb=line                  # ~30s
pytest tests/integration --deselect tests/integration/extract/test_extract_cross_page_link_hop.py::test_h5_visited_urls_dedupe -q --tb=line  # ~30s
```

The deselect-list captures known pre-existing failures (`test_h5_visited_urls_dedupe` has been failing on `cf9c4ba` since before today's session — confirm with `git stash && pytest <test> && git stash pop` before adding new entries).

---

## Phase 15 — File location reference

| What | Where |
|---|---|
| `_PORTAL_URL_PATTERNS` (portal host substrings) | `pms/adapters/_html_extract.py:891` |
| `_extract_portal_iframe_hints` (3-pass iframe/anchor/quoted scan) | `pms/scraper.py:1146-1198` |
| `extract_embedded_blobs_from_html` (1-4 MB JSON blob extractor) | `pms/adapters/_html_extract.py:689-777` |
| `find_unit_arrays` (recursive walker, ≥2 unit-signal keys) | `pms/adapters/_api_parser.py:108-189` |
| `_item_has_unit_signals` (uses normalize_field_key) | `pms/adapters/_api_parser.py:131-165` |
| `parse_api_responses` (consumer of walker + nested `rent.min`/`sqft.min` unwrap) | `pms/adapters/_api_parser.py:503+` |
| `_jsonld_item_has_unit_signal` (Schema.org + vendor fuzzy match) | `pms/adapters/_api_parser.py:180-241` |
| `has_floor_plan_signals` (single source of truth for "is this unit data?") | `pms/signal_engine/floor_plan_signals.py` |
| `FIELD_ALIASES` (vendor-camelCase to canonical) | `pms/signal_engine/floor_plan_signals.py:151-213` |
| `_PMS_SUB_PATH_PRIORS`, `_UNIVERSAL_SUB_PATH_PRIORS` | `pms/scraper.py` |
| `_rank_internal_links` (anchor + path keyword scoring) | `pms/scraper.py:1280+` |
| `_try_link_hop` (orchestrator) | `pms/scraper.py:1465+` |
| Cold-profile retry runner-side | `scripts/runners/jugnu.py:687-727` |
| Cold-profile retry in-memory profile clone | `pms/scraper.py:2402-2459` |
| LLM_DOM FP-signal gate | `pms/adapters/generic.py:2356-2375` |
| LLM_DOM retry-on-empty (prior TIER_4 winner) | `pms/adapters/generic.py:2407-2452` |
| RC3 monolithic deferral gate (`hop_depth == 0`) | `pms/signal_engine/decider.py:118-148` |
| `AdapterContext` field set (incl. `hop_depth`) | `pms/adapters/base.py:17-55` |
| `record_explored_link` (writer side) | `services/profile_updater.py:531-547` |
| `_is_infra_api_url` (profile poisoning filter) | `services/profile_updater.py` |
| `dom_hints_saved_this_run` flag (degraded-eviction suppression) | `services/profile_updater.py:556+` |
| L1 fetcher slow mouse-wheel scroll | `fetch/fetcher.py` (search `scroll_trigger`, `_SCROLL_STEPS`) |
| SGCaptcha early-exit | `fetch/fetcher.py` (search `SGCAPTCHA_WALL`) |
| Body-hash dedup (stripped text) | `pms/scraper.py:1094-1117` + `1721-1729` + `1858-1879` |
| Subpath URL composition fix | `pms/scraper.py:1958-1972` |
| Self-fetch suppression | `pms/scraper.py:1520-1561` |
| SightMap-vs-Entrata detector tiebreaker | `pms/detector.py:365-394` |
| `analyze_cloud_run.py` (artifact mirror + report generator) | `scripts/diagnostics/analyze_cloud_run.py` |
| `local_canary.py` (canary tool) | `scripts/diagnostics/local_canary.py` |
| Profile seeder for canary (CSV → local proppy) | `C:/tmp/seed_canary_profiles.py` |
| Cloud SQL profile export query | §6.3 of this document |
| **2026-05-15 additions** | |
| `_extract_portal_iframe_hints` 6-pass scanner (incl. unknown-portal discovery, AppFolio slug synth, inline-JS PMS parser) | `pms/scraper.py:1164-1330` |
| `_PORTAL_INFRA_BLACKLIST` (open-by-default complement) | `pms/adapters/_html_extract.py:1135-1240` |
| `_INLINE_JS_INIT_PATTERNS` (inline-JS PMS init capture) | `pms/scraper.py:1228+` |
| `_read_additional_property` (Schema.org `Product + additionalProperty` reader) | `pms/adapters/_html_extract.py:119-198` |
| `_extract_product_floorplans_as_units` (JSON-LD Pass 4 — Squarespace e-commerce) | `pms/adapters/_html_extract.py:445-525` |
| `count_listing_structural_signals` + `has_listing_structure` (STUB_AGGREGATE_COPY gate) | `pms/signal_engine/floor_plan_signals.py:391-490` |
| `_fuzzy_normalize` + `_PROTECTED_CANONICAL_KEYS` (camelCase vendor-key learning) | `pms/signal_engine/floor_plan_signals.py:230-356` |
| Redirect-aware `landed_url` in `_rank_internal_links` and `_try_link_hop` | `pms/scraper.py:1401-1556` + `pms/scraper.py:2790+` |
| Decider rule-conflation fix (rationale check at HOP_TO_URL) | `pms/adapters/generic.py:2655-2670` |
| Generic SPA-shell late-render detector (content-based, no host whitelist) | `fetch/fetcher.py:903-955` |
| `EMBEDDED_PORTAL_UNKNOWN_HOST_SEEN` event for cross-run aggregation | `observability/events.py` |
| Generic portal discovery design doc | `docs/generic_portal_discovery.md` |
| **2026-05-16 additions** | |
| Partial-recovery verdict emit (Bug #1) | `scripts/runners/jugnu.py:415-465` |
| `_OUTCOME_VERDICT_PREFIX` extended for CANCELLED + 4 more (Bug #2) | `pms/scraper.py:1473-1495` |
| `extract.adapter_selected` field-name fix (Bug #3) | `scripts/diagnostics/analyze_cloud_run.py:298` |
| `_IFRAME_DATA_SRC_RE` + `_SIGHTMAP_EMBED_URL_RE` (Bug #4 — partial; see "Phase 17") | `pms/scraper.py:1149-1198` |
| `AdapterContext.candidate_portal_urls` + Entrata candidate-derived probe (Bug #5) | `pms/adapters/base.py:55-65` + `pms/adapters/entrata.py:284-317` |
| Redirect-to-entry persistence into shared_budget (Bug #6) | `pms/scraper.py:2634-2655` |
| `entry_captcha_detected` / `entry_bot_blocked` split (Bug #7 + #11) | `scripts/diagnostics/analyze_cloud_run.py:85-96, 442-465` |
| `--expected-shards` default 100 + clamped denominator (Bug #8) | `scripts/diagnostics/analyze_cloud_run.py:1115-1124, 629` |
| `count_floor_plan_signal_cardinality` tests (Bug #9) | `tests/pms/signal_engine/test_floor_plan_cardinality.py` |
| `_emit_signal_inspection` property_id parameter (Bug #10) | `pms/adapters/_api_parser.py:634-689` |
| Wedge-rescue HTTP_ONLY retry pass | `scripts/runners/jugnu.py:497-680` |
| Hop-count cap on zero-signal entry pages | `pms/scraper.py:3375-3405` |
| `_HOP_CUMULATIVE_BUDGET_MS = 240_000` cap | `pms/scraper.py:2402-2435` + accumulator at 2504-2518 |
| Browser-restart on `context.close()` timeout | `fetch/browser_pool.py:165-240` |
| Host-level `page.goto` timeout (`asyncio.wait_for`) | `fetch/fetcher.py:694-722` |
| AsyncPool capped to `MAX_CONCURRENT_BROWSERS` | `scripts/runners/jugnu.py:307-329` |
| `WEDGE_RESCUE_RETRY_STARTED` / `_RESOLVED` EventKinds | `observability/events.py` (Phase 17) |
| Alternate SightMap init patterns (Bug #4 follow-up) | `pms/scraper.py:1438+` |

---

## Phase 17 — Deferred follow-ups (2026-05-16)

### Bug #4 — parent-marketing-site detection (PIDs 14524, 234945, similar)

**Diagnosis from 2026-05-16 Playwright forensic:**
- **14524 venterraliving.com** — corporate parent. 130 KB HTML, 0 SightMap/Engrain references in static or rendered HTML, only GTM iframe. The detector routed to `sightmap` because `pms_detected` was carried over from an earlier cloud run, but the entry page has no inventory at all. Inventory lives on `/apartments/<city>/<property-slug>/` sub-pages that the link-hop scheduler doesn't currently surface from a corporate-tier URL.
- **234945 imtresidential.com** — corporate parent. 9.6 MB HTML, **1838 'engrain' substrings** but **zero `engrain.com/<id>` URLs**. The substring count was a misleading signal — the 'engrain' references are CSS class names / content metadata, not SDK integration. Inventory lives on per-property sub-pages.

**Why the existing fixes don't help:**
- `_extract_portal_iframe_hints` requires an actual iframe `src=` / `data-src=` / quoted-URL match. Corporate marketing pages don't have those.
- `_scan_inline_js_pms_init` requires a SightMap-init call. Corporate pages don't have those either.
- The PMS detector's substring-only check (`if "sightmap.com" in h` at `pms/detector.py:427`) is the culprit for routing label; demoting it without corroboration is risky because some legitimate properties only have the SightMap host in a CDN reference + a JS-injected iframe.

**Recommended fix path (not yet shipped):**
1. **PARENT_SITE detector**: when a page has (a) multi-state property nav (≥3 state-name anchors), (b) zero per-unit signals (rent/bed/bath count == 0), (c) iframe count ≤1 AND the only iframe is on `_PORTAL_INFRA_BLACKLIST` (GTM, reCAPTCHA, AudioEye), classify as `PARENT_SITE_NEEDS_HOP` and surface a list of property-sub-anchors as hop candidates at score 9_500 (above PMS_PRIOR, below known-portal).
2. **Detector tightening**: at `pms/detector.py:427`, require `sightmap.com/(embed|api|app)/[\w-]+` rather than the bare substring. The strong path at lines 377-395 stays. Properties that match the bare host but not the structured path get DEMOTED from `sightmap` routing and fall through to the generic cascade.
3. **Telemetry**: emit `PARENT_SITE_DETECTED` event so cross-run aggregation can quantify how many failures fall into this category. Initial estimate: 2 of 4 SightMap_SHAPE_REJECTED canary failures, ~6-15 of the 25 cloud SightMap_SHAPE_REJECTED bucket.

Deferred because: requires new heuristic + telemetry + a new hop-candidate score band. Lower ROI than the immediate post-deploy Bug #4 alternate-init-pattern fix.

### Bug #4 — CF-protected SightMap properties (PID 16139)

**Diagnosis from Playwright forensic:** chaseknollsapts.com returns CF 403 on urllib but renders fine in Playwright with the default Chrome UA. The rendered HTML contains `sightmap.com/embed/api` (the SDK endpoint, not the iframe URL — the property-specific embed-id is supplied via an init call the agent couldn't enumerate).

**Status:** Partially fixed. The 2026-05-16 patch added 3 more inline-JS init patterns to `_INLINE_JS_INIT_PATTERNS` (`embed_id` / `mapId` / `<sightmap-*>` custom element + an Engrain-context permissive last-resort). If the chaseknolls SDK uses any of those, the iframe URL will be synthesized correctly. If not, a Playwright-rendered scan is the only fix — deferred.

**To verify after next cloud deploy:** check whether 16139 appears in the new run's `TIER_1_API_SIGHTMAP` (success) or stays in `TIER_1_API_SIGHTMAP_SHAPE_REJECTED`. If still failing, dispatch a follow-up Playwright forensic with `page.evaluate("window.SightMap")` to dump the actual SDK config blob.

---

## Phase 16 — Closing checklist before shipping a fix

1. **Code change** has file:line citations in the commit message.
2. **Unit test** exercising the change via a real ctx/profile (not Mocks).
3. **Live-fetch verification** if the change references specific URLs/hosts/patterns.
4. **Pytest** `tests/pms tests/fetch` green; deselect pre-existing failures explicitly.
5. **Cold canary** on a 32-PID sample from yesterday's failures + 4 sentinels. REGRESSED == 0.
6. **Warm canary** (with prod profiles seeded) — if your fix is profile-dependent, this is mandatory.
7. **Verify on at least one specifically-diagnosed PID** that the new event trace matches the expected tier sequence.
8. **No half-finished changes** in the diff (TODO comments, commented-out code, unused imports).
9. **Anti-patterns audit:** for each Phase 0 anti-pattern, ask yourself "did I avoid this in my diff?"

---

## Appendix A — Glossary

- **Cold profile** — `confidence.maturity == COLD`, typically `winning_page_url == None`; no prior-run state.
- **Warm/Hot profile** — `confidence.maturity ∈ {WARM, HOT}`; has at least one prior SUCCESS run.
- **PMS prior** — sub-path guessed from the detected PMS (e.g. `/floorplans` for RentCafe). Score 5000-5095.
- **Anchor-discovered link** — `<a href>` found by `_rank_internal_links`. Score 0-5600 depending on keywords.
- **Portal URL** — third-party PMS leasing widget (SightMap embed, OneSite portal, AppFolio listings, etc.). Score 10000 when found by §8.2 portal scan.
- **STUB_URL** — proposed verdict for properties with no unit data on ANY reachable page (§11). Not yet shipped.
- **ENV_MISMATCH** — local canary failure that the cloud run handles correctly (usually CF/bot-block with residential proxy).
- **Self-fetch** — `profile.winning_page_url == entry_url`; symptom of homepage being mistakenly tagged as the unit-data URL.
- **Hop_depth bug pattern** — a gate that reads `ctx.X` where `X` was never wired into the upstream `AdapterContext`; gate silently always-True or always-False.
