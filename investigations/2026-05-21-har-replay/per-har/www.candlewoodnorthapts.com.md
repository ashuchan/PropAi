# www.candlewoodnorthapts.com
Verdict: **discoverable_via_http_probe**

## HAR summary
- size: 8,336,990 bytes
- entries: 129
- pms_signals: `['knock', 'rentcafe']`
- candidate unit-data responses: 0
- top hosts:
  - 56× `cdngeneralmvc.rentcafe.com`
  - 25× `t.rentcafe.com`
  - 17× `doorway-api.knockrentals.com`
  - 6× `resource.rentcafe.com`
  - 4× `fonts.googleapis.com`
  - 4× `fonts.gstatic.com`

## Live HTTP probe (curl_cffi)
- 200 score=24 len=217,937  `https://www.candlewoodnorthapts.com/floorplans` 
  - title: `Studio, 1- &amp; 2-Bedroom Apartments in Northridge, CA`
- 200 score=24 len=217,937  `https://www.candlewoodnorthapts.com/floorplans/` 
  - title: `Studio, 1- &amp; 2-Bedroom Apartments in Northridge, CA`
- 404 score=0 len=115,014  `https://www.candlewoodnorthapts.com/floor-plans` **CF_BLOCK**
  - title: `Studio, 1- &amp; 2-Bedroom Apartments in Northridge, CA`
- 404 score=0 len=115,014  `https://www.candlewoodnorthapts.com/floor-plans/` **CF_BLOCK**
  - title: `Studio, 1- &amp; 2-Bedroom Apartments in Northridge, CA`
- 404 score=0 len=114,690  `https://www.candlewoodnorthapts.com/apartments` **CF_BLOCK**
  - title: `Candlewood North`

**Best URL for HTTP extraction:** `https://www.candlewoodnorthapts.com/floorplans`  (score=24, len=217,937)
