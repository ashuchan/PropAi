# www.ashfordcasaserena.com
Verdict: **probe_blocked_cf**

## HAR summary
- size: 12,898,568 bytes
- entries: 83
- pms_signals: `['entrata']`
- candidate unit-data responses: 0
- top hosts:
  - 28× `commoncf.entrata.com`
  - 12× `medialibrarycfo.entrata.com`
  - 9× `www.google-analytics.com`
  - 8× `www.ashfordcasaserena.com`
  - 5× `bam.nr-data.net`
  - 3× `www.googletagmanager.com`

## Live HTTP probe (curl_cffi)
- 200 score=5 len=142,529  `https://www.ashfordcasaserena.com/floorplans` **CF_BLOCK**
  - title: `Apartments for Rent in Houston, TX | Ashford Casa Serena | Welcome Home To Ashfo`
- 200 score=5 len=142,529  `https://www.ashfordcasaserena.com/floor-plans` **CF_BLOCK**
  - title: `Apartments for Rent in Houston, TX | Ashford Casa Serena | Welcome Home To Ashfo`
- 200 score=5 len=142,529  `https://www.ashfordcasaserena.com/floorplans/` **CF_BLOCK**
  - title: `Apartments for Rent in Houston, TX | Ashford Casa Serena | Welcome Home To Ashfo`
- 200 score=5 len=142,529  `https://www.ashfordcasaserena.com/floor-plans/` **CF_BLOCK**
  - title: `Apartments for Rent in Houston, TX | Ashford Casa Serena | Welcome Home To Ashfo`
- 200 score=5 len=142,529  `https://www.ashfordcasaserena.com/apartments` **CF_BLOCK**
  - title: `Apartments for Rent in Houston, TX | Ashford Casa Serena | Welcome Home To Ashfo`

**Best URL for HTTP extraction:** `https://www.ashfordcasaserena.com/floorplans`  (score=5, len=142,529)
