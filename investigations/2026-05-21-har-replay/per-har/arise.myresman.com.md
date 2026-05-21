# arise.myresman.com
Verdict: **covered_by_existing_adapter:resman.portal**

## HAR summary
- size: 102,172,321 bytes
- entries: 27
- pms_signals: `['resman']`
- candidate unit-data responses: 1
- top hosts:
  - 14× `arise.myresman.com`
  - 6× `resman.blob.core.windows.net`
  - 4× `www.google-analytics.com`
  - 1× `az416426.vo.msecnd.net`
  - 1× `www.googletagmanager.com`
  - 1× `dc.services.visualstudio.com`

### Top candidates

**1.** (resman.portal) GET `https://arise.myresman.com/Portal/Applicants/Availability?a=1526&p=fbe7867d-b879-4f81-9fc4-527465e08a4e&moveInDate=5/21/`
- signal_kind: `json_api` score=24
- shape: `{'rent_keys': 7, 'unit_keys': 4, 'sqft_keys': 2, 'bed_keys': 7, 'avail_keys': 2, 'html_rents': 0, 'html_beds': 0, 'html_sqft': 0, 'jsonld': 0}`
- content_type: `text/html`
- key headers: `{'referer': 'https://arise.myresman.com/Portal/Applicants/Availability?a=1526&p=fbe7867d-b879-4f81-9fc4-527465e08a4e'}`
