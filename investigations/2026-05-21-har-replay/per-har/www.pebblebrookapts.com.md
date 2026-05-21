# www.pebblebrookapts.com
Verdict: **discoverable_via_http_probe**

## HAR summary
- size: 9,391,258 bytes
- entries: 114
- pms_signals: `['rentcafe']`
- candidate unit-data responses: 0
- top hosts:
  - 53× `cdngeneralmvc.rentcafe.com`
  - 13× `www.pebblebrookapts.com`
  - 13× `resource.rentcafe.com`
  - 7× `cdn.cookielaw.org`
  - 5× `www.googletagmanager.com`
  - 4× `www.google.co.in`

## Live HTTP probe (curl_cffi)
- 200 score=34 len=247,932  `https://www.pebblebrookapts.com/floorplans` 
  - title: `1 &amp; 2-Bedroom Apartments in Overland Park, KS | Pebblebrook`
- 200 score=34 len=247,932  `https://www.pebblebrookapts.com/floorplans/` 
  - title: `1 &amp; 2-Bedroom Apartments in Overland Park, KS | Pebblebrook`
- 200 score=2 len=210,010  `https://www.pebblebrookapts.com/` 
  - title: `Apartments in Overland Park | Pebblebrook Apartment Homes`
- 404 score=0 len=161,541  `https://www.pebblebrookapts.com/floor-plans` **CF_BLOCK**
  - title: `1 &amp; 2-Bedroom Apartments in Overland Park, KS | Pebblebrook`
- 404 score=0 len=161,541  `https://www.pebblebrookapts.com/floor-plans/` **CF_BLOCK**
  - title: `1 &amp; 2-Bedroom Apartments in Overland Park, KS | Pebblebrook`

**Best URL for HTTP extraction:** `https://www.pebblebrookapts.com/floorplans`  (score=34, len=247,932)
