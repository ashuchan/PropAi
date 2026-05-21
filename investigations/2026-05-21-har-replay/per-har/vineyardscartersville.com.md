# vineyardscartersville.com
Verdict: **actionable_new_api**

## HAR summary
- size: 19,779,460 bytes
- entries: 149
- pms_signals: `['knock']`
- candidate unit-data responses: 3
- top hosts:
  - 36× `vineyardscartersville.com`
  - 33× `static.canva.com`
  - 15× `doorway-api.knockrentals.com`
  - 9× `cdn.jonahdigital.com`
  - 9× `app.meetelise.com`
  - 6× `www.canva.com`

### Top candidates

**1.** (novel) GET `https://vineyardscartersville.com/floorplans/_fp-renderable/params%3Ainstance%3D20f4d721f54a51b85d9fed8b7f6d8490%26actio`
- signal_kind: `json_api` score=430
- shape: `{'rent_keys': 38, 'unit_keys': 38, 'sqft_keys': 40, 'bed_keys': 40, 'avail_keys': 40, 'html_rents': 0, 'html_beds': 122, 'html_sqft': 116, 'jsonld': 0}`
- content_type: `text/html`
- key headers: `{'referer': 'https://vineyardscartersville.com/floorplans/'}`

**2.** (knock.units) GET `https://doorway-api.knockrentals.com/v1/property/2015834/units`
- signal_kind: `json_api` score=267
- shape: `{'rent_keys': 48, 'unit_keys': 72, 'sqft_keys': 27, 'bed_keys': 27, 'avail_keys': 24, 'html_rents': 0, 'html_beds': 0, 'html_sqft': 0, 'jsonld': 0}`
- content_type: `application/json`
- key headers: `{'content-type': 'application/json', 'origin': 'https://vineyardscartersville.com', 'referer': 'https://vineyardscartersville.com/floorplans/'}`

**3.** (novel) GET `https://vineyardscartersville.com/floorplans/_fp-renderable/params%3Ainstance%3D20f4d721f54a51b85d9fed8b7f6d8490%26actio`
- signal_kind: `json_api` score=161
- shape: `{'rent_keys': 14, 'unit_keys': 14, 'sqft_keys': 15, 'bed_keys': 15, 'avail_keys': 15, 'html_rents': 0, 'html_beds': 46, 'html_sqft': 44, 'jsonld': 0}`
- content_type: `text/html`
- key headers: `{'referer': 'https://vineyardscartersville.com/floorplans/'}`
