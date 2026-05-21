# www.thetrailsarlington.com
Verdict: **discoverable_via_http_probe**

## HAR summary
- size: 7,628,852 bytes
- entries: 99
- pms_signals: `[]`
- candidate unit-data responses: 0
- top hosts:
  - 37× `cdn.prod.website-files.com`
  - 36× `use.typekit.net`
  - 7× `challenges.cloudflare.com`
  - 6× `cdn.jsdelivr.net`
  - 4× `fonts.gstatic.com`
  - 2× `www.googletagmanager.com`

## Live HTTP probe (curl_cffi)
- 200 score=18 len=52,834  `https://www.thetrailsarlington.com/floor-plans` 
  - title: `Floor Plans at The Trails Apartments in Arlington, TX`
- 200 score=18 len=52,834  `https://www.thetrailsarlington.com/floor-plans/` 
  - title: `Floor Plans at The Trails Apartments in Arlington, TX`
- 200 score=13 len=76,836  `https://www.thetrailsarlington.com/` 
  - title: `The Trails Apartments in Arlington, TX`
- 404 score=0 len=9,448  `https://www.thetrailsarlington.com/floorplans` 
  - title: `Not Found`
- 404 score=0 len=9,448  `https://www.thetrailsarlington.com/floorplans/` 
  - title: `Not Found`

**Best URL for HTTP extraction:** `https://www.thetrailsarlington.com/floor-plans`  (score=18, len=52,834)
