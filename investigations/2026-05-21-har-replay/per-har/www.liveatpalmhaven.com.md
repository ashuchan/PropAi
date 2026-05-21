# www.liveatpalmhaven.com
Verdict: **covered_by_existing_adapter:knock.units**

## HAR summary
- size: 14,874,278 bytes
- entries: 153
- pms_signals: `['knock', 'rentcafe']`
- candidate unit-data responses: 1
- top hosts:
  - 52× `cdngeneralmvc.rentcafe.com`
  - 19× `resource.rentcafe.com`
  - 17× `doorway-api.knockrentals.com`
  - 9× `app.meetelise.com`
  - 7× `www.googletagmanager.com`
  - 4× `www.google-analytics.com`

### Top candidates

**1.** (knock.units) GET `https://doorway-api.knockrentals.com/v1/property/2024406/units`
- signal_kind: `json_api` score=1184
- shape: `{'rent_keys': 182, 'unit_keys': 273, 'sqft_keys': 145, 'bed_keys': 145, 'avail_keys': 91, 'html_rents': 0, 'html_beds': 129, 'html_sqft': 0, 'jsonld': 0}`
- content_type: `application/json`
- key headers: `{'content-type': 'application/json', 'origin': 'https://www.liveatpalmhaven.com', 'referer': 'https://www.liveatpalmhaven.com/floorplans'}`
