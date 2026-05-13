# Spherexx Presentation Software ("Convert") — adapter TBD

## What it is

Interactive building site-map widget used by some multifamily websites for
unit availability visualization. Backed by [Spherexx](https://spherexx.com/) —
a multifamily-industry marketing platform.

Example: `henryonthepark.com/interactive-site-map/` uses this widget. User
sees a building diagram with clickable "footprints" representing each unit.
Clicking a footprint reveals rent/availability/floor-plan details.

## Technical shape

The host site embeds a small `<div id="sspsiteplan">` and a loader:

```html
<script>window.sspcfg={'key':'ZnBhdzpkZWVlbXJvbg==','opts':{'inline':true,...}}</script>
<script src="https://presentation.spherexx.app/js/ssploader.js" defer></script>
<div id="sspsiteplan" style="height: 700px"></div>
```

`ssploader.js` creates an iframe pointing at `https://presentation.spherexx.app/`
and passes the `sspcfg.key` to the iframe via `postMessage` after a "READY"
handshake. The key is base64-encoded; decoded for henryonthepark it's
`fpaw:deeemron` (likely `<tenant>:<property_hash>`).

The iframe loads `presentation.spherexx.app/assets/index-*.js` — a 257KB
Vite-bundled SPA. The SPA calls Spherexx data API endpoints internally;
endpoints are not on guessable paths (`/api/availability`, `/api/units`,
`/ssp/availability` all 404 unauthenticated).

## What we know

- Building image: `presentation.spherexx.app/common/uploads/ws/HenryOnTheParkBG.jpg` (200, 1.5MB) — direct URL works
- Key format: base64-encoded `<username>:<password>` style
- iframe-based widget; data flows through postMessage handshake

## What we'd need to build the adapter

1. Use Playwright to load the iframe + capture the XHR network log to find
   the real API endpoints (URL pattern unknown — not on standard paths)
2. Decode the postMessage protocol — what messages does the parent send to
   the iframe? What does the iframe send back?
3. Confirm the API takes the base64 key as auth header / cookie / message
   payload
4. Build a parser for the response JSON

## Resolver-level fix in this branch

Added `presentation.spherexx.app` and `spherexx.app` / `spherexx.com` to
`_LEASING_PORTAL_DOMAINS`. So when a property page has an anchor to a
Spherexx-hosted URL, the resolver will navigate there. The canary's
Playwright capture should then collect the iframe's XHR responses in
`network_log` — those can be inspected post-run to reverse-engineer the
API shape for the adapter.

## Known sites using Spherexx

- henryonthepark.com (interactive-site-map)
- (other Spherexx customers are documented at https://spherexx.com/)

Count in May 13 cohort: 1 confirmed (henryonthepark). Real count likely
higher — many properties use Spherexx for building visualization but
manual QC may not have identified the host.
