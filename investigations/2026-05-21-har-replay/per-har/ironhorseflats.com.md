# ironhorseflats.com
Verdict: **discoverable_via_http_probe**

## HAR summary
- size: 7,941,213 bytes
- entries: 80
- pms_signals: `[]`
- candidate unit-data responses: 0
- top hosts:
  - 61× `ironhorseflats.com`
  - 3× `multisite.agencyfifty3.com`
  - 3× `analytics.google.com`
  - 2× `www.googletagmanager.com`
  - 2× `cmp.osano.com`
  - 2× `unpkg.com`

## Live HTTP probe (curl_cffi)
- 200 score=27 len=254,636  `https://ironhorseflats.com/floor-plans` 
  - title: `Floor Plans | 1 &amp; 2 Bedroom Apartments in North Austin`
- 200 score=27 len=254,636  `https://ironhorseflats.com/floor-plans/` 
  - title: `Floor Plans | 1 &amp; 2 Bedroom Apartments in North Austin`
- 200 score=27 len=296,180  `https://ironhorseflats.com/` 
  - title: `Ironhorse Flats | North Austin Texas Apartments | ATX`
- 404 score=0 len=183,806  `https://ironhorseflats.com/floorplans` **CF_BLOCK**
  - title: `Page Not Found - Ironhorse Flats`
- 404 score=0 len=183,814  `https://ironhorseflats.com/floorplans/` **CF_BLOCK**
  - title: `Page Not Found - Ironhorse Flats`

**Best URL for HTTP extraction:** `https://ironhorseflats.com/floor-plans`  (score=27, len=254,636)
