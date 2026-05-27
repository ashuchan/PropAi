# NO_FINGERPRINT_NO_API — deep-probe findings (2026-05-25)

**Scope:** 24 properties from `n_full_zero_w2_results.jsonl` with `verdict == NO_FINGERPRINT_NO_API` (no PMS marker in landing HTML + no fetch/axios constants in JS bundles + no conventional API host hits).

**Method:** curl_cffi static fetch of landing + (where status 200) Chrome MCP live render + network panel inspection. Found no new ≥3-property CMS cluster worth shipping an adapter; found three probe-heuristic bugs that account for several of the 24 false negatives.

---

## Headline

| Cluster | Count | Classification |
|---|---:|---|
| SiteGround `sgcaptcha` anti-bot wall (HTTP 202 + 167-byte meta-refresh) | **8** | DEFER — anti-bot wall, not a CMS. Misclassified as NO_FINGERPRINT |
| Probe URL-suffix bugs (`.aspx` / `.html` / `_` variant pages exist with units) | **3** | PROBE FALSE NEGATIVE — sites are on Knock / RealPage / RentalAddress / EXR |
| Custom operator-portfolio CMSes (plan-level static rent only) | **4** | Operator-data-gap on per-unit; plan-level extractable via existing fallback |
| GoDaddy Website Builder marketing-only sites | **2** | Operator-data-gap (full) |
| Stale CSV URLs (HTTP 404 / dead host) | **7** | CSV hygiene — quarantine |
| **Total accounted** | **24** | |

**No ≥3-prop CMS cluster** that warrants a new adapter chip. The 8-prop SiteGround cluster is real but is an anti-bot wall (existing `sucuri` / `datadome` siblings handle similar — see Cluster 1 fix below).

---

## Cluster 1 — SiteGround sgcaptcha anti-bot wall (8 props)

All return **HTTP 202** with `Content-Length: 167` and body:

```html
<html><head><link rel="icon" href="data:;"><meta http-equiv="refresh"
  content="0;/.well-known/sgcaptcha/?r=%2F&y=ipc:35.146.200.5:..."></meta></head></html>
```

Response header `sg-captcha: challenge` confirms SiteGround's anti-bot challenge.

| pid | name | host |
|---:|---|---|
| 299665 | Turner Pointe | turnerpointesc.com |
| 236919 | Reserve At Stone Port | liveatstoneport.com |
| 37776 | The Gilmore Apartments | thegilmoreapartments.com |
| 55165 | Legacy Oaks | legacyoaksapts.com |
| 61979 | Villa Milano | villamilano.us |
| 55729 | Cherry Court | keystonemanagement.com/apartments/nc/cherry-court-apartments (208-byte variant) |
| 2507 | Terrain | terrainaustin.com |
| 238785 | The Reserve at Belvedere | liveatbelvedere.com |

**Fix:** `PMS_MARKERS["sucuri"]` already contains `"sgcaptcha"` (line 71 of `probe_runner.py`), but the marker check runs against the **body**, and the 167-byte body does NOT contain `sgcaptcha` as a string — only the path inside the meta-refresh URL contains it. The check needs to inspect the body OR the response header `sg-captcha`. Today the 202 body's `sgcaptcha` literal would match (it's in the meta-refresh URL), but the probe's `_signatures(landing_html)` runs after status-coding the landing — looking at probe code, the marker check does match `sgcaptcha` substring in the body, so the body string `/.well-known/sgcaptcha/` SHOULD have hit. Worth a unit re-run to confirm — possibility is that the 202 body was empty-string fall-through, in which case probe should fall back to header inspection on 202s.

**Action (separate concern):** add a fast-path that classifies status 202 + body < 500 bytes + `meta http-equiv="refresh"` as `ANTIBOT_WALL` directly. Saves 8 misclassifications per run.

---

## Cluster 2 — Probe URL-suffix false negatives (3 props)

The probe sweeps `/floorplans`, `/floor-plans`, `/availability` but **does not try** `.aspx`, `.html`, `.php` extensions nor `_` (underscore) variants. Re-probing manually via Chrome MCP found unit/rent data at the real path:

| pid | name | landing | real unit URL | rent in DOM |
|---:|---|---|---|---|
| 283599 | Homer | 66homer.com | `/floor-plans.aspx` (HTTP 200) | $1,000 / $1,250 / $1,500 / $1,750 / $2,000 / $2,500 / $3,000 / $2,150 / $2,300 / $2,450 (10+ values, 10 unit nodes) |
| 261580 | 1515 Park Place | 1515parkplace.com | `/availability.html` (HTTP 200) | $2,300 / $2,450 / $2,300 / $2,450 / $2,200 / $2,300 / $3,000 / $2,750 |
| 218586 | Cedar Ridge | cedarridgeapts.rentaladdress.com | `/floor_plans` (HTTP 200, **underscore**) | $1,475 / $1,750 (2 plans, plan-level only) |

**Bonus finding — Homer is ALSO a `PMS_MARKERS["knock"]` false negative:**
- Homer's landing HTML contains `https://doorway.knck.io/latest/doorway.min.js` (Knock Doorway script)
- AND `https://cs-cdn.realpage.com/CWS/2267476/...` (RealPage CWS — site 2267476)
- Current `PMS_MARKERS["knock"] = ["knock-cdn", "knockrentals", "knock.app"]` misses `knck.io` (Knock's CDN domain)
- Current `PMS_MARKERS["realpage_cws"] = ["cws.realpage.com", "realpage-cws"]` misses `cs-cdn.realpage.com` (the actual CDN)
- The real adapter (`knock.py`) would route correctly if detector saw the marker

**Action:** patch `investigations/2026-05-25-unit-debug/probe_runner.py`:
- Append `.aspx`, `.html`, `.php` to the alt-URL sweep (steps 2_floorplans / 3_floor-plans / 4_availability)
- Append `floor_plans` (underscore variant)
- Add `"knck.io"`, `"doorway.knck"` to `PMS_MARKERS["knock"]`
- Add `"cs-cdn.realpage.com"` to `PMS_MARKERS["realpage_cws"]`

These are probe-quality fixes — they do NOT change adapter coverage today (the real adapters detect these sites correctly via the live HTML at the real URL), but they would re-classify Homer + 1515 Park Place + Cedar Ridge out of NO_FINGERPRINT bucket on the next probe run.

---

## Cluster 3 — Custom operator-portfolio CMSes with plan-level rent (4 props)

Each property's parent host is a bespoke property-management CMS. All ship plan-level rent in static HTML; none expose per-unit inventory. Already covered by existing `TIER_2_PLAN_TEXT` plan-level fallback.

| pid | name | host (CMS owner) | static rent rows |
|---:|---|---|---|
| 48708 | Windpoint | karademas.org (Karademas Management) | `<tr>` `1 Bedroom` `$965.00–$1,190.00`, `2 Bedroom` `$1,175.00–$1,250.00` |
| 77725 | Red Oak Ranch | redoakranch.net (custom) | 6 plan rows w/ sqft + bed + bath + rent + deposit ($945 / $1,005 / $1,120 / $1,340) |
| 72732 | The Willows | centralmngt.com (Central Management ASP.NET portfolio, `MainContent_rptOptions_optionsRent_N` IDs) | Plan rows: $1,199.00–$1,249.00, $1,550.00–$1,600.00 |
| 236195 | Walnut Crossing | parkrunmgt.com (Park Run Management custom CMS) | $1,000 / $1,200 in DOM (landing 500, listing/walnut-crossing/ 200) |

**No single CMS vendor has ≥3 properties** in the n_full_zero w2 cohort. Each is bespoke. Operator-data-gap on per-unit is the true classification.

---

## Cluster 4 — GoDaddy Website Builder marketing-only (2 props)

Both carry the same `<meta name="generator" content="Starfield Technologies; Go Daddy Website Builder 8.0.0000">`. All assets served from `img1.wsimg.com`. No rent strings anywhere in the static HTML; no inquiry / pricing widgets pointing to a real PMS.

| pid | name | host |
|---:|---|---|
| 266940 | Riverfront Lofts I | riverfrontloftsallentown.com |
| 284683 | Sinclair Ridge | sinclairridgetn.com |

**Classification:** operator-data-gap (full). Operator has not published any rent or unit data on their marketing site.

**Action (cheap win):** add `"img1.wsimg.com"` / `"Go Daddy Website Builder"` to `PMS_MARKERS` → tag `godaddy_builder` → classify as `OPERATOR_MARKETING_ONLY` rather than `NO_FINGERPRINT_NO_API`.

---

## Cluster 5 — Stale CSV URLs (7 props)

Operator changed/dropped the website; CSV URL is dead.

| pid | name | url | observed |
|---:|---|---|---|
| 97506 | Roebling Lofts I | http://roeblinglofts.com/ | 404 — host alive, all paths 404 |
| 16072 | Park at Walker's Landing | https://www.ariseequity.com/park-at-walkers-landing#neighborhood | 404 (portfolio page removed; parent site OK) |
| 57320 | Brookmeade Apartments | https://brookmeade-apartments.rentcafewebsite.com/ | 404 — RentCafe SaaS tenant deleted (would otherwise be RentCafe Tier-1) |
| 46108 | (no name) Stratford Mill | http://www.stratfordmill.com/home.html | 404, styled error page |
| 265892 | (no name) | https://www.34edenrentals.com/ | 404 |
| 68497 | (no name) Liveatmaya | http://www.liveatmaya.com/ | 404 |
| 74488 | St. Johns Wood | http://www.gscapts.com/apartments/Richmond_VA/zip_23225/gsc/4497 | URL redirects to gscapts.com root; specific property path stale. Jonah Digital marketing CMS detected but no per-property landing |

**Action:** flag in `properties.csv` (or a `data/state/quarantine.json`) with `stale_url=true`. These bias the failure-rate metric; quarantining sets a clean denominator.

---

## Decision: no ship chip

No newly-discovered CMS vendor has ≥3 properties in this cohort. Pattern is:
- 8 / 24 = anti-bot wall (sgcaptcha) — defer
- 3 / 24 = real PMS detector miss (Knock / RealPage / RentalAddress) — fixable in probe heuristics
- 4 / 24 = bespoke per-vendor CMSes with plan-level static rent — already covered by plan-level fallback
- 2 / 24 = GoDaddy marketing-only — operator-data-gap
- 7 / 24 = stale CSV URLs — CSV hygiene

The most leverage-y follow-ups are **probe-heuristic patches** (URL extensions + Knock/RealPage markers + sgcaptcha 202 fast-path + GoDaddy generator tag). These re-classify ~13 of the 24 out of `NO_FINGERPRINT_NO_API` on the next probe pass without touching any adapter.

---

## Files referenced

- Worklist: `.claude/worktrees/angry-murdock-c19e06/investigations/2026-05-25-unit-debug/artifacts/probe/n_full_zero_w2_results.jsonl`
- Probe runner (heuristics to patch): `.claude/worktrees/angry-murdock-c19e06/investigations/2026-05-25-unit-debug/probe_runner.py` (PMS_MARKERS @ line 45, alt-URL sweep @ line 173)
- Existing detector: `ma_poc/pms/detector.py`
