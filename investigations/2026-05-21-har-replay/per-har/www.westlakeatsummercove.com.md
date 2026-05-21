# www.westlakeatsummercove.com
Verdict: **covered_by_existing_adapter:knock.units**

## HAR summary
- size: 7,499,264 bytes
- entries: 104
- pms_signals: `['knock', 'rentcafe']`
- candidate unit-data responses: 2
- top hosts:
  - 45× `cdngeneralmvc.rentcafe.com`
  - 16× `doorway-api.knockrentals.com`
  - 10× `resource.rentcafe.com`
  - 5× `www.westlakeatsummercove.com`
  - 4× `fonts.gstatic.com`
  - 4× `app.launchdarkly.com`

### Top candidates

**1.** (knock.units) GET `https://doorway-api.knockrentals.com/v1/property/2015774/units`
- signal_kind: `json_api` score=184
- shape: `{'rent_keys': 28, 'unit_keys': 42, 'sqft_keys': 22, 'bed_keys': 22, 'avail_keys': 14, 'html_rents': 0, 'html_beds': 22, 'html_sqft': 0, 'jsonld': 0}`
- content_type: `application/json`
- key headers: `{'content-type': 'application/json', 'origin': 'https://www.westlakeatsummercove.com', 'referer': 'https://www.westlakeatsummercove.com/floorplans'}`

**2.** (novel) GET `https://www.westlakeatsummercove.com/floorplans`
- signal_kind: `html_inline` score=32
- shape: `{'rent_keys': 0, 'unit_keys': 0, 'sqft_keys': 7, 'bed_keys': 0, 'avail_keys': 0, 'html_rents': 6, 'html_beds': 9, 'html_sqft': 14, 'jsonld': 0}`
- content_type: `text/html`
- key headers: `{'Referer': 'https://www.westlakeatsummercove.com/'}`
