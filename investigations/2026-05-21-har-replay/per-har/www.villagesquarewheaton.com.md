# www.villagesquarewheaton.com
Verdict: **discoverable_via_http_probe**

## HAR summary
- size: 9,611,420 bytes
- entries: 51
- pms_signals: `[]`
- candidate unit-data responses: 0
- top hosts:
  - 15× `cdn.365residentservices.com`
  - 6× `apollostore.blob.core.windows.net`
  - 5× `www.gstatic.com`
  - 4× `www.myshowing.com`
  - 3× `www.google.com`
  - 2× `fonts.googleapis.com`

## Live HTTP probe (curl_cffi)
- 200 score=33 len=44,941  `https://www.villagesquarewheaton.com/floorplans` 
  - title: `1,2,3 Bedroom Apartments for Rent in Wheaton, MD | Village Square...`
- 200 score=33 len=44,941  `https://www.villagesquarewheaton.com/floorplans/` 
  - title: `1,2,3 Bedroom Apartments for Rent in Wheaton, MD | Village Square...`
- 200 score=33 len=44,941  `https://www.villagesquarewheaton.com/Marketing/FloorPlans` 
  - title: `1,2,3 Bedroom Apartments for Rent in Wheaton, MD | Village Square...`
- 404 score=0 len=3,527  `https://www.villagesquarewheaton.com/floor-plans` 
  - title: `The resource cannot be found.`
- 404 score=0 len=3,529  `https://www.villagesquarewheaton.com/floor-plans/` 
  - title: `The resource cannot be found.`

**Best URL for HTTP extraction:** `https://www.villagesquarewheaton.com/floorplans`  (score=33, len=44,941)
