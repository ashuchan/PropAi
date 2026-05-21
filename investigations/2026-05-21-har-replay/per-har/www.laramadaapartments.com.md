# www.laramadaapartments.com
Verdict: **covered_by_existing_adapter:knock.units**

## HAR summary
- size: 14,594,560 bytes
- entries: 110
- pms_signals: `['apts247', 'knock']`
- candidate unit-data responses: 2
- top hosts:
  - 23× `www.laramadaapartments.com`
  - 23× `static2.apts247.info`
  - 17× `doorway-api.knockrentals.com`
  - 10× `fonts.gstatic.com`
  - 6× `thumbs.apts247.info`
  - 5× `www.gstatic.com`

### Top candidates

**1.** (knock.units) GET `https://doorway-api.knockrentals.com/v1/property/2014927/units`
- signal_kind: `json_api` score=106
- shape: `{'rent_keys': 14, 'unit_keys': 21, 'sqft_keys': 18, 'bed_keys': 18, 'avail_keys': 7, 'html_rents': 1, 'html_beds': 18, 'html_sqft': 0, 'jsonld': 0}`
- content_type: `application/json`
- key headers: `{'content-type': 'application/json', 'origin': 'https://www.laramadaapartments.com', 'referer': 'https://www.laramadaapartments.com/'}`

**2.** (custom.api_v3_floorplans) GET `https://www.laramadaapartments.com/api/v3/floorplans/all/?api_key=36a1ad6c21c06b6f88dfeede75fd766f22584c86`
- signal_kind: `json_api` score=60
- shape: `{'rent_keys': 8, 'unit_keys': 8, 'sqft_keys': 0, 'bed_keys': 8, 'avail_keys': 2, 'html_rents': 12, 'html_beds': 24, 'html_sqft': 0, 'jsonld': 0}`
- content_type: `application/json`
- key headers: `{'referer': 'https://www.laramadaapartments.com/floorplans/'}`
