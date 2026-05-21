# Cookie-mint reuse — 300-property A/B evaluation + strategy

Date: 2026-05-21
Sample: 300 properties across 4 sheets (T3_No_extraction 173, T4_No_body_antibot 94, T4_Edge_RentCafe 22, T4_Edge_Knock 11)
Method: per-property A/B on the **discovered floor-plan target URL** (not homepage — that was v1's flaw).

## Verdict: cookie-mint reuse is **net-harmful** in this codebase

| Verdict | Count | % | Interpretation |
|---|---:|---:|---|
| **helped** | **0** | **0.0%** | Cookie-mint unblocked **no** properties |
| **hurt** | **10** | **3.3%** | Adding cookies turned 200-OK into 403 CF IUAM |
| no_change_both_ok | 77 | 25.7% | Cookie-mint was a no-op (target accessible w/o cookies) |
| no_change_both_blocked | 83 | 27.7% | Both blocked; cookie-mint irrelevant — cohort needs other mechanism |
| no_target_anchor | 61 | 20.3% | Couldn't probe — homepage had no floor-plan link |
| no_cookies_minted | 69 | 23.0% | L1 RENDER succeeded but minted zero clearance cookies (host has no CF/DataDome challenge that drops a cookie) |

**Net effect across 300: helped 0, hurt 10. Cookie-mint never helped a single property in this sample.**

## Block-class effectiveness (the actionable view)

| Target baseline block | n | helped | **hurt** | both ok | both blocked | no cookies |
|---|---:|---:|---:|---:|---:|---:|
| `none` (target reachable without cookies) | 153 | 0 | **10** | 77 | 0 | 66 |
| `cf_iuam` (target CF-blocked already) | 84 | 0 | 0 | 0 | 83 | 1 |
| `http_4xx` | 1 | 0 | 0 | 0 | 0 | 1 |
| `fetch_error` | 1 | 0 | 0 | 0 | 0 | 1 |

**Two cohorts, two distinct failure modes:**

1. **The 10 "hurt" cohort (`none` → `cf_iuam` after attaching cookies)**: target was working fine, the act of attaching minted cookies *triggered* CF bot-fight. UA-binding mismatch between the patchright Chromium UA (which minted `cf_clearance`) and the curl_cffi `chrome120` impersonation UA (which presents the cookie). CF sees a cookie that doesn't match the UA that solved the challenge → assumes bot replay → fresh IUAM challenge.
2. **The 83 "both blocked" cohort (cf_iuam regardless of cookies)**: target is genuinely CF-protected. Cookies minted on the homepage don't have clearance for the `/conventional/` path (CF scopes `cf_clearance` per path-segment when bot-fight is on). These properties need real-browser navigation to the target, not cookie reuse.

## The 10 properties cookie-mint actively broke

All went from `200 OK` (working) → `403 cf_iuam` (blocked) when cookies were attached:

| ID | Sheet | Target | No-cookie rents | With-cookie |
|---|---|---|---:|---|
| 11727 | T3 | risebedfordlake.com/.../conventional/ | **35** | 403 |
| 35683 | T3 | parkcrestatthelakesapts.com | 5 | 403 |
| 74344 | T3 | privatereserveapts.com | 8 | 403 |
| 75050 | T3 | santacruzapartmenthomes.com | 6 | 403 |
| 23747 | T3 | parkviewterrace.com | 2 | 403 |
| 36173 | T3 | wg.barringtonresidential.com | 4 | 403 |
| 37979 | T3 | steeplechasevillagecolumbus.com | 2 | 403 |
| 65468 | T3 | redtailapartments.com | 0 (sqft only) | 403 |
| 293923 | T4_antibot | woodsatcountrysidecrossing.com | 1 | 403 |
| 299792 | T4_antibot | caseycornertownhomes.com | 3 | 403 |

These properties' canary verdicts should have been SUCCESS, not T3_No_extraction. The cookie-mint mechanism is what's keeping them in the failure bucket.

## Why v2 found 0 helped vs the 5-property test seemed to show "blocked everywhere"

The earlier 5-property test was a microcosm of the 10-hurt cohort: those 5 were the same UA-binding pathology. The 300-property grind shows it's not all 109 Entrata properties — only ~10% of them. The other Entrata properties either work fine without cookies (the 77 "both ok") or are genuinely CF-protected at the target (the 69 cf_iuam baseline T3 properties).

## Recommended strategy

### Action 1 — Disable cookie-mint reuse by default

Patch `ma_poc/pms/adapters/_probe.py:_with_clearance` to **not attach minted cookies unless explicitly opt-in**:

```python
# Empty by default. Per the 300-property A/B (2026-05-21), reuse
# helped 0 properties and hurt 10 by triggering CF bot-fight via UA-
# binding mismatch (patchright Chromium UA mints cookies; curl_cffi
# chrome120 presents them → CF sees stolen-cookie replay → IUAM).
_CLEARANCE_REUSE_HOST_ALLOWLIST: set[str] = set()


def _with_clearance(opts: dict[str, Any], url: str | None = None) -> dict[str, Any]:
    minted = _clearance_cookies.get() or {}
    if not minted:
        return opts
    if url is not None:
        host = _urlparse(url).netloc.lower()
        if host not in _CLEARANCE_REUSE_HOST_ALLOWLIST:
            return opts  # default: don't attach
    explicit = opts.get("cookies") or {}
    if isinstance(explicit, dict):
        opts["cookies"] = {**minted, **explicit}
    return opts
```

Inverted defaults: the mechanism is **opt-in per host**, not opt-out via the Nestin pattern. This codifies the data — nothing in the 300-sample qualified as "helpful," so the allowlist starts empty.

### Action 2 — Auto-retry without cookies on CF IUAM response

In `probe_get` / `probe_post`, detect `<title>Just a moment...</title>` or status 403 + Cloudflare server header. If detected AND we attached cookies, retry once with `set_clearance_cookies(None)`:

```python
def probe_get(url: str, **kw) -> Response:
    opts = _with_clearance({**_DEFAULTS, **kw}, url=url)
    r = cc_requests.get(url, **opts)
    # 2026-05-21: cookie-mint UA-binding mismatch on CF bot-fight
    # zones produces a false-block when cookies are attached.
    # Retry once without cookies before escalating.
    if (
        r.status_code in (403, 429, 503)
        and _looks_like_cf_iuam(r)
        and opts.get("cookies")
    ):
        opts_no_cookies = {k: v for k, v in opts.items() if k != "cookies"}
        r2 = cc_requests.get(url, **opts_no_cookies)
        if r2.status_code == 200:
            return r2
    return r
```

This single change would have flipped all 10 hurt cases.

### Action 3 — The 83 both-blocked cohort needs L1 navigation to the target, not the homepage

For these, the homepage isn't CF-protected but `/conventional/` is. The current flow renders homepage via patchright (mints homepage clearance) then probes `/conventional/` via curl_cffi (fresh CF challenge, blocked). Two fixes — pick one:

(a) **Have patchright navigate to the discovered target URL** instead of the homepage. The clearance cookie minted at the target gets the correct path scope. Adapter then probes deeper sub-resources with the now-properly-scoped clearance.

(b) **HAR-replay**: capture a successful target visit (via Camoufox or manual browser) and replay the recorded tokens. Reference the existing investigation thread on anti-bot.

### Action 4 — Fix the broken Camoufox path

`ENABLE_CAMOUFOX=true` returns `TypeError` on every L1 fetch (confirmed earlier 5-property test). Camoufox is the documented escalation rung for CF challenges patchright can't pass, but it's non-functional. Until that integration is fixed, the 83 cohort has no real path forward.

### Action 5 — `no_target_anchor` cohort (61 properties)

20% of the sample had no floor-plan anchor discoverable from homepage. These need either:
- More label variants in the discovery regex (`Models`, `Pricing`, `Find Your Home`, hash anchors)
- In-page JS exploration (anchor lives in a JS-built menu)

This is independent of cookie-mint.

## What NOT to do

- **Don't escalate to residential proxy for DataDome targets** — prior investigation (`project_stillnosig_proxy_disproven` memory) shows residential proxy makes DataDome blocks *worse*. **CAVEAT (2026-05-21 update):** the proxy claim is per-anti-bot-vendor — for Yardi-managed `*.securecafe.com` (CF tenancy that blocks GCP IP ranges), BrightData via `PROBE_PROXY_URL` flips ~259 properties from blocked to extracted. See `project_securecafe_proxy_env_bug` memory. Don't generalise "no residential proxy" across vendors. The 83 cohort here is the CF-bot-fight subset — different from SecureCafe — and residential proxy still hurts there because the rotating IPs each trigger fresh IUAM, not because CF-tenancy blocks GCP per se.
- **Don't broadly enable cookie-mint** with the assumption it's a universal helper. The data is unambiguous: 0/300 helped, 10/300 hurt.
- **Don't expand the existing `set_clearance_cookies(None)` defensive opt-out pattern from `_rentcafe_nestin.py`** as the long-term solution — that's a per-adapter band-aid. The default at `_with_clearance` level needs to flip.

## Artifacts

- [worklist.json](investigations/2026-05-21-t3-grind/artifacts/blockwall_v2/worklist.json) — 300 input properties
- [results.jsonl](investigations/2026-05-21-t3-grind/artifacts/blockwall_v2/results.jsonl) — one line per property: render outcome, no-cookie target probe, with-cookie target probe, block classifications, verdict
- [STRATEGY.md](investigations/2026-05-21-t3-grind/artifacts/blockwall_v2/STRATEGY.md) — this file
- v1 (homepage A/B, flawed; kept for comparison): [../blockwall/](investigations/2026-05-21-t3-grind/artifacts/blockwall/)
