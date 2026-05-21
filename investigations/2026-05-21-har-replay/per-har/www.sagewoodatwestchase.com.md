# www.sagewoodatwestchase.com
Verdict: **covered_by_existing_adapter:knock.units**

## HAR summary
- size: 10,916,444 bytes
- entries: 147
- pms_signals: `['knock', 'rentcafe']`
- candidate unit-data responses: 2
- top hosts:
  - 49× `cdngeneralmvc.rentcafe.com`
  - 16× `doorway-api.knockrentals.com`
  - 14× `resource.rentcafe.com`
  - 13× `app.meetelise.com`
  - 7× `my.hy.ly`
  - 6× `analytics.google.com`

### Top candidates

**1.** (knock.units) GET `https://doorway-api.knockrentals.com/v1/property/2023945/units`
- signal_kind: `json_api` score=440
- shape: `{'rent_keys': 70, 'unit_keys': 105, 'sqft_keys': 64, 'bed_keys': 64, 'avail_keys': 35, 'html_rents': 0, 'html_beds': 0, 'html_sqft': 26, 'jsonld': 0}`
- content_type: `application/json`
- key headers: `{'content-type': 'application/json', 'origin': 'https://www.sagewoodatwestchase.com', 'referer': 'https://www.sagewoodatwestchase.com/floorplans'}`

**2.** (novel) GET `https://www.sagewoodatwestchase.com/floorplans`
- signal_kind: `html_inline` score=60
- shape: `{'rent_keys': 0, 'unit_keys': 0, 'sqft_keys': 0, 'bed_keys': 0, 'avail_keys': 0, 'html_rents': 21, 'html_beds': 26, 'html_sqft': 27, 'jsonld': 0}`
- content_type: `text/html`
- key headers: `{'referer': 'https://www.sagewoodatwestchase.com/'}`
