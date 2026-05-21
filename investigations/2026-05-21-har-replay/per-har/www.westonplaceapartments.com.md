# www.westonplaceapartments.com
Verdict: **covered_by_existing_adapter:spherexx.presentation**

## HAR summary
- size: 35,477,781 bytes
- entries: 278
- pms_signals: `['chatbot_fl', 'realpage', 'spherexx']`
- candidate unit-data responses: 3
- top hosts:
  - 130× `static.matterport.com`
  - 23× `presentation.spherexx.app`
  - 14× `my.matterport.com`
  - 12× `cdn.cookielaw.org`
  - 8× `events.matterport.com`
  - 7× `www.google.com`

### Top candidates

**1.** (spherexx.presentation) GET `https://presentation.spherexx.app/api/unit`
- signal_kind: `json_api` score=194
- shape: `{'rent_keys': 42, 'unit_keys': 42, 'sqft_keys': 21, 'bed_keys': 21, 'avail_keys': 21, 'html_rents': 16, 'html_beds': 0, 'html_sqft': 0, 'jsonld': 0}`
- content_type: `application/json`
- key headers: `{'Referer': 'https://presentation.spherexx.app/'}`

**2.** (novel) GET `https://fa-chatbot-prod-e418.azurewebsites.net/api/GetPropertyBlob/Settings-42E2A753-060C-4D22-99CE-850D7770A9DF`
- signal_kind: `html_inline` score=12
- shape: `{'rent_keys': 2, 'unit_keys': 0, 'sqft_keys': 0, 'bed_keys': 0, 'avail_keys': 1, 'html_rents': 13, 'html_beds': 4, 'html_sqft': 0, 'jsonld': 0}`
- content_type: `application/json`
- key headers: `{'Origin': 'https://www.westonplaceapartments.com', 'Referer': 'https://www.westonplaceapartments.com/'}`

**3.** (novel) GET `https://www.westonplaceapartments.com/apartments/fl/weston/floor-plans`
- signal_kind: `html_inline` score=7
- shape: `{'rent_keys': 0, 'unit_keys': 0, 'sqft_keys': 0, 'bed_keys': 0, 'avail_keys': 0, 'html_rents': 0, 'html_beds': 2, 'html_sqft': 0, 'jsonld': 1}`
- content_type: `text/html`
- key headers: `{'referer': 'https://www.westonplaceapartments.com/'}`
