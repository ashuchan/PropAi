# www.hpluxuryapts.com
Verdict: **discoverable_via_http_probe**

## HAR summary
- size: 7,487,069 bytes
- entries: 143
- pms_signals: `['rentcafe']`
- candidate unit-data responses: 0
- top hosts:
  - 53× `cdngeneralmvc.rentcafe.com`
  - 16× `api.theconversioncloud.com`
  - 11× `cdn.cookielaw.org`
  - 8× `resource.rentcafe.com`
  - 8× `tags.srv.stackadapt.com`
  - 7× `my.hy.ly`

## Live HTTP probe (curl_cffi)
- 200 score=22 len=224,701  `https://www.hpluxuryapts.com/floorplans` 
  - title: `1, 2 &amp; 3-Bedroom Apartments in North Garland | Seven Oaks`
- 200 score=22 len=224,701  `https://www.hpluxuryapts.com/floorplans/` 
  - title: `1, 2 &amp; 3-Bedroom Apartments in North Garland | Seven Oaks`
- 200 score=2 len=179,240  `https://www.hpluxuryapts.com/` **CF_BLOCK**
  - title: `Luxury Apartments in Fort Worth, TX | Highland Park Apartments`
- 404 score=0 len=128,322  `https://www.hpluxuryapts.com/floor-plans` **CF_BLOCK**
  - title: `1, 2 &amp; 3-Bedroom Apartments in North Garland | Seven Oaks`
- 404 score=0 len=128,322  `https://www.hpluxuryapts.com/floor-plans/` **CF_BLOCK**
  - title: `1, 2 &amp; 3-Bedroom Apartments in North Garland | Seven Oaks`

**Best URL for HTTP extraction:** `https://www.hpluxuryapts.com/floorplans`  (score=22, len=224,701)
