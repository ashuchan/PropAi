# liveatgraysonpark.com
Verdict: **covered_by_existing_adapter:wp.admin_ajax**

## HAR summary
- size: 31,004,466 bytes
- entries: 125
- pms_signals: `[]`
- candidate unit-data responses: 2
- top hosts:
  - 48× `swifty-media.s3.us-east-1.amazonaws.com`
  - 15× `liveatgraysonpark.com`
  - 12× `emilyv2.beswifty.com`
  - 8× `www.gstatic.com`
  - 7× `www.google.com`
  - 7× `fonts.gstatic.com`

### Top candidates

**1.** (wp.admin_ajax) POST `https://liveatgraysonpark.com/wp-admin/admin-ajax.php`
- signal_kind: `html_inline` score=21
- shape: `{'rent_keys': 0, 'unit_keys': 0, 'sqft_keys': 0, 'bed_keys': 0, 'avail_keys': 0, 'html_rents': 10, 'html_beds': 10, 'html_sqft': 8, 'jsonld': 0}`
- content_type: `text/html`
- key headers: `{'content-type': 'application/x-www-form-urlencoded; charset=UTF-8', 'origin': 'https://liveatgraysonpark.com', 'referer': 'https://liveatgraysonpark.com/floorplans/'}`

**2.** (novel) GET `https://liveatgraysonpark.com/wp-content/uploads/Custom_JSON_Files/schema.json`
- signal_kind: `html_inline` score=13
- shape: `{'rent_keys': 0, 'unit_keys': 0, 'sqft_keys': 7, 'bed_keys': 0, 'avail_keys': 0, 'html_rents': 0, 'html_beds': 1, 'html_sqft': 0, 'jsonld': 1}`
- content_type: `application/json`
- key headers: `{'Referer': 'https://liveatgraysonpark.com/floorplans/'}`
