# www.james-towne-village.com
Verdict: **covered_by_existing_adapter:knock.units**

## HAR summary
- size: 14,419,002 bytes
- entries: 125
- pms_signals: `['g5', 'knock', 'realpage']`
- candidate unit-data responses: 2
- top hosts:
  - 17× `doorway-api.knockrentals.com`
  - 12× `cdn.cookielaw.org`
  - 9× `www.google-analytics.com`
  - 8× `use.typekit.net`
  - 8× `inventory.g5marketingcloud.com`
  - 7× `www.google.com`

### Top candidates

**1.** (knock.units) GET `https://doorway-api.knockrentals.com/v1/property/2029784/units`
- signal_kind: `json_api` score=107
- shape: `{'rent_keys': 16, 'unit_keys': 24, 'sqft_keys': 14, 'bed_keys': 14, 'avail_keys': 8, 'html_rents': 0, 'html_beds': 13, 'html_sqft': 0, 'jsonld': 0}`
- content_type: `application/json`
- key headers: `{'content-type': 'application/json', 'origin': 'https://www.james-towne-village.com', 'referer': 'https://www.james-towne-village.com/'}`

**2.** (novel) GET `https://www.james-towne-village.com/apartments/sc/charleston/floor-plans`
- signal_kind: `html_inline` score=9
- shape: `{'rent_keys': 0, 'unit_keys': 0, 'sqft_keys': 2, 'bed_keys': 2, 'avail_keys': 0, 'html_rents': 0, 'html_beds': 2, 'html_sqft': 0, 'jsonld': 1}`
- content_type: `text/html`
- key headers: `{'referer': 'https://www.james-towne-village.com/'}`
