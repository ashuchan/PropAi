# www.liveatsymphony.com
Verdict: **discoverable_via_http_probe**

## HAR summary
- size: 13,718,076 bytes
- entries: 171
- pms_signals: `['knock', 'rentcafe']`
- candidate unit-data responses: 0
- top hosts:
  - 50× `cdngeneralmvc.rentcafe.com`
  - 17× `api.userway.org`
  - 14× `resource.rentcafe.com`
  - 14× `doorway-api.knockrentals.com`
  - 12× `cdn77.api.userway.org`
  - 11× `cdn.userway.org`

## Live HTTP probe (curl_cffi)
- 200 score=60 len=269,667  `https://www.liveatsymphony.com/floorplans` 
  - title: `1, 2 &amp; 3 Bedroom Apartments for Rent in Chandler, AZ (85224) | Symphony`
- 200 score=60 len=269,667  `https://www.liveatsymphony.com/floorplans/` 
  - title: `1, 2 &amp; 3 Bedroom Apartments for Rent in Chandler, AZ (85224) | Symphony`
- 200 score=16 len=176,182  `https://www.liveatsymphony.com/` **CF_BLOCK**
  - title: `Luxury Apartment Homes for Rent in Chandler, AZ (85224) with Move-In Specials | `
- 404 score=11 len=116,314  `https://www.liveatsymphony.com/floor-plans` **CF_BLOCK**
  - title: `1, 2 &amp; 3 Bedroom Apartments for Rent in Chandler, AZ (85224) | Symphony`
- 404 score=11 len=116,314  `https://www.liveatsymphony.com/floor-plans/` **CF_BLOCK**
  - title: `1, 2 &amp; 3 Bedroom Apartments for Rent in Chandler, AZ (85224) | Symphony`

**Best URL for HTTP extraction:** `https://www.liveatsymphony.com/floorplans`  (score=60, len=269,667)
