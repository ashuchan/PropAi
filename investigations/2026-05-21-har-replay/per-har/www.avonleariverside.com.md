# www.avonleariverside.com
Verdict: **covered_by_existing_adapter:knock.units**

## HAR summary
- size: 15,009,444 bytes
- entries: 130
- pms_signals: `['g5', 'knock', 'realpage']`
- candidate unit-data responses: 2
- top hosts:
  - 19× `doorway-api.knockrentals.com`
  - 12× `cdn.cookielaw.org`
  - 11× `www.google.com`
  - 8× `www.googletagmanager.com`
  - 7× `www.google-analytics.com`
  - 7× `fonts.gstatic.com`

### Top candidates

**1.** (knock.units) GET `https://doorway-api.knockrentals.com/v1/property/2004748/units`
- signal_kind: `json_api` score=115
- shape: `{'rent_keys': 20, 'unit_keys': 30, 'sqft_keys': 15, 'bed_keys': 15, 'avail_keys': 10, 'html_rents': 0, 'html_beds': 0, 'html_sqft': 0, 'jsonld': 0}`
- content_type: `application/json`
- key headers: `{'content-type': 'application/json', 'origin': 'https://www.avonleariverside.com', 'referer': 'https://www.avonleariverside.com/'}`

**2.** (novel) GET `https://www.avonleariverside.com/apartments/ga/atlanta/floor-plans`
- signal_kind: `html_inline` score=9
- shape: `{'rent_keys': 0, 'unit_keys': 0, 'sqft_keys': 2, 'bed_keys': 2, 'avail_keys': 0, 'html_rents': 0, 'html_beds': 2, 'html_sqft': 0, 'jsonld': 1}`
- content_type: `text/html`
- key headers: `{'referer': 'https://www.avonleariverside.com/'}`
