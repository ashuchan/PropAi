# www.lifeatthecobb.com
Verdict: **probe_blocked_cf**

## HAR summary
- size: 14,328,024 bytes
- entries: 125
- pms_signals: `['knock', 'rentcafe']`
- candidate unit-data responses: 0
- top hosts:
  - 51× `cdngeneralmvc.rentcafe.com`
  - 13× `doorway-api.knockrentals.com`
  - 12× `cdn.cookielaw.org`
  - 11× `www.googletagmanager.com`
  - 6× `resource.rentcafe.com`
  - 3× `www.lifeatthecobb.com`

## Live HTTP probe (curl_cffi)
- 200 score=117 len=164,236  `https://www.lifeatthecobb.com/floorplans` **CF_BLOCK**
  - title: `2 &amp; 3 Bedroom Townhomes in Austell GA | The Cobb Townhomes`
- 200 score=117 len=164,236  `https://www.lifeatthecobb.com/floorplans/` **CF_BLOCK**
  - title: `2 &amp; 3 Bedroom Townhomes in Austell GA | The Cobb Townhomes`
- 404 score=1 len=77,147  `https://www.lifeatthecobb.com/floor-plans` **CF_BLOCK**
  - title: `2 &amp; 3 Bedroom Townhomes in Austell GA | The Cobb Townhomes`
- 404 score=1 len=77,147  `https://www.lifeatthecobb.com/floor-plans/` **CF_BLOCK**
  - title: `2 &amp; 3 Bedroom Townhomes in Austell GA | The Cobb Townhomes`
- 404 score=0 len=76,853  `https://www.lifeatthecobb.com/apartments` **CF_BLOCK**
  - title: `The Cobb Apartments-Townhomes`

**Best URL for HTTP extraction:** `https://www.lifeatthecobb.com/floorplans`  (score=117, len=164,236)
