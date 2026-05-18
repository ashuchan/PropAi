# Stealth Playwright — implementation plan

**Owner:** TBD
**Status:** Draft / proposal — not yet started
**Drafted:** 2026-05-18
**Source incident:** Yardi `/conventional/` Cloudflare-challenge cluster, 2026-05-18 cloud run — 193 / 357 FAILED_NO_DATA properties (54%) where the entry fetched OK but every link-hop returned a Cloudflare 5,646-byte challenge stub (`cdn-cgi/challenge-platform`). Canonical PIDs: 53592 livethearch.com, 37719 1611onlakeunion.com, 78992 22slate.com, 35921 800sixth.com, 20959 dovevalleyapts.com, 16139 chaseknollsapts.com.

---

## Goal

Recover unit data from hop pages currently blocked by Cloudflare / SGCAPTCHA / PerimeterX / hCaptcha challenges. The 2026-05-18 verdict-classification fix (Bug #1) re-labels these properties as `FAILED_UNREACHABLE` so triage isn't misled, but does NOT extract their data. This plan adds the actual recovery mechanism.

---

## TL;DR

Build a small stealth-Playwright session pool that, on first encounter with a WAF challenge on a hop, solves the challenge once and caches the resulting WAF clearance cookie (`cf_clearance`, `__cf_bm`, `srcfh-cookie`, etc.) keyed by `(host, proxy_ip, ua_hash)`. Every subsequent fetch to the same host in the same property + run gets the cookie pre-injected and bypasses the challenge for ~30 minutes.

- **Effort:** ~1 week dedicated, in 4 phases shippable independently.
- **Cost:** ~$1/day in residential-proxy spend across 193 affected properties.
- **Recovery target:** 60-75% of the 193 PIDs (those that don't gate behind additional bot-detection beyond WAF).

---

## What is and isn't the bypass

### Cookie consent ("Accept All Cookies") — NOT a bypass
Site-level GDPR/CCPA banners (`cookieyes-consent=…`, `consent-2025=…`) record that the user agreed to analytics/marketing cookies. A handful of EU-hosted sites gate content behind consent; most US apartment sites just overlay the banner on already-rendered content. Skip implementing for this plan — would deliver 1-2% recovery at best with measurable false-positive risk (auto-clicking arbitrary DOM buttons can trigger unintended state changes like form submissions).

### WAF challenge clearance cookies — THE actual bypass
Issued by the CDN AFTER the browser successfully solves the challenge once. Subsequent requests on the same `(IP, UA, TLS fingerprint, cookie jar)` tuple skip the challenge for the cookie's TTL (typically 30 min for Cloudflare, 12 hours for SiteGround).

| Cookie name | CDN | Default TTL | Set on |
|---|---|---|---|
| `cf_clearance` | Cloudflare | 30 min (configurable) | Successful managed-challenge solve |
| `__cf_bm` | Cloudflare Bot Manager | 30 min | Any successful request to a CF-protected host |
| `srcfh-cookie`, `sg-cookies` | SiteGround / SGCAPTCHA | 12 hours | Successful captcha solve |
| `_pxhd` | PerimeterX | 1 hour | After PX challenge |
| `__hssc`, `__hstc` | HubSpot bot guard | Session | First page load |

The clearance cookie is the bypass mechanism. Acquiring it requires solving the challenge once, which is exactly what stealth Playwright is for.

---

## Architecture — 4 phases

Each phase is independently shippable and delivers value on its own. Phases 1 + 3 (detection + cookie jar) deliver an immediate manual-bootstrap win even without the full pool.

### Phase 1 — WAF provider detection (effort: 2-4 hours)

**File:** [ma_poc/fetch/captcha_detect.py](ma_poc/fetch/captcha_detect.py) — already classifies bodies as `BOT_BLOCKED`. Extend to ALSO classify the WAF provider into `fetch_result._meta.captcha_provider`:

```python
_PROVIDER_PATTERNS = {
    "cloudflare": (
        r"cdn-cgi/challenge-platform",
        r"__cf_chl_managed_tk__",
        r"Just a moment\.\.\.",
    ),
    "sgcaptcha": (
        r"sgcaptcha\.com",
        r"siteground\.com.*?security",
    ),
    "perimeterx": (
        r"_pxAppId",
        r"px-captcha\.com",
    ),
    "hcaptcha": (
        r"hcaptcha\.com/captcha",
        r"data-hcaptcha-sitekey",
    ),
    "recaptcha": (
        r"www\.google\.com/recaptcha/api\.js",
        r"g-recaptcha",
    ),
}

def detect_provider(body: bytes) -> str | None:
    text = body.decode("utf-8", "ignore")[:4096]  # only inspect first 4KB
    for provider, patterns in _PROVIDER_PATTERNS.items():
        if any(re.search(p, text, re.IGNORECASE) for p in patterns):
            return provider
    return None
```

Wire result into `FetchResult._meta.captcha_provider`. Verdict layer at [reporting/verdict.py](ma_poc/reporting/verdict.py) already consumes `entry_captcha_provider` via the wedge-rescue plumbing (playbook §8.22).

**Acceptance:** running on yesterday's failed-no-data bucket emits provider breakdown (cloudflare: ~180, sgcaptcha: ~10, perimeterx: ~3, …).

---

### Phase 2 — Cookie jar persistence (effort: 4-6 hours)

**New file:** `ma_poc/fetch/clearance_jar.py`

SQLite store under `data/state/clearance_jar.sqlite`. Schema:

```sql
CREATE TABLE clearance_cookies (
    host          TEXT NOT NULL,
    proxy_ip      TEXT NOT NULL,    -- exit IP of the residential proxy used to acquire
    ua_hash       TEXT NOT NULL,    -- sha256 of the User-Agent + Accept-Lang
    provider      TEXT NOT NULL,    -- "cloudflare" | "sgcaptcha" | …
    cookie_name   TEXT NOT NULL,
    cookie_value  TEXT NOT NULL,
    expires_at    TIMESTAMP NOT NULL,
    acquired_at   TIMESTAMP NOT NULL,
    PRIMARY KEY (host, proxy_ip, ua_hash, cookie_name)
);
CREATE INDEX ix_jar_lookup ON clearance_cookies (host, expires_at);
```

API:

```python
class ClearanceJar:
    def lookup(self, host: str, proxy_ip: str, ua_hash: str) -> dict[str, str]:
        """Return active cookies for the (host, proxy, UA) triple. Empty
        dict when none / expired. Auto-deletes expired rows opportunistically."""

    def store(
        self, host: str, proxy_ip: str, ua_hash: str,
        provider: str, cookies: dict[str, str], ttl_seconds: int,
    ) -> None:
        """Persist freshly-acquired clearance cookies."""

    def purge_expired(self) -> int:
        """Scheduled cleanup; called at run boundary."""
```

**Integration points:**
- [fetch/fetcher.py](ma_poc/fetch/fetcher.py) — before any HTTP-GET to a known-WAF host, look up jar and inject via `Cookie:` header.
- Playwright path — call `context.add_cookies([{name, value, domain, path}])` before `page.goto()`.
- After a successful 200 response that previously was 403, scan `Set-Cookie` headers + body-injected cookies and store into jar.

**Acceptance:** unit tests for TTL expiry, (host, proxy, UA) key uniqueness, concurrent-write safety (SQLite WAL mode).

---

### Phase 3 — Stealth browser pool (effort: 1-2 days)

**New file:** `ma_poc/fetch/stealth_browser.py`

A tightly-scoped Playwright pool dedicated to challenge-solving. NOT a replacement for the existing browser pool — only invoked when a hop returns `BOT_BLOCKED` with provider detected.

Configuration:
- **`playwright-stealth` plugin** ([repo](https://github.com/AtuboDad/playwright_stealth)) — masks `navigator.webdriver`, fingerprints `WebGL`, `audio`, `screen`, `permissions.query`, `chrome.runtime`, etc.
- **Identity rotation** — re-use the 8 curated identities from [fetch/stealth.py](ma_poc/fetch/stealth.py). Stealth pool MUST use the same identity as the original failed fetch so the clearance cookie matches the UA hash.
- **Residential proxy required.** Datacenter IPs (our default tier) score as bots in Cloudflare's IP intelligence. Add `PROXY_RESIDENTIAL_POOL_URL` env var; stealth pool always uses it.
- **`--disable-blink-features=AutomationControlled`** — single most-detected Playwright fingerprint.
- **Human-like interaction:** random mouse move (3-5 points), random scroll (200-800px), `page.wait_for_load_state("networkidle", timeout=20_000)`. Cloudflare's challenge-platform observes the first ~5s of interaction.
- **Single-use contexts** — never share cookies/storage across properties (defeats the per-property jar).

API:

```python
class StealthBrowserPool:
    def __init__(self, max_concurrent: int = 2, residential_proxy_url: str | None = None): ...

    async def solve_challenge(
        self,
        url: str,
        identity: StealthIdentity,
        provider: str,
        timeout_ms: int = 30_000,
    ) -> tuple[dict[str, str], FetchResult] | None:
        """Navigate to *url* with a stealth browser, solve the challenge,
        and return (clearance_cookies, post-challenge fetch_result). The
        cookies are extracted from the browser context after the challenge
        page redirects to real content. Returns None on timeout / unsolvable.

        Concurrency: bounded by max_concurrent semaphore. Cost: ~3-5s per
        solve, ~$0.001 compute + ~$0.004 proxy.
        """
```

**Acceptance:** integration test against a known Cloudflare-protected staging URL (don't hammer real customer sites in CI). Local verification by running against 5-10 PIDs from the 2026-05-18 failures and measuring solve-success-rate.

---

### Phase 4 — Wedge-rescue integration (effort: 4-6 hours)

Today's wedge-rescue (playbook §8.22) handles entry-page captchas via `wedge_rescue_decision()` in [scripts/runners/jugnu.py](ma_poc/scripts/runners/jugnu.py). Extend it to handle the hop-page case introduced by Bug #1 fix:

When `meta["all_hops_bot_blocked"]` is set AND a `captcha_provider` is known AND no cached clearance exists:
1. Pick the first hop URL that returned `BOT_BLOCKED`.
2. Invoke `StealthBrowserPool.solve_challenge(url, identity, provider)`.
3. On success, store clearance cookies into the jar via `ClearanceJar.store(...)`.
4. Re-run the link-hop cascade for THIS property only. The jar lookup in `fetch/fetcher.py` injects the cookies; the previously-blocked hops now return real content.
5. Budget cap: **1 stealth-browser invocation per property per run**. Hard cap regardless of how many hops were blocked.

Verdict reclassification: properties that successfully recover via stealth re-extraction promote from `FAILED_UNREACHABLE` to `SUCCESS` (or whatever the eventual extraction outcome is). Those that fail stealth-recovery stay `FAILED_UNREACHABLE` — accurate.

**Acceptance:**
- End-to-end test: seed local canary with 5-10 known Cloudflare-blocked PIDs; measure recovery rate.
- Cost telemetry: per-property `_stealth_solve_attempts` + `_stealth_solve_success_rate` in the run report.

---

## Cost & cost-control

| Component | Per-property cost | At 193 PIDs/day | At 1000 PIDs/day |
|---|---|---|---|
| Stealth browser compute (3-5s) | ~$0.001 | ~$0.20/day | ~$1/day |
| Residential proxy (~5 req/session) | ~$0.004 | ~$0.80/day | ~$4/day |
| Jar SQLite I/O | negligible | — | — |
| **Total** | ~$0.005 | **~$1/day** | **~$5/day** |

For comparison, today's daily LLM spend is ~$12. Stealth adds <10% to that budget while recovering ~150-200 PIDs.

Hard caps to prevent runaway cost:
- `STEALTH_MAX_INVOCATIONS_PER_PROPERTY = 1`
- `STEALTH_MAX_INVOCATIONS_PER_RUN = 500` (defends against a runaway cluster)
- Cookie jar TTL respect — never refresh an unexpired cookie

---

## Failure modes & mitigations

| Failure mode | Frequency expectation | Mitigation |
|---|---|---|
| Cloudflare ratchets bot detection | every 3-6 months | Maintenance budget. Pin `playwright-stealth` minor version; watch the project's issue tracker; have a fallback to manual cookie refresh. |
| Datacenter proxy reused by mistake | one-time setup error | Hard-fail at `StealthBrowserPool.__init__` if no residential proxy is configured. |
| Behavioural detection beyond WAF (mouse entropy, scroll velocity) | ~10-20% of cases | Phase 2 enhancement: more sophisticated interaction emulation. Out of scope for v1. |
| Challenge HARDER than managed-challenge (Turnstile, hCaptcha widget) | ~5% | Out of scope. Mark `_stealth_capability=NONE` on those properties; they stay `FAILED_UNREACHABLE`. |
| TLS-fingerprint mismatch between stealth-PW and downstream HTTP path | per-host | Use the same `curl_cffi` / `requests`-based TLS fingerprint for both paths once cookie is acquired. Existing [fetch/stealth.py](ma_poc/fetch/stealth.py) identity already pins UA + headers; extend to TLS JA3. |

---

## Rollout plan

**Week 1 — Phase 1 + Phase 2:**
- Ship WAF provider detection + cookie jar.
- Manual-bootstrap experiment: pick 5 PIDs from the Yardi `/conventional/` cluster; solve their challenges by hand (open browser, navigate, accept any popup, copy `cf_clearance` from devtools); inject into jar. Verify next cloud run recovers those 5 properties.
- Outcome: validates the cookie-replay mechanism end-to-end before investing in the pool.

**Week 2 — Phase 3:**
- Ship stealth browser pool. Run in shadow mode (computes clearance cookies, stores in jar, but doesn't gate any extraction flow). Compare stealth-acquired cookie set vs manual-bootstrap set from week 1.
- Tune until shadow recovery rate ≥ 80% of manual-bootstrap rate.

**Week 3 — Phase 4:**
- Wire wedge-rescue integration. Roll out behind `STEALTH_RESCUE_ENABLED=false` flag.
- Enable on canary shard first (10% of fleet).
- Measure: recovery rate, cost, false-positive rate (cases where stealth says solved but cookie doesn't actually work).
- Promote to 100% if recovery > 50% and cost < $5/day.

**Week 4 — Hardening:**
- Add observability dashboards.
- Document failure modes and on-call runbook.
- Cron-based jar `purge_expired()` at run boundary.

---

## Out of scope

- Replacing the existing HTTP/Playwright fetcher pool. Stealth is a narrow-purpose escalation, not a general replacement.
- CAPTCHA-solving services (2captcha, Anti-Captcha, etc.). They're $0.001-0.003/captcha but introduce a third-party dependency and a 5-20s round-trip per solve. Revisit only if Phase 3 stealth fails to crack ≥50% of WAF challenges.
- Browser fingerprint randomization beyond the 8 curated identities. Higher entropy is detectable as bot-like.

---

## File-by-file change list

| File | Change | Lines |
|---|---|---|
| `ma_poc/fetch/captcha_detect.py` | Add `detect_provider()`; populate `FetchResult._meta.captcha_provider` | +50 |
| `ma_poc/fetch/clearance_jar.py` (new) | SQLite-backed cookie jar with TTL | +180 |
| `ma_poc/fetch/stealth_browser.py` (new) | Stealth Playwright pool + challenge-solver | +220 |
| `ma_poc/fetch/fetcher.py` | Jar lookup + inject before request; jar store after 200 | +40 |
| `ma_poc/scripts/runners/jugnu.py` | Wedge-rescue extension for hop-page captcha; stealth invocation hook | +60 |
| `ma_poc/observability/events.py` | New event kinds: `STEALTH_INVOCATION`, `STEALTH_SOLVED`, `STEALTH_FAILED`, `CLEARANCE_CACHE_HIT` | +20 |
| `ma_poc/data_provider/sql/models.py` | Optional: mirror jar to Cloud SQL for cross-shard sharing | +30 |
| `ma_poc/tests/fetch/` | Phase 1 + 2 + 3 unit + integration tests | +400 |
| `ma_poc/docs/stealth_playwright_plan.md` | THIS doc | — |
| `pyproject.toml` (requirements) | `playwright-stealth>=2.0`, `curl_cffi>=0.6` | +2 |

**Total new code:** ~1,000 lines + 400 lines of tests.

---

## Open questions

1. **Residential proxy provider.** Brightdata vs Smartproxy vs Oxylabs. We already have a Brightdata account per `[.env.example](ma_poc/.env.example)`. Cheapest option for low-volume use is Smartproxy ($75/mo for 8GB). Decision needed before Phase 3.
2. **Cross-shard cookie sharing.** Today's plan keys jar by `(host, proxy_ip, ua_hash)` — a cookie acquired by shard 7 won't help shard 12 because they're on different proxy IPs. Solution: a Cloud-SQL-mirrored jar. Adds complexity but enables fleet-wide reuse. Defer to v2.
3. **Should the verdict layer auto-promote `FAILED_UNREACHABLE` → `SUCCESS` post-stealth?** Yes, but the runner needs to know to re-run extraction after stealth succeeds. Plumb via `stealth_recovery_applied` flag on the second-pass scrape result.

---

## Success metric

After Phase 4 ships:

- **Primary:** recovery rate on properties where `entry_captcha_detected=False AND all_hops_bot_blocked=True`. Target: ≥ 60% (~115 of 193 PIDs daily).
- **Secondary:** cost per recovered PID < $0.01.
- **Anti-regression:** no impact on the 4470 currently-succeeding PIDs (the stealth pool must NEVER fire for properties that are already succeeding).

These are tracked weekly in `data/reports/cloud_run_<date>/summary.md` once the implementation lands.
