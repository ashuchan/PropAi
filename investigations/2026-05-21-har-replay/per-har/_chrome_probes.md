# Chrome MCP probes — 12 properties from bucket E

Date: 2026-05-21

## 1. jalexrentals.com
- **Type:** Management-company page (multi-property)
- **PMS infrastructure:** iloveleasing.com widget (`luv.js`) + Spherexx chat
- **Findings:**
  - Site uses `https://www.iloveleasing.com/pub/widget/js/luv.js` widget
  - No `/floorplans` path (404)
  - Property-specific units live behind per-property subpages
- **Verdict:** Not a single property; needs per-property URL list to probe individual rentals. **iloveleasing.com** is a NEW PMS that the classifier missed — worth adding.

## 2. livegrammercy.com
- **Type:** Single property, Denver CO
- **PMS infrastructure:** Custom CMS — same `_fp-renderable` pattern as therailat1380.com + vineyardscartersville.com (cluster now 3+ properties)
- **Key URLs found:**
  - `https://livegrammercy.com/floorplans/_fp-renderable/params%3Ainstance%3D20f4d721f54a51b85d9fed8b7f6d8490%26action%3Drender%26type%3Dlisting-chunks/?forcecache=1`
  - `https://livegrammercy.com/floorplans/?action=check-pricing-cache&property_id=`
- **Verdict:** Confirmed cluster member — `_fp-renderable` CMS now 3+ properties; build adapter route for this pattern.

## 3. tenzenapartments.com
- **Type:** Single property, Bellevue WA
- **PMS infrastructure:** WordPress + Pusher (real-time chat); no Knock API call observed on /floorplans page load
- **Note:** /floorplans returns 404; Floor Plans nav link may be hash anchor or single-page-app route
- **Verdict:** Knock signal in HAR likely from chatbot widget, not unit data. Static page likely has data inline (HTML) — generic extractor should handle if probe gets past the static page.

## 4. terrainaustin.com
- **Type:** WordPress + Elementor static page
- **Findings:** Floor plans rendered as JPEG images (`Terrain-Floorplan-1-Bed.jpeg`); no API
- **Verdict:** No API; LLM-OCR or vision tier is the only path. Static image extraction is out of scope for HAR-replay/API approach.

## 5. townecrest.com
- **Type:** RealPage CmsSiteManager
- **Findings:** URL becomes `townecrest.com/floorplans/#k=16893` (RealPage key). Loads `cs-cdn.realpage.com/OLL/prod/oll/` assets.
- **Verdict:** Existing **realpage.cms_sitemanager** adapter should handle. Confirms HAR finding — `townecrest.com` is in the 4-property RealPage CmsSiteManager cluster.

## 6. www.allenproperties.net
- **Type:** Multi-property management company landing
- **PMS infrastructure:** Uses "DZAP Inc / Essentials theme" (`themes/essentials/corp/main`) — generic Rails-based CMS
- **Findings:** Lists properties across multiple regions; no unit data on landing — lives on per-property sub-sites
- **Verdict:** Not a single property; needs per-property URL list (same as jalexrentals).

## 7. www.brooklaneapts.com
- **Type:** Single property, Brook Lane
- **PMS infrastructure:** Sierra.chat AI leasing widget (POST /api/graphql)
- **Findings:** No floor-plan API on page load; data likely inline HTML; "funnel" detection in HAR was misclassification of the chat widget
- **Verdict:** Static HTML page — generic extractor should handle.

## 8. www.cottonwoodcreekapartments.com
- **Type:** Single property
- **PMS infrastructure:** **Site123** generic website builder (`cdn-cms-s-8-4.f-static.net`, websiteID=4137727)
- **Findings:** No data API — page builder serves static HTML; data is in inline HTML blocks
- **Verdict:** Site123 = generic CMS; data extractable from HTML, no API to integrate.

## 9. www.crossingsmadison.com
- **Type:** Wix-hosted single property
- **PMS infrastructure:** Wix (`static.parastorage.com` = Wix CDN); `/floor-plans` AND `/floorplans` both 404
- **Findings:** Wix routes use UI-builder paths, not standard /floorplans
- **Verdict:** Existing wix/squarespace_nopms handler should handle — needs proper page-discovery (the floor-plan content is on a non-standard Wix URL).

## 10. www.foxchaseofalexandriaapts.com
- **Type:** Single property, Alexandria VA
- **PMS infrastructure:** Custom static-HTML CMS with per-floor-plan URLs (`/floor-plan/{beds}-bedroom/{slug}.html`)
- **Findings:** Each floor plan = its own static HTML page; no API; per-plan navigation needed
- **Verdict:** Generic extractor needs per-plan-link discovery + HTML parser. Pattern: `/floor-plan/{beds}-bedroom/{slug}.html`.

## 11. www.rentthebeachhouseapts.com
- **Type:** Single property, Newport Beach CA
- **PMS infrastructure:** **On-Site.com** (RealPage subsidiary) — 404 page reveals "On-Site.com"
- **Findings:** URL path is `/floor_plans` (UNDERSCORE) — non-standard variant; static HTML page; data inline
- **Verdict:** On-Site.com is a NEW PMS the classifier missed. Also: add `/floor_plans` to the URL-variant list in the anchor-discovery code. Two findings here.

## 12. www.riverloftapartments.com
- **Type:** Single property, Philadelphia PA
- **PMS infrastructure:** **SAME custom static-HTML CMS as foxchaseofalexandriaapts.com** — per-plan URLs `/floor-plan/{beds}-bedroom/{slug}.html`
- **Findings:** Identical URL pattern + page structure. Cluster!
- **Verdict:** Confirmed cluster of 2+ properties on the same custom CMS. The pattern `/floor-plan/{beds-class}/{plan-slug}.html` is reusable.

---

# Cross-property cluster findings

## Re-clustered after Chrome probing

| Cluster | Properties | Action |
|---|---:|---|
| **_fp-renderable CMS** (originally vineyardscartersville + therailat1380 from HAR + livegrammercy from probe) | **3** | Build adapter route for `/_fp-renderable/params%3Ainstance%3D...` |
| **`/floor-plan/{beds}-bedroom/{slug}.html` custom CMS** (foxchase + riverloft from probes) | **2** | New per-plan link follower; static HTML parser |
| **iloveleasing.com `luv.js` widget** (jalexrentals — multi-property mgmt co) | 1+ | NEW PMS not in classifier; needs per-property URLs to test |
| **On-Site.com** (rentthebeachhouseapts — RealPage subsidiary) | 1+ | NEW PMS not in classifier; `/floor_plans` (underscore) path variant |
| Site123 (cottonwoodcreek) | 1 | Generic CMS — HTML extractor improvement |
| Wix (crossingsmadison) | 1 | Existing wix/squarespace_nopms handler |
| WordPress + Elementor (terrainaustin, brooklane, tenzen) | 3 | Static images / inline HTML; LLM or vision tier |
| RealPage CmsSiteManager (townecrest — confirmed) | 1 | Existing adapter; debug routing |
| Management-company landing pages (allenproperties, jalexrentals) | 2 | Need per-property URL list (not single-prop scrapeable) |

## New PMS classifiers to add

Based on Chrome probing, the following PMS identifiers should be added to `_PMS_MARKERS`:

1. **iloveleasing.com** — `www.iloveleasing.com/pub/widget/js/luv.js`
2. **On-Site.com** — page titles/error pages with "On-Site.com" branding (RealPage subsidiary)
3. **_fp-renderable CMS** — URL path `/floorplans/_fp-renderable/params%3Ainstance%3D...`
4. **Sierra.chat** — chat widget (not a PMS but appears as funnel-confusion)
5. **Site123** — `cdn-cms-s-8-4.f-static.net` CDN

## New URL-variant paths to add to anchor-discovery

- `/floor_plans` (UNDERSCORE) — rentthebeachhouseapts.com
- `/floor-plan/{beds}-bedroom/{slug}.html` — per-plan deep paths (foxchase, riverloft)
