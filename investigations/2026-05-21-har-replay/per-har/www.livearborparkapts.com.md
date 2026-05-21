# www.livearborparkapts.com
Verdict: **covered_by_existing_adapter:knock.units**

## HAR summary
- size: 13,677,041 bytes
- entries: 124
- pms_signals: `['knock', 'realpage']`
- candidate unit-data responses: 2
- top hosts:
  - 16× `doorway-api.knockrentals.com`
  - 13× `use.typekit.net`
  - 12× `cdn.cookielaw.org`
  - 7× `www.google.com`
  - 6× `inventory.g5marketingcloud.com`
  - 6× `www.google-analytics.com`

### Top candidates

**1.** (knock.units) GET `https://doorway-api.knockrentals.com/v1/property/2028448/units`
- signal_kind: `json_api` score=114
- shape: `{'rent_keys': 20, 'unit_keys': 30, 'sqft_keys': 14, 'bed_keys': 14, 'avail_keys': 10, 'html_rents': 0, 'html_beds': 0, 'html_sqft': 0, 'jsonld': 0}`
- content_type: `application/json`
- key headers: `{'content-type': 'application/json', 'origin': 'https://www.livearborparkapts.com', 'referer': 'https://www.livearborparkapts.com/'}`

**2.** (novel) GET `https://www.livearborparkapts.com/apartments/tx/hurst/floor-plans`
- signal_kind: `html_inline` score=10
- shape: `{'rent_keys': 0, 'unit_keys': 0, 'sqft_keys': 2, 'bed_keys': 2, 'avail_keys': 0, 'html_rents': 0, 'html_beds': 3, 'html_sqft': 0, 'jsonld': 1}`
- content_type: `text/html`
- key headers: `{'referer': 'https://www.livearborparkapts.com/'}`
