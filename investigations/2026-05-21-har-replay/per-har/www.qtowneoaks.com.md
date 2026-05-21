# www.qtowneoaks.com
Verdict: **covered_by_existing_adapter:knock.units**

## HAR summary
- size: 12,069,037 bytes
- entries: 142
- pms_signals: `['knock', 'rentcafe']`
- candidate unit-data responses: 2
- top hosts:
  - 50× `cdngeneralmvc.rentcafe.com`
  - 19× `resource.rentcafe.com`
  - 16× `doorway-api.knockrentals.com`
  - 6× `www.googletagmanager.com`
  - 4× `www.google-analytics.com`
  - 4× `www.google.com`

### Top candidates

**1.** (knock.units) GET `https://doorway-api.knockrentals.com/v1/property/2013868/units`
- signal_kind: `json_api` score=272
- shape: `{'rent_keys': 38, 'unit_keys': 57, 'sqft_keys': 43, 'bed_keys': 43, 'avail_keys': 19, 'html_rents': 0, 'html_beds': 39, 'html_sqft': 0, 'jsonld': 0}`
- content_type: `application/json`
- key headers: `{'content-type': 'application/json', 'origin': 'https://www.qtowneoaks.com', 'referer': 'https://www.qtowneoaks.com/floorplans'}`

**2.** (novel) GET `https://www.qtowneoaks.com/floorplans`
- signal_kind: `html_inline` score=120
- shape: `{'rent_keys': 0, 'unit_keys': 0, 'sqft_keys': 0, 'bed_keys': 0, 'avail_keys': 0, 'html_rents': 11, 'html_beds': 24, 'html_sqft': 93, 'jsonld': 0}`
- content_type: `text/html`
- key headers: `{'Referer': 'https://www.qtowneoaks.com/'}`
