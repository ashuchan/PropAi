# www.arborpointeapthomes.com
Verdict: **discoverable_via_http_probe**

## HAR summary
- size: 6,491,797 bytes
- entries: 111
- pms_signals: `['rentcafe']`
- candidate unit-data responses: 0
- top hosts:
  - 57× `cdngeneralmvc.rentcafe.com`
  - 13× `resource.rentcafe.com`
  - 12× `cdn.cookielaw.org`
  - 7× `t.rentcafe.com`
  - 5× `www.googletagmanager.com`
  - 3× `www.arborpointeapthomes.com`

## Live HTTP probe (curl_cffi)
- 200 score=120 len=271,407  `https://www.arborpointeapthomes.com/floorplans` 
  - title: `1, 2, &amp; 3 Bedroom Apartment Floor Plans | Apartments in Fairfield OH`
- 200 score=120 len=271,407  `https://www.arborpointeapthomes.com/floorplans/` 
  - title: `1, 2, &amp; 3 Bedroom Apartment Floor Plans | Apartments in Fairfield OH`
- 404 score=3 len=131,738  `https://www.arborpointeapthomes.com/floor-plans` **CF_BLOCK**
  - title: `1, 2, &amp; 3 Bedroom Apartment Floor Plans | Apartments in Fairfield OH`
- 404 score=3 len=141,673  `https://www.arborpointeapthomes.com/floor-plans/` **CF_BLOCK**
  - title: `1, 2, &amp; 3 Bedroom Apartment Floor Plans | Apartments in Fairfield OH`
- 404 score=0 len=131,391  `https://www.arborpointeapthomes.com/apartments` **CF_BLOCK**
  - title: `Arbor Pointe`

**Best URL for HTTP extraction:** `https://www.arborpointeapthomes.com/floorplans`  (score=120, len=271,407)
