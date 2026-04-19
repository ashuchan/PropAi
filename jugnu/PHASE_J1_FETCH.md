# Phase J1 — Layer 1 Fetch

> The fetch layer turns "get this URL" into a policy-driven operation
> with retries, proxy rotation, rate limits, stealth, and HTTP-conditional
> requests. This is where the **single biggest reliability win** lives —
> the architecture doc §3.9 ranks fetch-layer fixes as the #1 priority.
>
> Reference: Jugnu architecture §4.3 (Layer 1 detail), §3.1 (gaps),
> §7.1 J1 gate.

---

## Inbound handoff (from J0)

From J0 you must have:

- `docs/JUGNU_BASELINE.md` with the current **timeout rate** and
  **failure rate** recorded. The J1 gate measures against these.
- `data/baseline/<date>.json` machine-readable with
  `totals.failure_rate_pct` and a `failure_signatures` list
  including `SCRAPE_TIMEOUT`, `ERR_SSL_PROTOCOL_ERROR`, `407` patterns.

If any of these are missing, stop and complete J0 before starting J1.

---

## What J1 delivers

A new `ma_poc/fetch/` package that exposes **one public function**:

```python
# ma_poc/fetch/__init__.py
async def fetch(task: CrawlTask) -> FetchResult: ...
```

Everything else in the package is private implementation. Callers above
(L2, L3) import only `fetch()` and the contract types.

The 04-17 run showed ~55% of issues are fetch-layer problems. After J1,
the J1 gate measures the limit-50 run's timeout rate and requires it
to drop below 10%.

---

## Module breakdown (one responsibility per file)

The fetch layer is big. We split it into small modules with clear
boundaries so each one is independently testable. No single file over
~400 lines.

```
ma_poc/fetch/
├── __init__.py             # Re-exports fetch() and contract types
├── contracts.py            # FetchResult, FetchOutcome, RenderMode
├── fetcher.py              # Orchestrator — assembles the other components
├── retry_policy.py         # Exponential backoff, attempt budgeting
├── proxy_pool.py           # Pool with health tracking and rotation
├── rate_limiter.py         # Per-host token bucket (robots Crawl-delay aware)
├── stealth.py              # UA/TLS/cookie identity selection
├── conditional.py          # ETag / Last-Modified cache lookup and write
├── response_classifier.py  # status → FetchOutcome mapping
├── browser_pool.py         # Playwright context pool (for RENDER mode)
├── robots.py               # robots.txt consumer with in-memory TTL cache
└── captcha_detect.py       # Cloudflare / recaptcha / hCaptcha fingerprints
```

### Responsibilities by module (strict — SRP)

| Module | Owns | Knows nothing about |
|---|---|---|
| `contracts.py` | Dataclass shapes only | Anything stateful |
| `fetcher.py` | Assembly — the top-level `fetch()` function | HTTP details inside each component |
| `retry_policy.py` | When and how to retry | Proxy, rate, body |
| `proxy_pool.py` | Pool of proxies, health scores, sticky sessions | Request shape |
| `rate_limiter.py` | Token bucket per host | What's being fetched |
| `stealth.py` | Identity selection (UA/TLS/cookies) | URL, response |
| `conditional.py` | Cache read/write of ETag+LastModified | HTTP, retries |
| `response_classifier.py` | `(status, headers, body-head) → FetchOutcome` | How response was obtained |
| `browser_pool.py` | Playwright context lifecycle | What URL to fetch |
| `robots.py` | `robots.txt` fetch + Crawl-delay parse, 24h TTL | Request scheduling |
| `captcha_detect.py` | Inspects body fragment for CAPTCHA markers | Response transport |

---

## File creation order

Do in this order. Each file: implement → test → lint → commit-ready.

1. `ma_poc/fetch/__init__.py` (empty shell, filled at end)
2. `ma_poc/fetch/contracts.py` — see `JUGNU_CONTRACTS.md` §1
3. `ma_poc/fetch/response_classifier.py`
4. `ma_poc/fetch/conditional.py`
5. `ma_poc/fetch/retry_policy.py`
6. `ma_poc/fetch/rate_limiter.py`
7. `ma_poc/fetch/stealth.py`
8. `ma_poc/fetch/proxy_pool.py`
9. `ma_poc/fetch/robots.py`
10. `ma_poc/fetch/captcha_detect.py`
11. `ma_poc/fetch/browser_pool.py`
12. `ma_poc/fetch/fetcher.py` — the orchestrator, assembles all of the above
13. Populate `ma_poc/fetch/__init__.py` with re-exports

Test files mirror the module layout:

```
tests/fetch/
  test_contracts.py
  test_response_classifier.py
  test_conditional.py
  test_retry_policy.py
  test_rate_limiter.py
  test_stealth.py
  test_proxy_pool.py
  test_robots.py
  test_captcha_detect.py
  test_browser_pool.py
  test_fetcher.py           # integration across the whole layer
```

---

## Module specifications

### 3. `response_classifier.py`

Pure function, no I/O. Input: `(status: int, headers: dict, body_head: bytes)`.
Output: `FetchOutcome`.

```python
def classify(
    status: int | None,
    headers: dict[str, str],
    body_head: bytes | None,
    exception: Exception | None = None,
) -> tuple[FetchOutcome, str | None]:
    """
    Returns (outcome, error_signature_or_none).

    Rules (in order):
      - exception is SSLError or DNS failure → HARD_FAIL, "ERR_SSL_PROTOCOL_ERROR" or "ERR_DNS"
      - status == 304 → NOT_MODIFIED
      - status is None and exception → TRANSIENT, "timeout" / classname
      - status == 407 → PROXY_ERROR
      - status == 429 → RATE_LIMITED
      - status in {403} and captcha_detect.looks_like_captcha(body_head) → BOT_BLOCKED
      - 500 ≤ status < 600 → TRANSIENT
      - 2xx → OK
      - 4xx (other) → HARD_FAIL, f"HTTP_{status}"
    """
```

**Named tests (10):**

| Test | Input | Expected |
|---|---|---|
| `test_classify_ok_200` | (200, {}, b"<html>") | (OK, None) |
| `test_classify_not_modified` | (304, {}, None) | (NOT_MODIFIED, None) |
| `test_classify_proxy_407` | (407, {}, b"") | (PROXY_ERROR, "HTTP_407") |
| `test_classify_rate_limited` | (429, {"retry-after": "10"}, b"") | (RATE_LIMITED, _) |
| `test_classify_captcha_cloudflare` | (403, {}, b"...Cloudflare...") | (BOT_BLOCKED, "CF_CHALLENGE") |
| `test_classify_5xx_transient` | (503, {}, b"") | (TRANSIENT, _) |
| `test_classify_ssl_error` | exception=SSLError → | (HARD_FAIL, "ERR_SSL_PROTOCOL_ERROR") |
| `test_classify_dns_error` | exception=gaierror → | (HARD_FAIL, "ERR_DNS") |
| `test_classify_timeout_exception` | exception=asyncio.TimeoutError → | (TRANSIENT, "timeout") |
| `test_classify_404_hard_fail` | (404, {}, b"") | (HARD_FAIL, "HTTP_404") |

### 4. `conditional.py`

SQLite-backed cache of `(url → (etag, last_modified, fetched_at))`.
The `fetched_at` column lets the cache self-expire after 7 days
(renders are forced regardless of 304 after that — a safety net for
stale parsers).

```python
class ConditionalCache:
    def __init__(self, db_path: Path) -> None: ...
    def read(self, url: str) -> tuple[str | None, str | None]: ...
    def write(self, url: str, etag: str | None, last_modified: str | None) -> None: ...
    def expire_older_than(self, days: int) -> int: ...
```

**Named tests (5):**

- `test_cond_cache_roundtrip_etag`
- `test_cond_cache_roundtrip_last_modified`
- `test_cond_cache_read_missing_url_returns_nones`
- `test_cond_cache_expire_removes_stale`
- `test_cond_cache_thread_safe_writes` (use `ThreadPoolExecutor` fuzz test)

### 5. `retry_policy.py`

Pure logic — no sleeps during tests (tests pass a `clock` callable).

```python
@dataclass(frozen=True)
class RetryDecision:
    should_retry: bool
    wait_ms: int
    rotate_identity: bool

class RetryPolicy:
    def __init__(self, max_attempts: int = 3, base_ms: int = 500) -> None: ...
    def decide(self, outcome: FetchOutcome, attempt: int, retry_after_header: str | None) -> RetryDecision: ...
```

Rules:
- `OK`, `NOT_MODIFIED`, `HARD_FAIL` → no retry.
- `TRANSIENT` → retry up to `max_attempts` with `base_ms * 2^(attempt-1)` jittered ±25%.
- `RATE_LIMITED` → respect `Retry-After` header (seconds); retry once.
- `BOT_BLOCKED` → retry once **with identity rotation**.
- `PROXY_ERROR` → retry up to 2 times, each with a fresh proxy.

**Named tests (7):**

- `test_retry_ok_no_retry`
- `test_retry_hard_fail_no_retry`
- `test_retry_transient_schedules_backoff`
- `test_retry_rate_limited_honours_retry_after`
- `test_retry_bot_blocked_rotates_identity`
- `test_retry_proxy_error_rotates_proxy_twice`
- `test_retry_exhausts_after_max_attempts`

### 6. `rate_limiter.py`

Async token bucket per host. `robots.txt` Crawl-delay sets the refill
rate; default is 2 requests/second per host.

```python
class HostRateLimiter:
    def __init__(self, default_rps: float = 2.0) -> None: ...
    def set_crawl_delay(self, host: str, delay_sec: float) -> None: ...
    async def acquire(self, host: str) -> None:
        """Blocks until the host's bucket has a token."""
```

**Named tests (5):**

- `test_rate_limiter_allows_burst_within_capacity`
- `test_rate_limiter_blocks_once_exhausted`
- `test_rate_limiter_refills_over_time` (use an injected clock)
- `test_rate_limiter_crawl_delay_overrides_default`
- `test_rate_limiter_two_hosts_independent`

### 7. `stealth.py`

Returns an `Identity` to use for the next request.

```python
@dataclass(frozen=True)
class Identity:
    user_agent: str
    accept_language: str
    platform: str
    viewport: tuple[int, int]

class IdentityPool:
    """Rotates through a curated list of plausible identities."""
    def pick(self, sticky_key: str | None = None) -> Identity: ...
    def rotate(self, sticky_key: str) -> None: ...
```

Curated identity list stored as a Python literal (5–10 entries); **no
LLM-generated UA strings**, only real Chrome/Firefox/Edge on major
platforms. Sticky key is typically `property_id` so repeat scrapes of
the same property look like the same browser.

**Named tests (4):**

- `test_identity_pool_picks_deterministically_for_sticky_key`
- `test_identity_pool_rotate_changes_pick`
- `test_identity_pool_uas_are_realistic` (assert every UA contains
  "Mozilla/5.0" and a real version number format)
- `test_identity_pool_no_duplicate_entries`

### 8. `proxy_pool.py`

Health-weighted random selection. Starts with proxies from
`PROXY_POOL_URLS` env var (comma-separated). Each proxy starts at
health=1.0; a failure drops it 0.25 (min 0.1); a success boosts it
0.05 (max 1.0). A proxy at health<0.25 is skipped.

```python
@dataclass
class ProxyHealth:
    url: str                  # credentials redacted on repr
    health: float
    consecutive_failures: int
    last_used: datetime

class ProxyPool:
    def __init__(self, urls: list[str]) -> None: ...
    def pick(self, sticky_key: str | None = None) -> str | None:
        """None if pool is empty or all proxies are quarantined."""
    def mark_success(self, proxy_url: str) -> None: ...
    def mark_failure(self, proxy_url: str, reason: str) -> None: ...
    def health_snapshot(self) -> list[dict]: ...  # for L5 dashboards
```

**Abstract over provider.** `ma_poc/fetch/proxy_pool.py` is the
interface; a `ma_poc/fetch/providers/bright_data.py` and
`ma_poc/fetch/providers/zyte.py` (stubs for now) translate
provider-specific auth if needed. The architecture doc §8 flags this
as open; implementing both stubs now keeps the door open without
committing.

**Named tests (7):**

- `test_proxy_pool_empty_returns_none`
- `test_proxy_pool_picks_healthiest`
- `test_proxy_pool_failure_drops_health`
- `test_proxy_pool_success_raises_health`
- `test_proxy_pool_quarantines_after_low_health`
- `test_proxy_pool_sticky_key_returns_same_proxy_twice`
- `test_proxy_pool_repr_redacts_credentials`

### 9. `robots.py`

```python
class RobotsConsumer:
    def __init__(self, cache_ttl_hours: int = 24) -> None: ...
    async def is_allowed(self, url: str, user_agent: str) -> bool: ...
    async def crawl_delay(self, host: str, user_agent: str) -> float | None: ...
```

Uses `urllib.robotparser` — no custom parser. Cache per host. On fetch
failure of `robots.txt`, default to **allow** (consistent with being a
well-behaved but practical crawler).

**Named tests (5):**

- `test_robots_blocks_disallowed_path`
- `test_robots_allows_when_file_is_404`
- `test_robots_extracts_crawl_delay`
- `test_robots_cache_ttl_honours`
- `test_robots_different_user_agents_different_answers`

### 10. `captcha_detect.py`

Pure function. Input: body bytes (first ~4KB is enough). Output: bool
+ provider.

```python
def looks_like_captcha(body: bytes) -> tuple[bool, str | None]:
    """Returns (is_captcha, provider_name_or_none)."""
```

Provider fingerprints (research-backed — document source in
top-of-file comment):

- Cloudflare: `b"challenge-platform"` or `b"__cf_chl_"` or
  `b"Just a moment..."`
- reCAPTCHA: `b"g-recaptcha"` or `b"www.google.com/recaptcha"`
- hCaptcha: `b"hcaptcha.com"` or `b"h-captcha"`
- PerimeterX: `b"_pxhd"` or `b"PerimeterX"`

**Named tests (5):**

- `test_captcha_cloudflare`
- `test_captcha_recaptcha`
- `test_captcha_hcaptcha`
- `test_captcha_clean_html_returns_false`
- `test_captcha_on_binary_garbage_returns_false_safely`

### 11. `browser_pool.py`

Manages a small pool of Playwright browser contexts. Existing
`scripts/concurrency.py` already manages the **process-level** pool;
this is the **context-level** pool inside a worker.

```python
class BrowserContextPool:
    def __init__(self, max_contexts: int = 1) -> None: ...
    async def acquire(self, identity: Identity, proxy: str | None) -> Page: ...
    async def release(self, page: Page) -> None: ...
    async def close(self) -> None: ...
```

**Use `context.close()` not `browser.close()`** (existing convention).
Each property gets its own context, torn down after use, but the
browser is reused. This is already a pattern in
`ma_poc/scripts/entrata.py` — lift the same discipline.

**Named tests (4) — use Playwright mock:**

- `test_browser_pool_reuses_browser_across_contexts`
- `test_browser_pool_applies_identity_headers`
- `test_browser_pool_applies_proxy`
- `test_browser_pool_close_cleans_up_contexts`

### 12. `fetcher.py` — the orchestrator

This is the entry point the rest of Jugnu calls. Keep it thin — it
*assembles* the components; all the real logic is in them.

```python
# ma_poc/fetch/fetcher.py
class Fetcher:
    def __init__(
        self,
        proxy_pool: ProxyPool,
        rate_limiter: HostRateLimiter,
        robots: RobotsConsumer,
        cond_cache: ConditionalCache,
        identities: IdentityPool,
        browsers: BrowserContextPool,
        retry: RetryPolicy,
    ) -> None: ...

    async def fetch(self, task: CrawlTask) -> FetchResult:
        """Top-level entry. Never raises on transient errors.

        Flow (matches architecture doc §4.3):
          1. robots allow-check
          2. cond cache lookup → if match, return NOT_MODIFIED
          3. rate-limiter acquire(host)
          4. identity + proxy selection (sticky on property_id)
          5. issue request: HEAD / GET / RENDER
          6. classify response
          7. on transient/bot/proxy: retry with rotation
          8. on OK: write etag+last_modified to cond cache
          9. build and return FetchResult
        """

# Module-level singleton factory used by callers that don't want DI.
_default: Fetcher | None = None
def get_default_fetcher() -> Fetcher: ...
async def fetch(task: CrawlTask) -> FetchResult:
    return await get_default_fetcher().fetch(task)
```

**Key invariant:** `Fetcher.fetch()` never raises. A disk I/O error
from the cond cache is swallowed with a log warning; the fetch
proceeds as a miss. A retry-exhausted failure returns a
`FetchResult` with `outcome in {TRANSIENT, HARD_FAIL, BOT_BLOCKED}`
and `body=None`.

### `fetcher.py` — integration tests

These hit mocked HTTP endpoints via `aiohttp` test servers; no live
network.

| Test | Scenario |
|---|---|
| `test_fetcher_head_to_304_returns_not_modified` | cond cache has etag; mock returns 304 |
| `test_fetcher_transient_503_retries_and_succeeds` | two 503s then 200 |
| `test_fetcher_captcha_rotates_identity` | first attempt returns CF page; second succeeds under new UA |
| `test_fetcher_proxy_407_rotates_proxy` | first proxy errors, second succeeds |
| `test_fetcher_ssl_error_short_circuits` | mock raises SSL error; outcome==HARD_FAIL, attempts==1 |
| `test_fetcher_robots_disallow_does_not_fetch` | robots.txt disallows; returns outcome==HARD_FAIL, status==None |
| `test_fetcher_honours_retry_after_on_429` | 429 with Retry-After: 2 → waits ≥ 2s (inject clock, assert elapsed) |
| `test_fetcher_render_mode_returns_network_log` | mocked Playwright returns 3 XHR responses; `FetchResult.network_log` has 3 entries |
| `test_fetcher_never_raises_on_any_input` | fuzz with 20 pathological URLs (binary, `javascript:`, `""`) — all return a FetchResult |

---

## Cross-cutting concerns

### Event emission

Every non-trivial step in `fetcher.py` emits an event. These are
consumed by L5 in J5, but the **emission points are added now**. Use a
lightweight module-level helper:

```python
# ma_poc/observability/events.py  (created in J5; stub file for J1)
def emit(kind: EventKind, property_id: str, **data) -> None: ...
```

Events emitted by L1 (names fixed; J5 will consume):

- `fetch.started`  — on each attempt, with `attempt`, `url`, `render_mode`
- `fetch.completed` — on each attempt, with `outcome`, `status`, `elapsed_ms`
- `fetch.cache_hit` — when cond cache returns 304 without a network call
- `fetch.retry` — on each retry decision, with `wait_ms`, `reason`
- `fetch.rotated_identity` — when identity changes mid-property
- `fetch.bot_blocked` — on first CAPTCHA detection

For J1, emit these via a stub `emit()` that writes to `logging.info`.
J5 will replace the stub with a real ledger writer.

### Raw-HTML storage for replay (J5 dep)

When `FetchResult.outcome == OK` and `render_mode == RENDER`, write the
body to `data/raw_html/<date>/<property_id>.html.gz`. The replay tool
in J5 reads this directory. Keep 30 days; a nightly cron (out of
scope for J1) prunes.

Use a small helper in `ma_poc/fetch/fetcher.py`:

```python
def _persist_raw_html(property_id: str, body: bytes) -> None: ...
```

Fail silently on disk errors — persistence is best-effort.

---

## Refactoring / code-quality checklist

- [ ] No module over 400 lines. If a module grows past that, split it
      further — that's a sign two concerns snuck into one file.
- [ ] Every public function has a type hint and a docstring.
- [ ] `mypy --strict ma_poc/fetch/` clean.
- [ ] `ruff check ma_poc/fetch/` clean.
- [ ] No circular imports — verify with `python -c "import ma_poc.fetch"`
      after each file.
- [ ] Every public class has an `__init__` that lists its dependencies
      as constructor params (no hidden globals, no import-time side
      effects). This makes unit tests trivial.
- [ ] Test coverage ≥ 85% on `ma_poc/fetch/`.
- [ ] No `print()` anywhere in `ma_poc/fetch/`.
- [ ] No `asyncio.sleep()` outside `retry_policy.py` and
      `rate_limiter.py`; tests must be able to inject a clock.

---

## Gate — `scripts/gate_jugnu.py phase 1`

Passes iff:

- All module-level tests pass (~60 tests across the 10 submodules).
- All 9 `fetcher.py` integration tests pass.
- `ruff check ma_poc/fetch/` clean.
- `mypy --strict ma_poc/fetch/` clean.
- Coverage ≥ 85% on `ma_poc/fetch/`.
- **Observable check:** running `scripts/daily_runner.py --limit 50`
  against the property CSV, with Jugnu fetch routed via the `fetch()`
  entry point (L2 still mocked — see below), produces a timeout rate
  **below 10%**. Compare to `baseline.timeout_rate_pct` from J0.
- **Observable check:** no property in the --limit 50 run crashes
  the runner. The never-fail contract is intact.

### How to run the J1 observable check when L2 isn't built yet

For J1's gate only, add a **shim in `scripts/daily_runner.py`**:

```python
# TEMPORARY J1 SHIM — replaced by the real L2 in J2
from ma_poc.fetch import fetch as jugnu_fetch
from ma_poc.fetch.contracts import RenderMode
from ma_poc.discovery.contracts import CrawlTask, TaskReason

async def _j1_shim_fetch(url: str, property_id: str) -> FetchResult:
    return await jugnu_fetch(CrawlTask(
        url=url, property_id=property_id, priority=0, budget_ms=180_000,
        reason=TaskReason.SCHEDULED, render_mode=RenderMode.RENDER,
    ))
```

And plumb this through the existing `_scrape_in_thread` path so the
--limit 50 run uses the new fetcher to load the homepage. J2 replaces
the shim with the real scheduler.

---

## Outbound handoff (to J2 Discovery)

- **Function** `ma_poc.fetch.fetch(task: CrawlTask) -> FetchResult` —
  stable, tested, usable by L2.
- **Dataclasses** `FetchResult`, `FetchOutcome`, `RenderMode` in
  `ma_poc.fetch.contracts`.
- **Directory** `data/raw_html/<date>/` with today's run's bodies
  stored — J5 replay consumes this.
- **Directory** `data/cache/` with `conditional.sqlite` — L1's cond
  cache; never touched outside `ma_poc.fetch.conditional`.
- **Event names** fixed (`fetch.*`) — J5 consumes these.
- **Baseline delta:** `docs/JUGNU_BASELINE.md` appended with a "J1
  observed" row showing the new timeout rate on --limit 50.

Commit message: `Jugnu J1: fetch layer — retry, proxy, stealth,
conditional GET, rate limit`.

---

*Next: `PHASE_J2_DISCOVERY.md`.*
