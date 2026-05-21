# www.theleightonapartments.com
Verdict: **covered_by_existing_adapter:realpage.cms_sitemanager**

## HAR summary
- size: 11,262,350 bytes
- entries: 91
- pms_signals: `['knock', 'realpage']`
- candidate unit-data responses: 2
- top hosts:
  - 20× `cs-cdn.realpage.com`
  - 15× `www.theleightonapartments.com`
  - 10× `capi.myleasestar.com`
  - 10× `cdn.cookielaw.org`
  - 8× `www.googletagmanager.com`
  - 6× `maps.googleapis.com`

### Top candidates

**1.** (realpage.cms_sitemanager) GET `https://www.theleightonapartments.com/CmsSiteManager/callback.aspx?act=Proxy/GetUnits&available=true&honordisplayorder=t`
- signal_kind: `json_api` score=66
- shape: `{'rent_keys': 6, 'unit_keys': 24, 'sqft_keys': 6, 'bed_keys': 0, 'avail_keys': 0, 'html_rents': 0, 'html_beds': 0, 'html_sqft': 0, 'jsonld': 0}`
- content_type: `application/x-javascript`
- key headers: `{'referer': 'https://www.theleightonapartments.com/'}`

**2.** (novel) GET `https://www.theleightonapartments.com/Floor-plans.aspx`
- signal_kind: `html_inline` score=7
- shape: `{'rent_keys': 0, 'unit_keys': 0, 'sqft_keys': 0, 'bed_keys': 0, 'avail_keys': 0, 'html_rents': 0, 'html_beds': 2, 'html_sqft': 0, 'jsonld': 1}`
- content_type: `text/html`
- key headers: `{'referer': 'https://www.theleightonapartments.com/'}`
