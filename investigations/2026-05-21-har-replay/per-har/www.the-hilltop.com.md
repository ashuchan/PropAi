# www.the-hilltop.com
Verdict: **covered_by_existing_adapter:knock.units**

## HAR summary
- size: 19,402,947 bytes
- entries: 144
- pms_signals: `['knock', 'realpage']`
- candidate unit-data responses: 2
- top hosts:
  - 17× `doorway-api.knockrentals.com`
  - 15× `www.google.com`
  - 12× `cdn.cookielaw.org`
  - 12× `www.googletagmanager.com`
  - 9× `g5-assets-cld-res.cloudinary.com`
  - 8× `inventory.g5marketingcloud.com`

### Top candidates

**1.** (knock.units) GET `https://doorway-api.knockrentals.com/v1/property/2018626/units`
- signal_kind: `json_api` score=267
- shape: `{'rent_keys': 46, 'unit_keys': 69, 'sqft_keys': 37, 'bed_keys': 37, 'avail_keys': 23, 'html_rents': 0, 'html_beds': 0, 'html_sqft': 0, 'jsonld': 0}`
- content_type: `application/json`
- key headers: `{'content-type': 'application/json', 'origin': 'https://www.the-hilltop.com', 'referer': 'https://www.the-hilltop.com/'}`

**2.** (novel) GET `https://www.the-hilltop.com/apartments/tx/temple/floor-plans`
- signal_kind: `html_inline` score=10
- shape: `{'rent_keys': 0, 'unit_keys': 0, 'sqft_keys': 2, 'bed_keys': 2, 'avail_keys': 0, 'html_rents': 0, 'html_beds': 3, 'html_sqft': 0, 'jsonld': 1}`
- content_type: `text/html`
- key headers: `{'referer': 'https://www.the-hilltop.com/'}`
