# apartments.thepointeatlapts.com
Verdict: **probe_blocked_cf**

## HAR summary
- size: 35,329 bytes
- entries: 3
- pms_signals: `['rentcafe']`
- candidate unit-data responses: 0
- top hosts:
  - 2× `t.rentcafe.com`
  - 1× `apartments.thepointeatlapts.com`

## Live HTTP probe (curl_cffi)
- 200 score=11 len=121,461  `https://apartments.thepointeatlapts.com/floorplans` **CF_BLOCK**
  - title: `Floor Plans of The Pointe in Stone Mountain, GA`
- 200 score=11 len=121,554  `https://apartments.thepointeatlapts.com/floorplans/` **CF_BLOCK**
  - title: `Floor Plans of The Pointe in Stone Mountain, GA`
- 200 score=5 len=67,667  `https://apartments.thepointeatlapts.com/` **CF_BLOCK**
  - title: `The Pointe | Apartments in Stone Mountain, GA | RENTCafe`
- 404 score=0 len=2,940  `https://apartments.thepointeatlapts.com/floor-plans` **CF_BLOCK**
  - title: `Rentcafe Error`
- 404 score=0 len=2,940  `https://apartments.thepointeatlapts.com/floor-plans/` **CF_BLOCK**
  - title: `Rentcafe Error`

**Best URL for HTTP extraction:** `https://apartments.thepointeatlapts.com/floorplans`  (score=11, len=121,461)
