# www.amherstbedford.com
Verdict: **covered_by_existing_adapter:knock.units**

## HAR summary
- size: 16,659,686 bytes
- entries: 158
- pms_signals: `['apts247', 'knock']`
- candidate unit-data responses: 2
- top hosts:
  - 25× `static2.apts247.info`
  - 18× `www.amherstbedford.com`
  - 15× `doorway-api.knockrentals.com`
  - 13× `www.google.com`
  - 8× `tags.srv.stackadapt.com`
  - 8× `thumbs.apts247.info`

### Top candidates

**1.** (knock.units) GET `https://doorway-api.knockrentals.com/v1/property/2030359/units`
- signal_kind: `json_api` score=174
- shape: `{'rent_keys': 30, 'unit_keys': 45, 'sqft_keys': 24, 'bed_keys': 24, 'avail_keys': 15, 'html_rents': 0, 'html_beds': 0, 'html_sqft': 0, 'jsonld': 0}`
- content_type: `application/json`
- key headers: `{'content-type': 'application/json', 'origin': 'https://www.amherstbedford.com', 'referer': 'https://www.amherstbedford.com/'}`

**2.** (custom.api_v3_floorplans) GET `https://www.amherstbedford.com/api/v3/floorplans/all/?api_key=272616b0ece52ed18b08e66e78e351e36f744481`
- signal_kind: `json_api` score=111
- shape: `{'rent_keys': 21, 'unit_keys': 13, 'sqft_keys': 0, 'bed_keys': 21, 'avail_keys': 13, 'html_rents': 42, 'html_beds': 29, 'html_sqft': 0, 'jsonld': 0}`
- content_type: `application/json`
- key headers: `{'referer': 'https://www.amherstbedford.com/floorplans/'}`
