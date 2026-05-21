# www.ovationattempe.com
Verdict: **probe_blocked_cf**

## HAR summary
- size: 11,963,918 bytes
- entries: 142
- pms_signals: `['rentcafe']`
- candidate unit-data responses: 0
- top hosts:
  - 48× `cdngeneralmvc.rentcafe.com`
  - 11× `www.google.com`
  - 10× `resource.rentcafe.com`
  - 8× `www.googletagmanager.com`
  - 8× `sdk.getflex.com`
  - 7× `tags.srv.stackadapt.com`

## Live HTTP probe (curl_cffi)
- 200 score=19 len=201,828  `https://www.ovationattempe.com/floorplans` **CF_BLOCK**
  - title: `Floor Plans | Ovation at Tempe | Tempe, AZ`
- 200 score=19 len=201,828  `https://www.ovationattempe.com/floorplans/` **CF_BLOCK**
  - title: `Floor Plans | Ovation at Tempe | Tempe, AZ`
- 404 score=0 len=117,376  `https://www.ovationattempe.com/floor-plans` **CF_BLOCK**
  - title: `Floor Plans | Ovation at Tempe | Tempe, AZ`
- 404 score=0 len=117,376  `https://www.ovationattempe.com/floor-plans/` **CF_BLOCK**
  - title: `Floor Plans | Ovation at Tempe | Tempe, AZ`
- 404 score=0 len=117,064  `https://www.ovationattempe.com/apartments` **CF_BLOCK**
  - title: `Ovation at Tempe`

**Best URL for HTTP extraction:** `https://www.ovationattempe.com/floorplans`  (score=19, len=201,828)
