# www.rentharborcliff.com
Verdict: **actionable_html_extractor**

## HAR summary
- size: 10,670,381 bytes
- entries: 140
- pms_signals: `['knock', 'rentcafe']`
- candidate unit-data responses: 2
- top hosts:
  - 51× `cdngeneralmvc.rentcafe.com`
  - 16× `doorway-api.knockrentals.com`
  - 11× `cdn.cookielaw.org`
  - 9× `resource.rentcafe.com`
  - 5× `www.rentharborcliff.com`
  - 5× `www.googletagmanager.com`

### Top candidates

**1.** (novel) GET `https://www.rentharborcliff.com/floorplans`
- signal_kind: `html_inline` score=89
- shape: `{'rent_keys': 0, 'unit_keys': 10, 'sqft_keys': 17, 'bed_keys': 9, 'avail_keys': 9, 'html_rents': 5, 'html_beds': 51, 'html_sqft': 0, 'jsonld': 0}`
- content_type: `text/html`
- key headers: `{'referer': 'https://www.rentharborcliff.com/'}`

**2.** (knock.units) GET `https://doorway-api.knockrentals.com/v1/property/2012112/units`
- signal_kind: `json_api` score=82
- shape: `{'rent_keys': 12, 'unit_keys': 18, 'sqft_keys': 11, 'bed_keys': 11, 'avail_keys': 6, 'html_rents': 0, 'html_beds': 11, 'html_sqft': 0, 'jsonld': 0}`
- content_type: `application/json`
- key headers: `{'content-type': 'application/json', 'origin': 'https://www.rentharborcliff.com', 'referer': 'https://www.rentharborcliff.com/floorplans'}`
