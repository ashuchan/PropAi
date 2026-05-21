# www.clearwatercreekapartments.com
Verdict: **discoverable_via_http_probe**

## HAR summary
- size: 6,963,685 bytes
- entries: 111
- pms_signals: `['rentcafe']`
- candidate unit-data responses: 0
- top hosts:
  - 48× `cdngeneralmvc.rentcafe.com`
  - 11× `resource.rentcafe.com`
  - 7× `www.googletagmanager.com`
  - 7× `tags.srv.stackadapt.com`
  - 5× `my.hy.ly`
  - 4× `www.google.com`

## Live HTTP probe (curl_cffi)
- 200 score=30 len=268,315  `https://www.clearwatercreekapartments.com/floorplans` 
  - title: `1, 2 &amp; 3-Bedroom Apartment Homes in Richardson, TX | Clearwater Creek`
- 200 score=30 len=268,315  `https://www.clearwatercreekapartments.com/floorplans/` 
  - title: `1, 2 &amp; 3-Bedroom Apartment Homes in Richardson, TX | Clearwater Creek`
- 200 score=3 len=181,746  `https://www.clearwatercreekapartments.com/` **CF_BLOCK**
  - title: `Apartment Homes in Richardson, TX | Clearwater Creek`
- 404 score=0 len=125,923  `https://www.clearwatercreekapartments.com/floor-plans` **CF_BLOCK**
  - title: `1, 2 &amp; 3-Bedroom Apartment Homes in Richardson, TX | Clearwater Creek`
- 404 score=0 len=125,923  `https://www.clearwatercreekapartments.com/floor-plans/` **CF_BLOCK**
  - title: `1, 2 &amp; 3-Bedroom Apartment Homes in Richardson, TX | Clearwater Creek`

**Best URL for HTTP extraction:** `https://www.clearwatercreekapartments.com/floorplans`  (score=30, len=268,315)
