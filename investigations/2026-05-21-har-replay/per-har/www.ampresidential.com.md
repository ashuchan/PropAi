# www.ampresidential.com
Verdict: **discoverable_via_http_probe**

## HAR summary
- size: 26,578,392 bytes
- entries: 234
- pms_signals: `[]`
- candidate unit-data responses: 0
- top hosts:
  - 40× `www.facebook.com`
  - 37× `www.google-analytics.com`
  - 36× `www.ampresidential.com`
  - 29× `maps.googleapis.com`
  - 15× `cdnjs.cloudflare.com`
  - 12× `cdn.cookielaw.org`

## Live HTTP probe (curl_cffi)
- 200 score=4 len=143,088  `https://www.ampresidential.com/find-your-home` 
  - title: `Apartments | AMP Residential | Find Your Home`
- 200 score=4 len=132,715  `https://www.ampresidential.com/` 
  - title: `AMP Residential I Homepage`
- 404 score=0 len=1,905  `https://www.ampresidential.com/floorplans` 
  - title: `Company: AMP Residential | LeaseLabs CMS`
- 404 score=0 len=1,905  `https://www.ampresidential.com/floor-plans` 
  - title: `Company: AMP Residential | LeaseLabs CMS`
- 404 score=0 len=1,905  `https://www.ampresidential.com/floorplans/` 
  - title: `Company: AMP Residential | LeaseLabs CMS`

**Best URL for HTTP extraction:** `https://www.ampresidential.com/find-your-home`  (score=4, len=143,088)
