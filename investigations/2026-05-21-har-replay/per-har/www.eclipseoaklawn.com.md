# www.eclipseoaklawn.com
Verdict: **covered_by_existing_adapter:knock.units**

## HAR summary
- size: 11,308,056 bytes
- entries: 103
- pms_signals: `['apts247', 'knock']`
- candidate unit-data responses: 2
- top hosts:
  - 25× `static2.apts247.info`
  - 19× `www.eclipseoaklawn.com`
  - 13× `doorway-api.knockrentals.com`
  - 8× `thumbs.apts247.info`
  - 5× `www.gstatic.com`
  - 4× `fonts.gstatic.com`

### Top candidates

**1.** (knock.units) GET `https://doorway-api.knockrentals.com/v1/property/2027331/units`
- signal_kind: `json_api` score=157
- shape: `{'rent_keys': 26, 'unit_keys': 39, 'sqft_keys': 27, 'bed_keys': 27, 'avail_keys': 13, 'html_rents': 0, 'html_beds': 0, 'html_sqft': 0, 'jsonld': 0}`
- content_type: `application/json`
- key headers: `{'content-type': 'application/json', 'origin': 'https://www.eclipseoaklawn.com', 'referer': 'https://www.eclipseoaklawn.com/'}`

**2.** (custom.api_v3_floorplans) GET `https://www.eclipseoaklawn.com/api/v3/floorplans/all/?api_key=7b64d3548c88b7f985c7cd88d9b608ae4989f2e4`
- signal_kind: `json_api` score=118
- shape: `{'rent_keys': 19, 'unit_keys': 19, 'sqft_keys': 0, 'bed_keys': 19, 'avail_keys': 11, 'html_rents': 45, 'html_beds': 27, 'html_sqft': 0, 'jsonld': 0}`
- content_type: `application/json`
- key headers: `{'referer': 'https://www.eclipseoaklawn.com/floorplans/'}`
