# RentCafe direct-path — Phase 0 research

**Status:** Placeholder. The aggregator endpoint URLs and response shapes
hard-coded in `ma_poc/pms/rentcafe_direct/{propertyid_resolver,fetcher}.py`
must be **verified against live RentCafe.com behavior** before the
direct path is enabled in production. This document is the venue for
that verification — it should be filled in alongside the live diagnostic
work.

## Required sections (to be filled in)

### 1. Sample properties (≥5)

For each: `id`, `name`, `city`, `zip`, `vanity_domain`, `propertyId`.

| canonical_id | name | city | zip | vanity_domain | propertyId |
|---|---|---|---|---|---|
| _TBD_ | _TBD_ | _TBD_ | _TBD_ | _TBD_ | _TBD_ |
| _TBD_ | _TBD_ | _TBD_ | _TBD_ | _TBD_ | _TBD_ |
| _TBD_ | _TBD_ | _TBD_ | _TBD_ | _TBD_ | _TBD_ |
| _TBD_ | _TBD_ | _TBD_ | _TBD_ | _TBD_ | _TBD_ |
| _TBD_ | _TBD_ | _TBD_ | _TBD_ | _TBD_ | _TBD_ |

### 2. Search endpoint

- **Method:** _TBD_
- **URL:** _TBD_  (currently hard-coded as `https://www.rentcafe.com/api/search`)
- **Required params:** _TBD_ (e.g. `q`, `location`, `lat`, `lng`)
- **Response shape:**
  ```json
  // _TBD_ — paste a real captured payload
  ```
- **Authentication required?** _TBD_

### 3. Floorplans endpoint

- **URL pattern:** _TBD_  (currently hard-coded as `https://www.rentcafe.com/wp-json/middleware/v1/getFloorplans/?propertyId%5B%5D={pid}`)
- **Envelope shape:** matches existing `ma_poc/pms/adapters/rentcafe.py`
  `_RENTCAFE_WRAPPER_KEYS` / `_RENTCAFE_WRAPPER_KEYS_L2` (any of: root
  list, `{data: []}`, `{Result: []}`, `{response: {result: []}}`).
- **Auth required?** _TBD_

### 4. Disambiguation strategy validation

Test by querying a name that occurs in multiple zips — confirm the
`(name + zip)` combination is enough to uniquely resolve. Document at
least one such collision found in the production set.

| name | zip A | propertyId A | zip B | propertyId B |
|---|---|---|---|---|
| _TBD_ | _TBD_ | _TBD_ | _TBD_ | _TBD_ |

### 5. Known failure modes

Document any properties where:
- The aggregator search returns no result despite the property being a
  legitimate RentCafe site.
- The floorplans endpoint returns a different envelope shape from the
  ones above.
- The aggregator itself is Cloudflare-protected from production egress.
  (See §8.8 — if F2 verdict is `IP_REPUTATION`, the rentcafe_direct
  premise is undermined; document this finding here so the next
  reviewer doesn't repeat the work.)

## How to capture the data

1. Open https://www.rentcafe.com in a browser, with DevTools Network
   tab filtering on `XHR` / `fetch`.
2. Search for a known property (e.g. one listed in
   `data/runs/<latest>/bot_blocked_properties_latest.json`).
3. Capture the search request URL + headers + payload + response.
4. Click into the property; capture the floorplans request similarly.
5. Replay both requests via `curl` and `httpx` to confirm they work
   without browser-only headers (cookies, custom origin etc.).
6. Repeat for ≥5 properties spanning at least 2 zip codes and 2
   management companies.

If any step fails (auth required, Cloudflare wall, etc.), the spec
premise is invalidated — document the failure here and flag F4-F7 as
blocked before merging.
