# www.maplewoodapthomes.com
Verdict: **covered_by_existing_adapter:knock.units**

## HAR summary
- size: 15,517,770 bytes
- entries: 118
- pms_signals: `['apts247', 'knock']`
- candidate unit-data responses: 2
- top hosts:
  - 26× `static2.apts247.info`
  - 22× `www.maplewoodapthomes.com`
  - 16× `doorway-api.knockrentals.com`
  - 9× `www.gstatic.com`
  - 8× `fonts.gstatic.com`
  - 5× `www.google.com`

### Top candidates

**1.** (knock.units) GET `https://doorway-api.knockrentals.com/v1/property/2014798/units`
- signal_kind: `json_api` score=49
- shape: `{'rent_keys': 6, 'unit_keys': 9, 'sqft_keys': 10, 'bed_keys': 10, 'avail_keys': 3, 'html_rents': 0, 'html_beds': 9, 'html_sqft': 0, 'jsonld': 0}`
- content_type: `application/json`
- key headers: `{'content-type': 'application/json', 'origin': 'https://www.maplewoodapthomes.com', 'referer': 'https://www.maplewoodapthomes.com/'}`

**2.** (custom.api_v3_floorplans) GET `https://www.maplewoodapthomes.com/api/v3/floorplans/all/?api_key=f373e4525047d5dc6e518145e471deac9adc88ff`
- signal_kind: `json_api` score=41
- shape: `{'rent_keys': 7, 'unit_keys': 7, 'sqft_keys': 0, 'bed_keys': 7, 'avail_keys': 3, 'html_rents': 13, 'html_beds': 9, 'html_sqft': 0, 'jsonld': 0}`
- content_type: `application/json`
- key headers: `{'referer': 'https://www.maplewoodapthomes.com/floorplans/'}`
