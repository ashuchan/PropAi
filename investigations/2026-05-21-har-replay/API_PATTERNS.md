# API endpoints discovered in the HAR archive → integration plan

Date: 2026-05-21
Source: `/Users/ankur/Downloads/HAR FILES.zip` (133 HARs across the `T2_LLM_only` cohort)

## Approach

Mined all 133 HARs for response bodies with strong unit-data signal (rent + floor-plan/unit shape), then **clustered by parametrized URL template** (numeric/hex/UUID property IDs generalized to `{VALUE}`). Each cluster is a candidate adapter route — implementing one covers **every property on that PMS class**, not just the ones in this archive.

## Headline findings

| | Count |
|---|---:|
| Unit-data responses found | 79 |
| Distinct URL templates after parametrization | 62 |
| Templates matching an EXISTING adapter | 6 |
| **NOVEL templates with ≥2 properties** (high-ROI integration targets) | **2** |
| Novel single-property templates (mostly the property's own /floorplans HTML — handled by generic JSON-LD tier) | 54 |

## The two actionable findings

### 1. **Knock `/v1/property/{id}/units`** — 10 properties, validated LIVE

```
GET https://doorway-api.knockrentals.com/v1/property/{property_id}/units
Headers: Accept: */*
         Origin: https://<property_domain>
No cookies. No auth. No CSRF token.
```

**Response:** clean JSON, ~10-20KB per property:
```json
{
  "units_data": {
    "units": [
      {"name": "602", "bedrooms": 2, "bathrooms": 2, "area": 1050,
       "price": "2077", "displayPrice": "2077",
       "available": true, "availableOn": "2026-04-06",
       "propertyId": 2023409, "layoutId": "0e3d98ac-...",
       "layoutName": "2 Bed 2 Bath", ...}
    ],
    "layouts": [
      {"name": "2 Bed 1 Bath Partial Reno", "bedrooms": 2, "bathrooms": 1,
       "area": 1050, "images": [...], ...}
    ],
    "buildings": [...],
    "status_code": "ok"
  }
}
```

**Live test confirmation (just now):** `curl_cffi.get(url, headers={"Origin": ..., "Accept": "*/*"}, impersonate="chrome131")` returned 200 + 4 units from `lakeviewatnewcastle.com` (propertyId=2023409). ~1s latency, $0 cost.

**Properties using this endpoint in the HAR archive (10):**
- crossdaleflatsapts.com
- james-towne-village.com
- lakeviewatnewcastle.com
- laramadaapartments.com
- liveatpalmhaven.com
- ...

**Why this is high-value:**
- Existing `knock` adapter uses different endpoints (community / property metadata). The `/units` endpoint returns the actual unit-level data we want.
- Knock-by-domain resolver (shipped recently) already discovers the property ID from homepage HTML — feeds directly into the URL.
- ~30+ Knock-detected properties exist across the failure cohorts; this could move most of them from T2_LLM_only to a deterministic Tier-1 route.

### 2. **Spherexx `/api/unit` (session flow)** — 2 properties

Multi-call session-based flow:
```
1. GET /convert.asp?key=<base64>           — bootstraps the session (key encodes propertyId)
2. POST /api/authenticate                   — creates session cookie
3. GET /api/community                       — property metadata
4. GET /api/configuration                   — per-property config
5. GET /api/unit                            — units array
6. GET /api/floorplan                       — floor plans array
```

**Response from `/api/unit`** — array of units with `ID, Name, Number, Sqft, Bed, Bath, Floor, Price, PriceMin, PriceMax, EffectivePrice, AvailableDate, Amenity[], FloorplanName, ApplicationLinkURL` — very complete unit-level shape.

**Properties using this in HAR archive (2):**
- invitationalapartments.com
- westonplaceapartments.com

**Implementation complexity:** higher than Knock (multi-step session). Estimated 250-400 lines vs 100-150 for Knock.

## Why the single-property "novel" templates aren't actionable as APIs

54 of the 56 novel templates are single-property. Looking at top hosts:
- The property's own `/floorplans` HTML page (RentCafe / Knock embedded data on `*.com/floorplans` HTML)
- Property's own `/Apartments/module/property_info/` (Entrata embedded page; classifier missed)
- `fa-chatbot-prod-e418.azurewebsites.net` × 3 (Funnel leasing chatbot — different shape)
- One-off URLs on individual property domains

These aren't shared APIs — they're property-specific HTML pages with inline unit data. The codebase's generic JSON-LD / embedded-JSON tier already handles this shape. No new adapter needed.

## Why the 6 "known adapter" clusters appear as 1-property each

My parametrizer kept distinct cache-buster timestamps (`?_=1716284491831`) as distinct templates. The RealPage CmsSiteManager and Sightmap endpoints recur across the archive, but each call has a unique timestamp. A second clustering pass that strips cache-busters would consolidate these into 2-3 known-adapter clusters with N properties each — confirming our existing adapters cover that traffic.

## Recommended implementation plan

### Phase 5.1 — Knock `/v1/property/{id}/units` adapter route (high ROI)

**Scope:** ~150 lines + tests.

1. **Property-ID discovery** — extend the existing `knock-by-domain` resolver to also surface the `propertyId` integer. The resolver currently extracts a "community hash"; the `propertyId` is in adjacent SSR markup. One regex pass over the homepage HTML.
2. **Adapter route** — new function in `ma_poc/pms/adapters/knock.py`:
   ```python
   async def fetch_knock_units(property_id: int, origin: str) -> dict:
       url = f"https://doorway-api.knockrentals.com/v1/property/{property_id}/units"
       r = await probe_get(url, headers={"Accept": "*/*", "Origin": origin})
       if r.status_code != 200:
           return {"units": [], "layouts": []}
       return r.json().get("units_data", {})
   ```
3. **Parser** — map Knock response fields (`name`, `area`, `price`, `availableOn`, `bedrooms`, `bathrooms`, `layoutName`) into the unit-dict shape. Most fields are direct 1:1.
4. **Wiring** — register as a new tier-1 route in the Knock adapter; fires when `knock` is detected AND the property_id is resolvable.
5. **Feature flag** — `ENABLE_KNOCK_UNITS_API=true` (default off until measured on a canary).
6. **Tests** — fixture-based parser tests using [knock_units_www.lakeviewatnewcastle.com.json](investigations/2026-05-21-har-replay/fixtures/knock_units_www.lakeviewatnewcastle.com.json) (saved from the HAR).

**Expected coverage uplift:** ~30+ properties across failure cohorts that currently hit Tier-4 LLM extraction → switch to Tier-1 deterministic.

### Phase 5.2 — Spherexx session-flow adapter (medium ROI)

**Scope:** ~300 lines + tests.

1. **Discover the `key=` parameter** in the property's homepage HTML (the team's HAR shows it embedded in an iframe URL on the property's own domain).
2. **Session bootstrap** — call `/convert.asp?key=...`, capture session cookie.
3. **Authenticate** — `POST /api/authenticate` with the session.
4. **Fetch units + floorplans** — `GET /api/unit` and `GET /api/floorplan` with the session.
5. **Wire into existing Spherexx adapter** (the codebase has one; this extends it with the presentation-app flow).
6. **Feature flag** — `ENABLE_SPHEREXX_PRESENTATION=true`.

**Expected coverage uplift:** ~5-15 Spherexx properties across the failure cohorts.

### NOT worth doing

- **Per-property HAR-replay manifests** — was the original idea before this survey. Replaced by the adapter-route approach because (a) it generalizes to all properties on a PMS class, (b) no per-property maintenance, (c) the captured data isn't reused, only the URL recipe.

## Artifacts

- [api_patterns.json](investigations/2026-05-21-har-replay/api_patterns.json) — all 62 URL-template clusters with property samples + response-shape metadata
- [fixtures/knock_units_www.lakeviewatnewcastle.com.json](investigations/2026-05-21-har-replay/fixtures/knock_units_www.lakeviewatnewcastle.com.json) — live Knock response, drop-in for parser tests
- [fixtures/spherexx_units_www.invitationalapartments.com.json](investigations/2026-05-21-har-replay/fixtures/spherexx_units_www.invitationalapartments.com.json) — live Spherexx response
- [index.json](investigations/2026-05-21-har-replay/index.json) — original per-HAR catalogue
- `raw/` — original HAR files (gitignored)
