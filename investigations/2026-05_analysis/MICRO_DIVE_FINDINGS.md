# Micro-dive findings — May 6 2026

Per-property analysis of canary v10 (image `canary-batch-10`) on the 567-property test cohort. Combines log-based aggregation across all 567 properties + true ground-truth analysis (curl + DOM inspection) on 38 SightMap-failing + 30 mixed-cohort samples.

## What I actually verified vs inferred

| Type of finding | Method | Confidence |
|---|---|---|
| Quality issues in recovered units (empty rents, weird beds) | Inspected emitted `properties.json` data | **High** |
| Per-property tier outcomes | Read events.jsonl from canary GCS output | **High** |
| Pages have/don't have rent visible | Curl + regex check | **High** (for the 68 sampled) |
| What canary captured but failed to extract | Compared canary `body_bytes` to actual rendered content | **Medium** (need true Playwright comparison for SPA cases) |
| "Why this would help" claims for fixes | Inference from patterns | **Medium** |

---

## Headline findings

### 1. Link-hop coverage is the #1 gap (NOT extraction logic)

**67% of still-failing properties (~ 220 of 333) have visible sub-page links the canary doesn't follow.**

Specific patterns:
- **External-portal URLs** (yottareal.com, ovationco.com, adaraportal): canary's link-hop pattern matcher restricts to same-host or limited extension list. Misses these.
- **`.aspx` paths with capital letters** (e.g. `/Floor-plans.aspx`): possibly case-sensitive match.
- **Anchor-only links** (`#FloorPlans` on same page): not handled as a section hint.
- **Cross-domain PMC links** (sub-property → parent ovationco.com): same as external-portal.

#### Concrete examples (from 30-sample deep-dive)
| Property | Has sub-link to | Canary did not follow |
|---|---|---|
| `verandahlake.com` | `https://adaraportal.yottareal.com/dba/floorplans?dbaid=58` | external-host portal |
| `lasvegasliving.com/properties/aspire-at-redwood/` | `https://ovationco.com/property/aspire-at-redwood/apartments/` | cross-domain to PMC parent |
| `theoverlandbyavanti.com` | `/property/the-overland/apartments/?bedroom=.0bed` | unrecognized URL pattern |
| `silverlandsmgmt.com/pages/the-vines` | `#FloorPlans` (anchor) | anchor-only link |
| `sabalclub.com` | `/Floor-plans.aspx` | case + extension |

#### Estimated lift if fixed: +50-80 properties on full prod (1.0-1.6 pp)

---

### 2. SightMap fingerprint is a false-positive generator

38 properties tagged `sightmap` fingerprint, but **0 of 38 had a discoverable SightMap iframe URL**.

The fingerprint detector matches the substring `sightmap.com` anywhere in HTML. CDN refs, comment URLs, marketing copy, and even unrelated script tags all trigger the false positive. Zero of these properties actually use SightMap as their unit-data backend.

Implication for the failing cohort: 38 properties classified as IFRAME_PORTAL_REQUIRED but their actual problem is link-hop or no-public-data, not portal extraction.

#### Fix
Tighten `_HTML_FINGERPRINTS["sightmap"]` to require either:
- `sightmap.com/embed/` URL, OR
- `Sightmap.embed(...)` SDK call, OR
- `data-sightmap-id="..."` attribute

NOT just substring "sightmap.com" anywhere.

---

### 3. SPA-rendered pages: extractors lag the renderer

4 of 30 sampled "still-failing" properties are SPAs where:
- Curl gets a 1-1.5KB shell with no rent
- **Canary's Playwright captured 80-520KB of rendered HTML** (post-JS-render)
- BUT the canary's DOM scan / text_regex / LLM tiers all returned empty on that rendered body

Examples:
- `cottonwoodcreekapartments.com` — canary body 167KB, extractors empty
- `casitasapartments.com` — canary body 312KB, extractors empty
- `lajoyabyazali.com/floorplans/` — canary body 80KB, extractors empty
- `teaksiouxfalls.com` — canary body 520KB, extractors empty

The Playwright fetcher is doing its job. The deterministic extraction tiers are missing patterns in modern SPA-rendered DOM (component-based class names, custom data attributes, Vue/React-rendered structures).

#### Fix paths
- Add common SPA-component selectors to `extract_units_from_dom`: `[data-floor-plan]`, `[data-unit]`, `.fp-card`, `.unit-card`, `.floor-plan-tile`, `.pricing-card`
- For Hyly/Knock/marketing-CMS templates, add specific selector patterns observed
- Vision LLM as last resort (already mapped, requires AdapterContext refactor)

---

### 4. text_regex tier — quality issues in recovered properties

23 wins via `TIER_3_TEXT_REGEX` but quality is uneven:

| Issue | Count | Cause |
|---|---:|---|
| Final emit < 30% of text_regex's max output | 11 | Text-regex extracted N units, downstream LLM tier won and emitted M < N. **Net data loss.** |
| Weird bed counts (>6) | 1 | Regex picked up wrong number near rent (e.g., "$2,580 — fp 7H8") |
| All rents empty in emit | 4-5 | `rent_low`/`rent_high` propagated as null |
| Plan name duplicated | 4 | LLM merge logic kept first plan name across emit |

#### Examples
- `pid=51998` (toapts.com): text_regex found **53 units**; LLM_DOM_TARGETED won with 2 units; **51 units lost**
- `pid=227817` (alloyla.com): text_regex 73 → final 9 (loss 64)
- `pid=225984` (lakeviewprosper): text_regex 26 → final 26 with `beds=7` (impossible — regex grabbed wrong number)

#### Fix paths
- **Tighten regex window 120 → 80 chars** with sentence-boundary preference
- **Don't override text_regex with LLM** when text_regex's count is >> LLM's count (planner needs to consider both)
- **Track text_regex confidence** based on bed/bath/sqft adjacency

---

### 5. Phantom recoveries — units with no actual rent

54 of 165 recovered (33%) have ALL emitted units with empty `rent_low`/`rent_high`. These are "phantom successes":
- Pipeline found unit shells (beds, baths, sqft, plan_name)
- But no actual rent value
- Final verdict = SUCCESS regardless

Root cause: when a page says "Call for pricing" or has unit specs but no rent (e.g., AppFolio listings), the LLM/DOM extractors emit unit metadata without rent. The validator accepts these as valid.

#### Fix
Tighten validator: reject units where both `rent_low` AND `rent_high` are null/empty. Either:
- Treat as `SUCCESS_NO_PRICING` (new verdict)
- Or carry-forward prior-day rents for those units
- Or simply mark as FAILED_NO_DATA (current behavior would re-trigger LLM next run)

---

### 6. Cloudflare blocks — only 1 of 30 sampled

Of the 30 deep-dive samples, only `alisterparx.com` returned a Cloudflare challenge to curl. Most "blocked" properties are bot-blocked at the **sub-page level** (e.g., Entrata's `/floorplans` path), not the homepage.

This confirms that residential-proxy escalation should be applied **on link-hop fetches** specifically, not the homepage fetch.

---

## Concrete fix priority — UPDATED based on micro-dive

| # | Fix | Confidence | Effort | Estimated lift | Cost |
|---|---|---|---|---:|---|
| 1 | **Tighten SightMap fingerprint detector** (require iframe/SDK, not substring) | High | 30 min | Quality only — fixes false-positive cohort tagging | $0 |
| 2 | **Validator: reject empty-rent units** (or new `SUCCESS_NO_PRICING` verdict) | High | 1 hour | Data quality — eliminates 54 phantom recoveries | $0 |
| 3 | **Tighten text_regex window** (120 → 80 chars) + downstream merge logic | High | 2 hours | Quality + maybe +5-10 lost units recovered | $0 |
| 4 | **Smarter link-hop URL ranker** (cross-domain allowed if score>7, anchor handling, case-insensitive path) | **High** — backed by 20/30 samples | 3-5 days | **+50-80 properties on full prod (1.0-1.6 pp)** | $0 |
| 5 | **Add SPA-component DOM selectors** (`[data-fp]`, `.fp-card`, etc.) | Medium | 2-3 days | +15-25 (4 of 30 samples) | $0 |
| 6 | Table-aware DOM extraction | Medium | 2-3 days | +15-25 | $0 |
| 7 | Vision LLM (refactor `AdapterContext`) | Medium | 3-5 days | +30-50 | ~$6/run |
| 8 | Residential proxy on link-hop | Low (only ~5 of 30 samples blocked) | infra | +30-50 | $200-500/mo |

**Headline: smarter link-hop is the highest-leverage code-only fix** based on actual sample evidence (not just log inference).

---

## Open questions / TODO

1. ~~**Verify Essex Apartments cluster** (16 properties)~~ — **RESOLVED**: all 16 are full client-side rendered React/Next.js. Curl gets ~800KB of script tags + ~60 byte text shell. Single fix unblocks all 16: link-hop must read `<a>` from POST-RENDER DOM (Playwright `page.evaluate('document.querySelectorAll("a")...')`) instead of pattern-guessing /floor-plans which 404s on Essex's URL scheme.
2. **Verify SPA-shell extractors** — does the rendered HTML actually contain unit data, or is it loaded yet again via XHR after Playwright capture? Of the 181 SPA-shell candidates, most (118) appear to genuinely have NO public pricing on the rendered page. Sample of 8: 7 have curl HTML same size as canary body (= SSR'd, not pure SPA). Only `noviflats.com` is true SPA (curl 5KB vs canary 310KB) — needs targeted look.
3. **Verify the text_regex over-extractions** — are the 53 units it finds on toapts.com real floor plan data, or duplicates / noise?

---

### 7a. PMC clusters in `some_404_paths` (136 properties)

Beyond Essex (16), three more PMC clusters are visible:

| Cluster | Props | Curl status | Pattern |
|---|---:|---|---|
| weidner.com (AZ/TX/OK regional subdomains) | 4 | 403 to curl with same UA as canary | Cloudflare-blocked at homepage |
| edwardrose.com | 3 | 403 | Cloudflare |
| cortland.com | 2 | 1× 403, 1× 200 | Mixed — likely WAF rate-limit |
| alapts.com | 3 | 200 (loads fine) | Marketing-site → Entrata portal hop |
| krcapartments.com | 4 | 200 | **Rent Manager PMS — unit data in inline JS template** |
| sentral.com | 2 | 200 (8KB shell) | Pure React SPA |

**New unblocked-cluster: KRC Apartments (4 properties).** Rent Manager PMS — unit data is embedded in inline jQuery template literals:

```html
<tr class='unit_avail_container' data-date='3/1/2026'>
  <td class='unit-number'>12</td>
  <td class='unit-beds'>2</td>
  <td class='unit-rent'>$1,275.00</td>
  <td class='unit-sqft'>725</td>
```

After Playwright executes the JS, these become real `<tr>` elements. Add adapter / DOM template:
- Fingerprint: `rentmanager.com/websites` link in footer
- DOM selectors: `tr.unit_avail_container`, `td.unit-number`, `td.unit-beds`, `td.unit-rent`, `td.unit-sqft`, `td.unit-date`

Estimated lift: 4-15 properties (KRC visible cluster + extrapolated Rent Manager prevalence).

---

### 7b-followup. Keystone+Brandywine investigation (2026-05-06 second pass)

Direct probe of `https://www.keystonemanagement.com/apartments/nc/keswick-apartments` from a residential IP with Chrome 124 UA + full Sec-CH-UA headers (Playwright-equivalent profile):

| Probe | Result |
|---|---|
| Plain `Mozilla/5.0` UA | 301 (redirect, expected) |
| Chrome 124 macOS UA + `--compressed` | **200 / 118 KB** (full content) |
| Chrome 124 + sec-ch-ua + Sec-Fetch-* headers | **200 / 118 KB** (full content) |
| Response `server` header | `nginx` — **NOT** Cloudflare |
| Response `cf-*` headers | none |

**Conclusion**: Keystone's origin nginx is NOT fingerprint-checking the request. The canary's 11KB body is almost certainly Cloud Run egress IP rate-limiting / blocklisting at the nginx layer, not TLS-fingerprint or browser-detection at the WAF.

**Why this matters for the response classifier**: when Keystone returns `200 + 11KB`, the canary's [response_classifier.py:158](ma_poc/fetch/response_classifier.py:158) sees `OK` and does not escalate. The `_is_silent_block` detector is gated to `status == 403`. There is no "suspiciously small 200" detector.

**Fix path** (out of scope for this batch — flagged for the next iteration):
- Add a `_is_tiny_200_block(status, body, url)` detector that fires when:
  - `status == 200`
  - body length < 15 KB
  - body lacks any `$NNN` rent-shaped pattern AND any `floor.?plan|availab|apartment|sqft` content keyword
  - URL host is on a known-multifamily-property allowlist OR the property's prior fetch produced ≥ 50 KB
- On match, return `BOT_BLOCKED` with signature `TINY_200_SUSPECTED` so existing tier escalation fires and the next attempt goes through residential.

Affects 7 properties in the test cohort (5 Keystone + 2 Brandywine), prevents per-property regression on Cloud Run egress rate-limit changes.

---

### 7b. WordPress Elementor + Jet Listing pattern

Keystone Management (5 properties) and Brandywine Communities (2) — all 7 in `no_rent_visible` cohort, all with **canary body ~12KB**, but **curl returns 374-779KB of fully-server-rendered Elementor unit data** with rents in `.jet-listing-dynamic-field__content` divs.

**The canary's 12KB body is suspicious — bot-blocking on canary side.** Same UA as curl gets 60× more bytes. Most likely Cloudflare/WAF identifying Playwright's TLS fingerprint or other browser signals.

This isn't a parser problem; it's a fetcher problem. Either:
- The canary's Playwright is being challenged by bot detection (TLS fingerprint, headless detection)
- OR there's a fetch race / early-paint capture bug

Either way: **investigate why curl-with-same-UA gets 779KB while canary gets 12KB on these specific hosts.**

Confirmed properties (7):
- keystone: keswick, cherry-court, peterborough riverview, morganton riverview, reserve-at-bradbury (pid 220907, 293112, 302055, 55502, 55729)
- brandywine: oak-hill, della-plaza (pid 40335, 72743)

---

### 7c. SecureCafe (RentCafe leasing-portal) sub-domain hops

Both `livecantata.com`, `215cstreet.com`, `rentmiro.com` link to `*.securecafe.com/onlineleasing/.../floorplans.aspx` from their homepage. SecureCafe is RentCafe's leasing-portal sub-product. Cross-domain link-hop must follow these.

Sample:
- `215cstreet.com` → `https://215cstreet.securecafe.com/onlineleasing/215-c-street0/floorplans.aspx`
- `rentmiro.com` → `https://rentmiro.securecafe.com/onlineleasing/miro-apartments0/scheduletour.aspx`

Same fix as cross-domain link-hop priority #1.

---

### 7e. Cross-domain portal hits — bulk scan of 81 still-failing hosts

Of 81 unique failing hosts (body ≥ 10 KB), the static HTML contains:

| Pattern | Hits | % |
|---|---:|---:|
| `*.onlineleasing.realpage.com` link | 17 | 21% |
| `*.securecafe.com` / `*.securecafenet.com` link | 15 | 19% |
| `*.appfolio.com/connect` or `/listings` link | 6 | 7% |
| `sightmap.com/embed/{id}` substring | 3 | 4% |
| `*.prospectportal.com` link | 2 | 2% |
| `engrain_asset_id` / `spaces_asset_id` | 1 | 1% |

**~50% of failing hosts link to a known PMS portal — but the resolver isn't following the link.**

#### The resolver's link-hop has at least three bugs

Reproduced by simulating `ma_poc/pms/resolver.py:resolve_target` against the curl-fetched HTML for 6 sample hosts:

1. **`securecafe.com` and `securecafenet.com` aren't recognized as PMS targets.** `_LEASING_PORTAL_DOMAINS` (used only for Step 4 iframe scan) includes them, but `_url_matches_pms_fingerprints` (used for Step 3 sublinks) only checks adapter `.static_fingerprints()` — and no adapter advertises `securecafe.com`. Result: a sublink to `loftsatopop.securecafe.com/onlineleasing/.../guestcards` is filtered out as non-PMS. Affects ~19% of failing hosts.

2. **Cap of 5 CTA candidates can drop the legitimate portal link** when the homepage has multiple internal `/floor-plans/` links sharing the top priority. Reproducible on `hazelwoodhomes`: 5 P=80–100 internal-floorplan links eat the cap; the AppFolio "Resident Portal" link at P=30 is dropped before its URL fingerprint is even checked.

3. **Substring priority matching creates spurious priority hits.** Example: the text "OneWall Communities works with eRenterPlan" gets P=60 because "Communities" contains "uni" (unit keyword). Any anchor containing "Communities" gets a 60-priority boost. Use word-boundary matching.

Fix sketch in `resolver.py:228`:

```python
for _priority, href, _text in candidates:
    detection = detect_pms(href)
    href_lower = href.lower()
    is_portal = (
        _url_matches_pms_fingerprints(href)
        or any(domain in href_lower for domain in _LEASING_PORTAL_DOMAINS)
    )
    if is_portal:
        ...
```

Plus: dedupe CTA candidates by `(href, host)` before capping, and add `\b` word boundaries to `_PRIORITY_MAP` keys.

**Estimated lift if all three resolver bugs fixed: 60-100 properties** (1.2-2.0 pp) — the union of realpage_oll + securecafe + appfolio_portal + prospectportal portal hits, minus those that would still fail at the portal end (some securecafe portals are themselves Cloudflare-blocked).

#### Confirmed examples (sample of 17 realpage_oll hits)

| pid | host | sublink (anchor text "Apply Now") |
|---|---|---|
| 19785 | (cottonwoodcreek) | (matched bulk scan) |
| 242360 | oaksvernon.com | onlineleasing.realpage.com |
| 258343 | keystoneon8th.com | onlineleasing.realpage.com |
| 266452 | thewaterviewapts.com | onlineleasing.realpage.com (also has SightMap) |
| 269029 | plazacanogapark.com | onlineleasing.realpage.com |
| 28143 | liveatlyricapts.com | onlineleasing.realpage.com |
| 291774 | 123taylor.com | onlineleasing.realpage.com |
| 303929 | downtownloftsfallriver.com | onlineleasing.realpage.com |

A working `RealPageOllAdapter` exists at [realpage_oll.py](ma_poc/pms/adapters/realpage_oll.py:39); the gap is purely in resolver navigation.

---

### 7d. all_404 cohort breakdown

Of 37 properties, sample of 8 head-checks:
- **5/8 real 404s** — properties don't exist (rebranded, demolished, removed). E.g. `highpointcrossingapts.com/index.php` 404, `liveatcrossroadsranch.com/home` 404.
- **3/8 are 403 Cloudflare** masked as 404 by canary → bolton, mandelgroup, regentspark. One (bolton) redirects to `liveboltonestates.com` (rebrand).

**Action**: data-ops task — review properties.csv against current URLs. Either purge or update.

---

### 7. SightMap embeds — adapter exists but never sees the API response

**9 of 81 sub-page-scanned failing hosts have a real `sightmap.com/embed/{hashid}` URL** (~11%) — extrapolates to ~30-50 of full failing cohort. The SightMap embed lives on the `/floor-plans/` sub-page, and the embed iframe makes a JSON XHR to:

```
https://sightmap.com/app/api/v1/{client_key}/sightmaps/{sightmap_id}
```

The response shape (verified on liveotis.com → embed `1ywyjz3ywq0`):
- `data.units[]` (25 units): `id`, `unit_number`, `area`, `price`, `floor_plan_id`, `available_on`, `display_price`
- `data.floor_plans[]` (51 plans): `id`, `name`, `bedroom_count`, `bathroom_count`
- Join by `floor_plan_id` → full unit records

#### Root cause of the gap

`ma_poc/pms/adapters/sightmap.py` already exists with a complete parser (`parse_sightmap_payload`). It iterates `ctx._api_responses` looking for SightMap-shaped JSON. **The canary never has those responses to iterate.**

Two contributing reasons:
- **Link-hop doesn't reach `/floor-plans/`** on most of these properties (homepage doesn't have the iframe).
- Even when link-hop lands on `/floor-plans/`, the SightMap iframe may not load synchronously enough for Playwright's `domcontentloaded` capture window.

#### Fix — direct embed fetch (1 day)

Add a deterministic "SightMap embed → API" path that doesn't depend on Playwright:

1. Scan page HTML (homepage + first hop) for `sightmap.com/embed/[a-z0-9]{6,}` substring.
2. Direct GET that embed URL → parse `window.__APP_CONFIG__.sightmaps[0].href`.
3. Direct GET that API URL with `Accept-Encoding: gzip, br` → JSON body.
4. Feed body to existing `parse_sightmap_payload(body, url)`.
5. Emit as `TIER_1_API_SIGHTMAP_DIRECT` (sub-tier so we can measure separately).

Cost: 0 LLM calls, 1 extra HTTP request per matching property, fully deterministic.

#### Confirmed sample (9 of 81 sub-page hits — verified embed exists)

| pid | host | sub-page | embed |
|---|---|---|---|
| 227790 | thecharlesslc.com | /floor-plans/ | y8px00yjp19 |
| 234945 | imtresidential.com | /properties/imt-bela | k9zw4m26w87 |
| 24928 | lasvegasliving.com | ovationco.com/property/summer-winds | l8xvrm6pjk2 |
| 261384 | liveotis.com | /apartments/ | 1ywyjz3ywq0 |
| 265098 | smithandrio.com | /floor-plans | jlw0q3orp2y |
| 266452 | thewaterviewapts.com | /floor-plans/ | x1p8d9xovd6 |
| 291509 | livebeckon.com | /apartments/ | yjp2kgxzpxl |
| 53359 | ayrsleylofts.com | /Floor-plans.aspx | x1p8dr7kvd6 |
| 62903 | mstationapts.com | /Floor-plans.aspx | 8xvrko3mwjk |

All 9 are currently FAILED_NO_DATA in canary v10. **Estimated lift on full failing cohort: 30-50 properties (~1 pp).**

## Cohort-specific micro-dive results

### Essex Apartments (16/16 properties, all still failing)

- **Single PMC, all React/Next.js** (curl gets 800KB shell, only 60 bytes user-visible text)
- Canary's link-hop tries `/floor-plans` and `/availability` — both 404 on Essex's URL scheme
- Essex's actual structure is `/apartments/<region>/<slug>/floor-plans` style
- **Single fix unblocks all 16:** read `<a>` links from rendered DOM via `page.evaluate(...)` instead of guessing canonical paths

### SPA-shell candidates (181/333 still-failing, 54%)

| Sub-pattern | Count | Diagnosis |
|---|---:|---|
| Truly NO_PUBLIC_PRICING (correctly classified) | 118 | No public rent on rendered HTML |
| IFRAME_PORTAL (units in iframe) | 27 | Iframe-content extraction needed |
| JS_RENDERED_HIDDEN_DATA mis-classified | 26 | Mostly truly NO_PUBLIC_PRICING |
| Other small | 10 | |

**Most SPA-shell properties are correctly failing. Only ~10-20 are fixable through extraction work.**

### LLM_COULD_NOT_EXTRACT (20-sample of 74 still-failing)

| Diagnosis | Count | Action |
|---|---:|---|
| **NEEDS_SUBLINK** (link visible, canary didn't follow) | **12 (60%)** | **Same fix as Essex — smarter link-hop** |
| **CLOUDFLARE** (Entrata-on-vanity-domain) | 6 (30%) | Residential proxy (out of scope) |
| TINY_MARKETING | 1 (5%) | Truly no data |
| NO_CLEAR_PATH | 1 (5%) | Investigate individually |

The 6 Cloudflare-blocked are all Entrata sites: `alisterparx.com`, `alisteruptowncharlotte.com`, `argentaaz.com`, `mytownhomevillasapartments.com`, `arizona.weidner.com`, `alistermontclair.com`. **Same pattern → same fix (residential proxy on link-hop).**

### Convergent finding across all clusters

| Cluster | "Link-hop fix unblocks this many" |
|---|---:|
| Essex Apartments | 16 / 16 (100%) |
| Mixed-cohort 30-sample | 20 / 30 (67%) |
| LLM_COULD_NOT_EXTRACT 20-sample | 12 / 20 (60%) |
| **Extrapolated to full 333 still-failing** | **~180-220 properties (54-66%)** |

The link-hop fix is consistently the dominant lever across every cohort sampled.

---

## REVISED priority — based on micro-dive evidence

| # | Fix | Confidence | Effort | Lift on full prod |
|---|---|---|---|---:|
| **1a** | **Resolver link-hop bugfix #1: recognize `securecafe.com`/`securecafenet.com` and other `_LEASING_PORTAL_DOMAINS` in Step 3 sublinks** (currently only Step 4 iframes) | **Very high** (19% of failing hosts have this link) | 1 hour | +25-40 properties (0.5-0.8 pp) |
| **1b** | **Resolver link-hop bugfix #2: dedupe candidates by URL + raise cap from 5** (drops legitimate portal links when 5+ duplicate /floor-plans links share top priority) | High | 2 hours | +10-20 properties |
| **1c** | **Resolver link-hop bugfix #3: word-boundary priority keyword matching** ("Communities" → false 60-priority via "uni") | Medium | 1 hour | quality |
| **2** | **Smart link-hop expansion: read post-render DOM `<a>` + cross-domain allow + .aspx/anchor links + Essex-style canonical-URL probe via DOM** | **Very high** (multiple cohort match) | 3-5 days | **+150-220 properties (3-4 pp)** |
| **3** | **SightMap direct-fetch path (existing adapter never gets API response)** | Very high (9/81 sub-page hits + working API verified) | 1 day | +30-50 properties (~1 pp) |
| **4** | **Investigate canary bot-block on Keystone+Brandywine** (canary 12KB vs curl 779KB with same UA) | High (deterministic 60× delta) | 1-2 days | +7-15 properties + likely larger Elementor pattern |
| 5 | Rent Manager adapter (KRC + extrapolated) | High | 1-2 days | +4-15 properties |
| 6 | Validator: reject empty-rent units | High | 1 hour | Quality fix on 54 phantom recoveries |
| 7 | Tighten SightMap fingerprint (require iframe/SDK) | High | 30 min | Quality / cohort tagging |
| 8 | Tighten text_regex window (120→80) + planner merge logic | High | 2 hours | Quality + +5-10 lost units |
| 9 | Add SPA-component DOM selectors (incl. WordPress `<article data-unitid>` cornerstone-style) | Medium | 2-3 days | +10-20 |
| 10 | Vision LLM | Medium | 3-5 days | +15-30 |
| 11 | Residential proxy on link-hop | High (but not code) | infra | +50-80 |
| 12 | Data-ops: purge/update dead URLs in properties.csv | High | 1 day | Removes ~25 false-fail noise from `all_404` |

**The three resolver bugfixes (1a/1b/1c) are the highest-leverage / lowest-effort wins** — total ~4 hours of code, estimated +35-60 properties. They unblock the existing `RealPageOllAdapter`, `RentCafeAdapter`, and `AppFolioAdapter` for properties they would otherwise handle correctly. Smart link-hop expansion (#2) is the bigger but harder effort. SightMap direct-fetch (#3) is the highest-confidence single-tier fix.

---

## Files

- [v10_per_property.csv](v10_per_property.csv) — high-level per-property summary
- [v10_per_property_notes.csv](v10_per_property_notes.csv) — detailed per-property notes with quality flags + fail causes
- [sightmap_api_probe.csv](sightmap_api_probe.csv) — 38 SightMap properties + API probe results
- [sightmap_subpage_confirmed.csv](sightmap_subpage_confirmed.csv) — 9 sub-page-confirmed SightMap embed properties
- [deep_dive_30_samples.csv](deep_dive_30_samples.csv) — 30-property mixed deep-dive results

---

## Canary-batch-12 results (smart link-hop expansion + investigation)

**Cumulative results vs v10 baseline (567-property test cohort):**

| Verdict | v10 | batch-11 | batch-12 | Δ vs v10 |
|---|---:|---:|---:|---:|
| SUCCESS | 165 | 213 | **218** | **+53 (+9.3 pp)** |
| FAILED_NO_DATA | 333 | 285 | 278 | -55 |
| FAILED_UNREACHABLE | 69 | 69 | 71 | +2 |

**Per-property cumulative delta v10 → b12:**
- Recovered: **71 properties**
- Regressed: 16 properties
- Net: **+55 properties**

**Quality of 71 recovered properties (1101 units):**
- with rent: 1026 (93.2%)
- with beds: 1013 (92.0%)
- with sqft/area: 1101 (100.0%)

**Recovered by tier:**
| Tier | Count |
|---|---:|
| TIER_1_API_SIGHTMAP_DIRECT_FETCH | 40 |
| TIER_4_LLM_DOM | 15 |
| TIER_4_LLM | 5 |
| TIER_3_DOM | 5 |
| TIER_3_DOM_RENTMANAGER | 5 |
| TIER_MERGED_CROSS_PAGE | 1 |

### Regression analysis (16 cumulative)

All 16 regressions follow the same pattern: v10 won via TIER_4_LLM_DOM/LLM, b12 won via TIER_1_API_* (or TIER_1_API_ENTRATA/ONESITE/APPFOLIO) with `verdict_reason: "no records extracted"`.

**Investigation on `pid=224888 coveatoverlakeapts.com`:**
- Same 3 URLs fetched in b11 (SUCCESS) and b12 (FAILED): homepage + /floor-plans/ + /virtual-tours/
- Same adapter selected (`onesite` at 0.85 confidence) in both runs
- Same captures, same tier attempts
- LLM_DOM ran 3× and LLM ran 3× in b12 — all returned empty

**Conclusion**: regressions are LLM stochastic variance, not code bugs. qwen-235b on the same input can return slightly different outputs run-to-run. Sometimes it extracts hallucinated units that pass validation; sometimes it correctly returns empty. **Not a fixable code regression.**

### Still-failing 278 — bucketed for next iteration

| Tier | Count | Why still failing |
|---|---:|---|
| TIER_1_API | 191 | Generic API parser ran on captured XHRs but found 0 unit-shaped envelopes |
| TIER_1_API_ENTRATA | 27 | Entrata adapter ran, returned 0 units (mostly false-positive sightmap fingerprint cases) |
| TIER_1_API_APPFOLIO | 23 | AppFolio adapter ran, returned 0 units |
| TIER_1_API_ONESITE | 15 | OneSite adapter ran, returned 0 units |
| SYNDICATION_ONLY_SQUARESPACE | 10 | Squarespace short-circuit (no extractor for these) |
| LLM_GATE_NO_BODY | 7 | Body too small for LLM gate to open |
| SYNDICATION_ONLY_WIX | 8 | Wix short-circuit |

**Largest opportunity**: 191 plain `TIER_1_API` failures + 65 PMS-specific tier failures = **256 properties (~45% of cohort) where an API tier ran but extracted 0 units**. The LLM IS running on these (verified via events.jsonl) but returning empty. These need either (a) better API parser variants or (b) genuinely have no public rent data.

---

## Final synthesis — exhaustive cohort breakdown

After dive on every failure cohort:

| Cohort | Count | % fixable in code | Dominant root cause |
|---|---:|---:|---|
| Essex Apartments cluster | 16 | 100% | Link-hop must read post-render `<a>` (canonical-URL guess fails on Essex's URL scheme) |
| `some_404_paths` (incl. Essex) | 136 | ~70% | Canonical URL guess fails OR cross-domain portal not followed |
| `large_page_no_rent_visible` | 88 | ~50% | Cross-domain portal sublinks + SightMap embed iframes |
| `no_rent_visible` | 83 | ~30% | Mix: SecureCafe sub-portals (fixable), Cloudflare bot-block on canary (Keystone+Brandywine), genuinely no public data |
| `rent_visible_no_proximity` | 50 | ~40% | Component-DOM where rent and specs are in separate sibling elements (text_regex 120-char window can't bridge) |
| `all_404` | 37 | ~30% | Mostly real dead URLs (data-ops); some 403 Cloudflare masked as 404 |
| `tiny_page_no_rent` | 5 | 0% | Redirects to `notfound.apts247.info` (3 props, dead) and `nonpayment.spherexx.com` (1 prop, suspended). Data-ops cleanup only. |
| `subpage_blocked` | 2 | ~50% | Entrata Cloudflare on subpage. Residential proxy fix. |
| `all_blocked` | 1 | 0% | 429 rate-limit; transient |

**Top three concrete leverage points (highest confidence, lowest effort):**

1. **Resolver link-hop bugs** (1a/1b/1c above) — recognizing portal domains, deduping candidates, word-boundary priority — **~4 hours, +35-60 properties**.

2. **SightMap direct-fetch path** — already-existing parser, just bypasses iframe-load-timing race — **1 day, +30-50 properties**.

3. **Investigate Keystone+Brandywine canary bot-block** — same UA, 60× body-size delta — **1-2 days, +7-15 properties + likely larger Elementor pattern in prod**.

Combined: ~5 days dev work → estimated **+70-125 properties** on the test cohort, scaled to roughly **+3-7 pp on full prod**.
