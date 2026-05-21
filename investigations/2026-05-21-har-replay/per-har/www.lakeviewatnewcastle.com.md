# www.lakeviewatnewcastle.com
Verdict: **covered_by_existing_adapter:knock.units**

## HAR summary
- size: 21,188,589 bytes
- entries: 212
- pms_signals: `['knock', 'rentcafe', 'sightmap']`
- candidate unit-data responses: 1
- top hosts:
  - 49× `cdngeneralmvc.rentcafe.com`
  - 21× `static.canva.com`
  - 17× `doorway-api.knockrentals.com`
  - 14× `font-public.canva.com`
  - 11× `www.google.com`
  - 10× `cdn.cookielaw.org`

### Top candidates

**1.** (knock.units) GET `https://doorway-api.knockrentals.com/v1/property/2023409/units`
- signal_kind: `json_api` score=86
- shape: `{'rent_keys': 10, 'unit_keys': 15, 'sqft_keys': 18, 'bed_keys': 18, 'avail_keys': 5, 'html_rents': 0, 'html_beds': 18, 'html_sqft': 0, 'jsonld': 0}`
- content_type: `application/json`
- key headers: `{'content-type': 'application/json', 'origin': 'https://www.lakeviewatnewcastle.com', 'referer': 'https://www.lakeviewatnewcastle.com/floorplans'}`
