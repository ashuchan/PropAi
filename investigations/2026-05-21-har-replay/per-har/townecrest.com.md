# townecrest.com
Verdict: **needs_chrome_probe**

## HAR summary
- size: 15,227,301 bytes
- entries: 100
- pms_signals: `['realpage']`
- candidate unit-data responses: 0
- top hosts:
  - 28× `townecrest.com`
  - 23× `leasing.realpage.com`
  - 15× `cs-cdn.realpage.com`
  - 5× `townecrest.wpenginepowered.com`
  - 5× `p11.techlab-cdn.com`
  - 4× `fonts.googleapis.com`

## Live HTTP probe (curl_cffi)
- 200 score=2 len=88,472  `https://townecrest.com/` 
  - title: `Apartments in Gaithersburg, MD | Towne Crest Apartments`
- 200 score=0 len=70,191  `https://townecrest.com/floorplans` 
  - title: `Floorplans | Towne Crest Apartments`
- 404 score=0 len=56,345  `https://townecrest.com/floor-plans` 
  - title: `Page not found | Towne Crest Apartments and Townhomes`
- 200 score=0 len=70,191  `https://townecrest.com/floorplans/` 
  - title: `Floorplans | Towne Crest Apartments`
- 404 score=0 len=56,345  `https://townecrest.com/floor-plans/` 
  - title: `Page not found | Towne Crest Apartments and Townhomes`
