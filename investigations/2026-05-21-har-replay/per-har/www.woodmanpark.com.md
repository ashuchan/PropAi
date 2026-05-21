# www.woodmanpark.com
Verdict: **actionable_html_extractor**

## HAR summary
- size: 11,535,045 bytes
- entries: 127
- pms_signals: `['knock', 'rentcafe']`
- candidate unit-data responses: 2
- top hosts:
  - 51× `cdngeneralmvc.rentcafe.com`
  - 15× `doorway-api.knockrentals.com`
  - 12× `cdn.cookielaw.org`
  - 8× `resource.rentcafe.com`
  - 5× `www.googletagmanager.com`
  - 4× `fonts.googleapis.com`

### Top candidates

**1.** (novel) GET `https://www.woodmanpark.com/floorplans`
- signal_kind: `html_inline` score=130
- shape: `{'rent_keys': 0, 'unit_keys': 38, 'sqft_keys': 28, 'bed_keys': 23, 'avail_keys': 23, 'html_rents': 8, 'html_beds': 16, 'html_sqft': 8, 'jsonld': 0}`
- content_type: `text/html`
- key headers: `{'Referer': 'https://www.woodmanpark.com/'}`

**2.** (knock.units) GET `https://doorway-api.knockrentals.com/v1/property/2006949/units`
- signal_kind: `json_api` score=20
- shape: `{'rent_keys': 2, 'unit_keys': 3, 'sqft_keys': 5, 'bed_keys': 5, 'avail_keys': 1, 'html_rents': 0, 'html_beds': 5, 'html_sqft': 0, 'jsonld': 0}`
- content_type: `application/json`
- key headers: `{'content-type': 'application/json', 'origin': 'https://www.woodmanpark.com', 'referer': 'https://www.woodmanpark.com/floorplans'}`
