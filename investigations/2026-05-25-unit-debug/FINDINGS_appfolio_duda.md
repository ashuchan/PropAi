# AppFolio /availability sub-page fallback — deep-probe 2026-05-25

## TL;DR

User-flagged Cluster C ("AppFolio vanity sites where the existing adapter
visits `/listings` but returns 0 units, yet the vanity site has unit data
at `/availability`") turned out to be a different shape than the original
hypothesis:

- The seed property **SCS Athens** (https://www.livescs.com/property/scs-athens)
  is NOT a standard AppFolio vanity site. The only `appfolio.com` string
  on the page is a footer "Terms" link, so the existing slug-vanity fallback
  finds no slug and exits with 0 units (tier_used stays at the default
  `TIER_1_API_APPFOLIO`).
- The site is built on **Duda CMS** and uses the **AppFolio Websites**
  product (a separate AppFolio offering from the listings widget). The
  page injects `https://cdn.appfoliowebsites.com/sites/resources/js/appfolio-global-scripts.js`
  as a definitive marker.
- The listings widget renders client-side from a **Duda public-collection
  REST API** at:
  ```
  https://{host}/rts/collections/public/{site_id}/runtime/collection/appfolio-listings/query-data?pageSize=100&pageNumber={N}&query=()&language=ENGLISH
  ```
- Each record carries the SAME AppFolio fields the standard listings API
  returns (`market_rent`, `bedrooms`, `bathrooms`, `square_feet`,
  `available`, `available_date`, `full_address`, `listable_uid`,
  `unit_template_name`, `property_lists`).
- Per-property pages scope the widget with a `propertyGroup` config value
  that lives in a base64-encoded JSON binding payload. The widget filters
  the collection client-side by matching `propertyGroup` (case-insensitive)
  against entries in each listing's `property_lists[].name`.

## Cohort sizing

Random sample of 80 properties from `properties.csv` → **6 hits = 7.5%**.
All 6 served a valid collection API:

| Property | Site ID | Listings |
|---|---|---|
| SCS Athens (livescs) | 3885d159 | 256 total (28 at Athens, ~4 filtered to "SCS Athens") |
| Parkview at Spring Hill | 73a8fee9 | 37 |
| Beaumont Cove | cbad8b42 | 56 |
| Wind Chase (pearlinvestment) | 54e77a2f | 17 |
| Live at the Biltmore | 2d96882f | 4 |
| Mall Apartments | 262a2f0f | 3 |

Extrapolated to the full CSV (4,981 rows) at the same hit rate: ~370
properties on AppFolio Websites CMS — many of them currently scoring
n_full=0 because the existing adapter has no path to the Duda collection
endpoint.

## Step 1 — Verify

Probed three URL shapes per seed:

| URL | SCS Athens | Notes |
|---|---|---|
| `/property/scs-athens` | 200 (297 KB) | Marketing page, propertyGroup="SCS Athens" |
| `/property/scs-athens/availability` | 404 | Per-property sub-page does NOT exist |
| `/availability` | 200 (214 KB) | Site-wide page, propertyGroup="" (no filter) |

So the original framing ("unit data at /availability") was approximately
right — but the data isn't in the SSR HTML, it's in the client-side XHR.
The HTML at both `/property/scs-athens` and `/availability` carries the
SAME widget config; only the `propertyGroup` value differs (per-property
vs site-wide).

Confirmed via Playwright that opening `/availability` fires three
`/rts/collections/public/3885d159/runtime/collection/appfolio-listings/query-data?...&pageNumber=0|1|2`
requests (3 pages × 100 each, 256 total). Direct curl_cffi
(chrome120 impersonation) to that endpoint succeeds without auth, no
proxy needed.

## Step 2 — Identify the pattern

This is shape (B) per the original task framing: **a separate CMS**
(Duda) republishing AppFolio data via its own collections API. NOT
shape (A) (SSR HTML of the same `/listings` data) — the SSR HTML
contains zero unit rows; the widget JS bundle is inlined but the data
is loaded post-render.

How the widget gets its config & data:
1. Page HTML embeds the Duda widget JS bundle (the AppFolio Listing
   widget code, minified and inlined via Duda's custom-widget system).
2. Widget config (`propertyGroup`, filter flags, etc.) lives in a
   base64-encoded JSON blob inside Duda's binding/data attributes
   on the page.
3. Site id (e.g. `3885d159`) is the Duda site identifier; visible in
   every `irp.cdn-website.com/{site_id}/...` or
   `lirp.cdn-website.com/{site_id}/...` asset URL.
4. Widget calls Duda's public collection endpoint to fetch all
   `appfolio-listings` records, then filters client-side by
   `propertyGroup` matching `property_lists[].name`.

The collection is the SAME AppFolio data the operator's
`{slug}.appfolio.com/listings` would return (the `database_name` field
on each record confirms — e.g. `scswiderski`, the SCS AppFolio slug).
So this is the AppFolio data, but served via a different delivery
channel that the existing adapter has no path into.

## Step 3 — Ship

New helper module + wiring into `AppFolioAdapter.extract`:

- `ma_poc/pms/adapters/_appfolio_websites_duda.py`
  - `is_appfolio_websites_cms(html)` — checks for the
    `cdn.appfoliowebsites.com/sites/resources/` loader.
  - `extract_duda_site_id(html)` — pulls the hex site id from
    `(irp|lirp|static).cdn-website.com/{id}/`.
  - `extract_appfolio_websites_property_group(html)` — decodes every
    long base64 token on the page; returns the first `propertyGroup`
    string from a JSON object (NOT from a binding-list payload that
    only references a binding path).
  - `parse_appfolio_websites_listing(record, url)` — converts one
    collection record into a standard unit dict (`make_unit_dict`),
    tier `TIER_1_API_APPFOLIO_DUDA`. Drops dim-less rows up-front to
    match the downstream validity gate.
  - `parse_collection_payload(payload, url, property_group)` — full
    page → unit list + total_pages, with case-insensitive
    `property_lists` filter when a `property_group` is supplied.
  - `collection_url(host, site_id, page_number)` + `origin_from_url(url)`.

- `ma_poc/pms/adapters/appfolio.py`
  - New sub-tier inserted AFTER `data-listing-id` SSR / detail-page
    paths and BEFORE the slug-vanity fallback. It fires only when the
    AppFolio Websites loader marker is present, so bare AppFolio
    vanity pages (where slug-vanity already handles it) are
    untouched.
  - Paginates with a defensive 10-page cap. Records errors as
    `appfolio-websites-duda-error: site_id=... property_group=... <ExcType>`
    so failures are visible in the run report instead of silent.

### Coordination with adjacent chips

- **Chip #100 (propertyGroup filter for multi-property PMC vanity)** —
  ORTHOGONAL. Touches the slug-vanity path (`{slug}.appfolio.com/listings?filters[property_list]=...`),
  which the new Duda path runs BEFORE only when the AppFolio Websites
  marker is present. No code shared, no path overlap.
- **Chip #107 (Academy Place address filter)** — also ORTHOGONAL for
  the same reason. The Duda path returns first when the marker is
  present; the slug-vanity path is only reached when the marker is
  absent.

## Tests

`ma_poc/tests/pms/adapters/test_appfolio_websites_duda.py` — 30 tests,
all passing:

- Marker detection (3 tests; positive on live SCS Athens fixture,
  negative on bare AppFolio terms link, empty input).
- Duda site-id extraction (3 tests; live fixture, lirp subdomain, no
  Duda assets).
- Property-group extraction from base64 bindings (4 tests; live SCS
  Athens "SCS Athens", site-wide null, binding-path-shape rejection,
  empty input).
- Property-group matching (4 tests; case-insensitive, no match, None
  passes through, no `property_lists` field).
- Listing parser (4 tests; happy path with all fields, unavailable
  status, dim-less drop, bad input).
- Collection payload (4 tests; unfiltered, SCS Athens filter, no
  matches, malformed payload).
- URL helpers (3 tests).
- End-to-end adapter (5 tests; SCS Athens live HTML, multi-page
  pagination, skipped when no marker, error logging on exception,
  Beaumont Cove second cohort sample).

Live HTML fixtures committed:

- `fixtures/appfolio_websites_duda/scs_athens_property.html` (297 KB)
- `fixtures/appfolio_websites_duda/scs_collection_page0.json` (10 listings)
- `fixtures/appfolio_websites_duda/beaumont_cove_home.html` (174 KB)
- `fixtures/appfolio_websites_duda/beaumont_collection_page0.json` (5 listings)

## What this does NOT fix

- Properties that have the AppFolio Websites loader BUT serve no
  listings (e.g. a brand-new operator with `totalItems=0`). The
  adapter records `appfolio-websites-duda-error` only on HTTP errors
  / exceptions, not on legitimately empty inventory — those fall
  through to the slug-vanity path, which will also return 0.
- Operators that publish their AppFolio listings via a non-Duda
  marketing CMS (e.g. WordPress + a custom widget). Out of scope for
  this chip.

## Open follow-ups

- The Duda-CMS marker detection (`is_appfolio_websites_cms`) could
  also feed into the detector to bump confidence on the AppFolio
  routing for these sites — currently they route to AppFolio via the
  weak "appfolio.com" string match (footer terms link), which is a
  pass-3 0.80 marker. A pass-2 0.92 marker on the explicit AppFolio
  Websites loader would let the orchestrator prefer this adapter
  over generic / LLM fallbacks even when the page is otherwise a
  Wix/Squarespace shell. NOT changed here to keep the chip scoped.
