# www.thecarsonlongview.com
Verdict: **covered_by_existing_adapter:knock.units**

## HAR summary
- size: 25,041,174 bytes
- entries: 161
- pms_signals: `['knock', 'repli360']`
- candidate unit-data responses: 1
- top hosts:
  - 30× `wsv3cdn.audioeye.com`
  - 29× `static.cdn-website.com`
  - 16× `doorway-api.knockrentals.com`
  - 13× `app.repli360.com`
  - 12× `irp.cdn-website.com`
  - 6× `rtc.multiscreensite.com`

### Top candidates

**1.** (knock.units) GET `https://doorway-api.knockrentals.com/v1/property/2014660/units`
- signal_kind: `json_api` score=73
- shape: `{'rent_keys': 12, 'unit_keys': 18, 'sqft_keys': 13, 'bed_keys': 13, 'avail_keys': 6, 'html_rents': 0, 'html_beds': 0, 'html_sqft': 0, 'jsonld': 0}`
- content_type: `application/json`
- key headers: `{'content-type': 'application/json', 'origin': 'https://www.thecarsonlongview.com', 'referer': 'https://www.thecarsonlongview.com/'}`
