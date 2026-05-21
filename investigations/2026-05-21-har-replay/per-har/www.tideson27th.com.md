# www.tideson27th.com
Verdict: **covered_by_existing_adapter:knock.units**

## HAR summary
- size: 18,993,990 bytes
- entries: 131
- pms_signals: `['knock']`
- candidate unit-data responses: 3
- top hosts:
  - 16× `doorway-api.knockrentals.com`
  - 11× `www.tideson27th.com`
  - 11× `app.meetelise.com`
  - 10× `www.google.com`
  - 9× `www.gstatic.com`
  - 8× `sdk.getflex.com`

### Top candidates

**1.** (knock.units) GET `https://doorway-api.knockrentals.com/v1/property/2027940/units`
- signal_kind: `json_api` score=405
- shape: `{'rent_keys': 72, 'unit_keys': 108, 'sqft_keys': 45, 'bed_keys': 45, 'avail_keys': 36, 'html_rents': 0, 'html_beds': 0, 'html_sqft': 0, 'jsonld': 0}`
- content_type: `application/json`
- key headers: `{'content-type': 'application/json', 'origin': 'https://www.tideson27th.com', 'referer': 'https://www.tideson27th.com/'}`

**2.** (wp.admin_ajax) POST `https://www.tideson27th.com/wp-admin/admin-ajax.php`
- signal_kind: `html_inline` score=15
- shape: `{'rent_keys': 0, 'unit_keys': 0, 'sqft_keys': 0, 'bed_keys': 0, 'avail_keys': 0, 'html_rents': 6, 'html_beds': 6, 'html_sqft': 7, 'jsonld': 0}`
- content_type: `text/html`
- key headers: `{'content-type': 'application/x-www-form-urlencoded; charset=UTF-8', 'origin': 'https://www.tideson27th.com', 'referer': 'https://www.tideson27th.com/floorplans/'}`

**3.** (novel) GET `https://www.tideson27th.com/wp-content/uploads/Custom_JSON_Files/schema.json`
- signal_kind: `html_inline` score=11
- shape: `{'rent_keys': 0, 'unit_keys': 0, 'sqft_keys': 6, 'bed_keys': 0, 'avail_keys': 0, 'html_rents': 0, 'html_beds': 0, 'html_sqft': 0, 'jsonld': 1}`
- content_type: `application/json`
- key headers: `{'Referer': 'https://www.tideson27th.com/floorplans/'}`
