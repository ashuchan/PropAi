# HAR Files inventory + integration plan

Date: 2026-05-21
Source: `/Users/ankur/Downloads/HAR FILES.zip` (133 files, ~2.1GB extracted)
Index data: [index.json](investigations/2026-05-21-har-replay/index.json)

## Headline

The team's HAR captures land **almost entirely on the `T2_LLM_only` cohort** — properties where production currently relies on expensive LLM extraction. This is the exact target HAR-replay was designed for: substitute deterministic replay for an LLM call.

| | n |
|---|---:|
| Total HARs indexed | 133 |
| With candidate unit-data entries | 94 (70.7%) |
| Without (or shape didn't match the URL-path filter) | 39 |

## Overlap with the manual-validation cohorts

| Sheet | n in sheet | HARs that overlap |
|---|---:|---:|
| **`T2_LLM_only`** | **771** | **127 (95.5% of HARs)** |
| T3_No_extraction | 233 | 2 |
| T4_Code_Generic_DOM | 44 | 2 |
| T4_Edge_SightMap | 16 | 1 |
| T4_Code_Apts247 | 14 | 1 |
| T4_Code_Merged_cross_page | 47 | 1 |
| All other failure sheets | — | 0 |
| Domains not in any sheet | — | 5 |

The "L2 LLM HAR" framing now makes sense: **the team captured HARs for properties currently routed through Tier-4 LLM extraction**, so we can replace LLM calls with deterministic replays. 127 of 133 HARs cleanly target this cohort.

## PMS distribution across the 133 HARs

| PMS | HARs | % |
|---|---:|---:|
| knock | 35 | 26.3% |
| rentcafe | 30 | 22.6% |
| realpage | 27 | 20.3% |
| entrata | 19 | 14.3% |
| sightmap | 5 | 3.8% |
| apts247 | 5 | 3.8% |
| funnel | 4 | 3.0% |
| spherexx | 3 | 2.3% |
| g5 | 3 | 2.3% |
| repli360 | 3 | 2.3% |
| resman, appfolio | 1 each | 0.8% |

Most HARs touch **multiple PMS hosts** (e.g. RentCafe + Knock front-end on the same property). The dominant cluster is Knock-fronted Yardi/RentCafe — exactly the cohort where the live Knock-by-domain resolver shipped recently.

## Strong-signal sample (validated to contain rent + unit shape)

| Property | Body size | URL |
|---|---:|---|
| pepperwoodknoll.com | 37KB | `/CmsSiteManager/callback.aspx?act=Proxy/GetUnits` |
| rentcitadel.com | 18KB | `/CmsSiteManager/callback.aspx?act=Proxy/GetUnits` |
| staywelleby.com | 50KB | `/CmsSiteManager/callback.aspx?act=Proxy/GetUnits` |
| theleightonapartments.com | 11KB | `/CmsSiteManager/callback.aspx?act=Proxy/GetUnits` |

All four match the RealPage OneSite `Proxy/GetUnits` API pattern — confirming a deterministic parser exists. The remaining 90 candidates have unit-data URLs but my body-shape heuristic was too narrow (matched specific JSON keys). A pass that scores by body content rather than URL path would lift this number significantly.

## Proposed HAR-replay integration

Where this fits in the cascade I've been building (Phases 0-3):

```
adapter.extract(page, ctx)
  ├─ existing tier-1 paths (network_log, JSON-LD, etc.)
  └─ probe_get_with_render_fallback(target_url)
        ├─ probe_get (curl_cffi)
        │     ├─ retry-without-cookies          [Phase 0.3]
        │     └─ Web Unlocker                   [pre-existing]
        └─ render_url_via_l1                    [Phase 1.1]
              ├─ patchright
              └─ Camoufox if flag               [Phase 0.4]

NEW: HAR-replay rung — inserted BEFORE LLM tiers fire
  if har_manifest_exists_for(canonical_id):
     manifest = load_har_manifest(canonical_id)
     resp = curl_cffi.request(
         method=manifest.method,
         url=manifest.url,
         headers=manifest.request_headers,
         cookies=manifest.request_cookies,
     )
     if resp.ok and resp.body_shape_matches(manifest.expected_shape):
         return parse_via(manifest.parser_id)(resp.body)
     # else: fall through to LLM (current behavior)
```

This sits **above** the LLM tiers because:
- HARs are deterministic (free, fast, stable parsing)
- LLM is the existing fallback for these 127 properties
- A failed HAR-replay (stale token, posture change) gracefully falls through

## Implementation steps (Phase 4.x if pursued)

### 4.1 Pre-process HARs offline into manifests

For each HAR file:
1. Find the response that contains the actual unit data (largest JSON/HTML response matching unit-shape heuristics).
2. Emit a manifest record:
   ```json
   {
     "canonical_id": "...",
     "domain": "townecrest.com",
     "captured_at": "2026-05-21T07:33:00Z",
     "method": "GET",
     "url": "https://www.townecrest.com/...",
     "request_headers": {"Accept": "application/json", ...},
     "request_cookies": {"_ga": "...", "session_id": "..."},
     "expected_content_type": "application/json",
     "expected_min_body_size": 1000,
     "expected_body_signature": {"json_keys": ["floorplanid", "unit_number"]},
     "parser_id": "realpage_oll_proxy_getunits"
   }
   ```
3. ~5KB per property × 127 = ~635KB total. Small enough to check in or store in a sidecar DB table.

### 4.2 Wire replay rung into the adapter dispatch

In `scraper.scrape()` or each adapter's `extract()`:
- Before Tier-4 LLM, try `await replay_har(canonical_id, ctx)`.
- On success: skip LLM, return units, record `tier_used="TIER_HAR_REPLAY"`.
- On failure: keep going with current cascade (LLM still runs as fallback).

### 4.3 Drift detection

HARs decay over time:
- Cookies expire (session, CSRF tokens)
- CF clearance is replaced
- Site changes URL structure

Per-replay-failure counter on `ScrapeProfile.confidence`:
- 1 fail → retry next run
- 3 consecutive fails → mark manifest stale, fall through to live cascade for re-discovery
- Successful live extraction → fresh HAR-capture queued (manual or automated)

This mirrors the existing `_BlockedEndpoint.attempts` and `LlmFieldMapping.consecutive_replay_failures` patterns.

### 4.4 Sticky-route extension (Phase 2.1 alignment)

Add a new `last_winning_probe_source` value: `"har_replay"`. When set, the adapter skips even the probe/render cascade and goes straight to HAR-replay. Self-correcting same as the probe/render axis.

## What I haven't done yet (recommended next steps)

1. **Body-content scoring pass** — broaden the "strong-data" filter from URL-path matching to body-content scoring. Many of the 39 "no candidates" HARs likely DO contain unit data on unusual paths.
2. **Manifest generation script** — emit one JSON manifest per HAR, ready to load at scrape time. Estimated ~100 lines.
3. **Live replay smoke test** — pick 5 HAR manifests, feed them through curl_cffi with captured cookies, verify the response still parses cleanly (i.e. tokens haven't expired).
4. **Production wiring** — add `ENABLE_HAR_REPLAY` flag (default off), wire the new rung, measure tier-distribution shift on the next canary.

## Artifacts

- [index.json](investigations/2026-05-21-har-replay/index.json) — per-HAR index (133 records)
- [raw/HAR FILES/](investigations/2026-05-21-har-replay/raw/HAR%20FILES/) — original HARs (gitignored due to size)
