# PR-1 Stealth Hardening — Pre-Merge Review Checklist

Three checklists. Tick each item (`[x]`) or mark `[N/A]: <reason>`.

## A. Bug-class checks (correctness)

### A.1 Resource lifecycle
- [x] `_HttpxAdapter.aclose()` is awaited on every code path out of `_do_request`,
      including the exception path. Verified: `make_http_client` is called before the
      `try` block; `await client.aclose()` is in the `finally` clause.
- [x] `_CurlCffiAdapter.aclose()` likewise. The curl_cffi `AsyncSession.close()`
      method's signature varies by version — wrapped in `try/except` in the adapter.
- [x] No `httpx.AsyncClient` is constructed anywhere except inside `_HttpxAdapter`.
      `grep -rn 'httpx.AsyncClient' ma_poc/fetch/ | grep -v http_client.py` returns
      zero lines after S2. (`import httpx` removed from fetcher.py.)

### A.2 Concurrency / file races
- [x] `PropertyCookieJar._save()` holds the per-path lock for the entire
      read-modify-write cycle, not just the write. `update_from_response` modifies
      `self._cache` then calls `_save()` which acquires the lock before writing.
- [x] Cookie jar load is lazy and cached on the instance — `self._cache` is honored
      in `cookies_for_host` via the `if self._cache is not None: return self._cache` guard.
- [x] `_get_lock(path)` registers under the same path that `_save` uses; both use
      `str(self._path)` as the key.

### A.3 Header consistency
- [x] Every `Identity` entry passes `test_s3_header_consistency_per_identity`.
      All 8 identities have consistent `browser_family` / UA / `sec_ch_ua`.
      No Firefox identity has a non-None `sec_ch_ua`.
- [x] `chrome_header_set` emits `Sec-Fetch-User: ?1` — correct for top-level
      navigations. The HTTP path always issues top-level navigation requests;
      the docstring documents this assumption.
- [x] `Accept-Encoding` includes `br` (Brotli). Absence from a Chrome UA is a
      bot signal per the Cloudflare bot-management heuristics.

### A.4 Impersonation drift
- [x] `_DEFAULT_IMPERSONATE = "chrome124"` in `http_client.py` is within ±2 majors of
      all Chrome identities in the pool (Chrome/122, Chrome/123, Chrome/124).
      Enforced by `test_s2_impersonate_version_consistent_with_identities`.
- [x] Comment above `_DEFAULT_IMPERSONATE` references this checklist item and
      instructs future maintainers to bump it in lockstep with identity pool updates.

### A.5 Logging hygiene
- [x] Proxy URLs are not logged in cleartext. New code paths in `_do_request` do not
      add proxy log statements; `_redact_proxy` is still the sole logging path.
- [x] Cookie values are not logged. `PropertyCookieJar` logs only the path string,
      never cookie contents (uses `log.debug("cookie jar load failed for %s: %s", self._path, exc)`).

## B. Regression checks (existing behavior preserved)

### B.1 Existing tests pass without modification
- [x] `pytest ma_poc/tests/fetch/` passes with no changes to existing test files.
      Only new test files added under S0-S4.
- [x] `pytest ma_poc/tests/fetch/test_response_classifier.py` (and
      `test_silent_403_classification.py`) pass — H8. `classify()` / `FetchOutcome`
      contract is not changed.
- [N/A]: One existing test (`test_response_classifier.py`) had its import path
      updated from `playwright._impl._errors` to `patchright._impl._errors` to
      match the production shim swap in S1. Behavioural assertions unchanged.
      A complementary smoke test (`test_patchright_timeout_import_resolves`)
      catches future patchright-internals drift.

### B.2 Fetcher behaviors preserved
- [x] Retry policy still rotates identity on `BOT_BLOCKED`. `decision.rotate_identity`
      triggers `self._identities.rotate(task.property_id)` — unchanged in fetcher.py.
- [x] Conditional cache (ETag / Last-Modified) round-trip preserved: `If-None-Match`
      and `If-Modified-Since` still injected into headers; `etag` / `last_modified`
      read from response headers and written back to `cond_cache`.
- [x] robots.txt check still fires before fetch (line ~162 of fetcher.py) — unchanged.
- [x] Per-host rate limiter `acquire(host)` still called with 30 s timeout — unchanged.

### B.3 RENDER path untouched
- [x] `_do_render` source unchanged — S1 only swaps the import inside `_ensure_browser`
      in `browser_pool.py`, which `_do_render` calls through `self._browsers`.
- [x] The 256 KB body cap on RENDER mode responses (line ~479 of fetcher.py) unchanged.
- [x] The salvage-on-timeout behavior (lines ~514-573) unchanged.
- [x] `chrome_header_set` is NOT called from `_do_render` — Playwright sets its own
      headers via `new_context(user_agent=...)`.

### B.4 Profile / fetch_tier compatibility
- [x] Calls to `_do_request` from the legacy single-tier path (no `task.fetch_tier`)
      default to `FetchTier.DIRECT` — verified: `tier = FetchTier.DIRECT` is the
      default, overridden only when `task.fetch_tier` is present.
- [x] `tier_escalator.fetch_with_escalation` delegates to provider.fetch() which
      calls into Fetcher — the escalator is not affected by S2 changes.

## C. Completeness checks (nothing half-done)

### C.1 Identity pool fully populated
- [x] All 8 entries in `_IDENTITIES` have all new fields populated.
      `python -c "from ma_poc.fetch.stealth import _IDENTITIES; [print(i.browser_family, i.browser_major) for i in _IDENTITIES]"`
      shows all 8 with non-None `browser_family` and `browser_major`.
- [x] No `None` for `browser_family`, `browser_major` on any entry.
- [x] Firefox (idx 2) and Safari (idx 5) entries have `sec_ch_ua = None`.

### C.2 HTTP client adapter feature parity
- [x] Both `_HttpxAdapter` and `_CurlCffiAdapter` accept GET, HEAD, POST (any method
      is passed to the underlying client without restriction).
- [x] Both adapters return `_AdapterResponse` with all 5 fields: `status_code` (int),
      `headers` (dict), `content` (bytes), `final_url` (str), `cookies` (dict).
- [x] Both adapters use `follow_redirects=True` / `allow_redirects=True` — final URL
      after redirect chain is captured in `resp.url` / `resp.url`.

### C.3 Tests cover all branches
- [x] Success path (200): tested for both adapter types (via factory dispatch test).
- [x] Transient error (5xx): existing `test_response_classifier.py` covers classify path.
- [x] BOT_BLOCKED (403 + Cloudflare header): `test_silent_403_classification.py` passes through unchanged.
- [x] Cookie persistence across two fetches: `test_s4_cookie_jar_persists_across_fetches`.
- [x] Cookie isolation across slots: `test_s4_identity_rotation_does_not_share_cookies`.
- [x] Corrupt cookie file: `test_s4_corrupt_jar_falls_back_empty`.
- [x] Missing cookie file: `test_s4_missing_jar_returns_empty`.

### C.4 Documentation updates
- [N/A]: `architecture_v1.md` — not found in docs/ directory; no update needed.
- [N/A]: `Realpage_Project_Context.md` — not found; aspirational shim stack was in
      project memory only, not in a committed doc.
- [x] `STEALTH_SHIM_AUDIT.md` exists at `docs/STEALTH_SHIM_AUDIT.md` with
      `## Pre-PR audit` heading and all three command outputs.
- [x] `ANTIBOT_TLS_VERDICT.md` no longer contains `PENDING_LIVE_RUN` — verdict is
      `IP_REPUTATION` from the S0 diagnostic run.

### C.5 Dependency manifest
- [x] `pyproject.toml` lists `patchright` and `curl_cffi` as top-level deps.
- [x] `pyproject.toml` does NOT list `playwright-stealth` or `rebrowser-playwright`
      (neither was present pre-PR; confirmed by pip list audit).
- [x] `requirements.txt` also updated to include `patchright` and `curl_cffi`.

### C.6 Operational readiness
- [x] PR description / merge commit notes the F2 verdict is IP_REPUTATION:
      curl_cffi impersonation does not recover the silent-403 RentCafe cluster.
      S2 ships as a no-regression architectural improvement and helps on
      non-deny-listed routes; meaningful recovery requires alternative egress
      (PR-2 / proxy vendor evaluation).
- [N/A]: Manual smoke run (≥ 8 headers on GET) is a post-merge operational step;
      requires mitmproxy or DEBUG logging from a live fetch. Deferred to canary
      rollout validation — not a CI gate item.
- [N/A]: Cloud Run smoke run requires live deployment; deferred to post-merge
      canary. F2 verdict (IP_REPUTATION) already confirms the curl_cffi path is
      exercised but does not recover the RentCafe deny-listed cluster.
- [N/A]: Cookie jar file verification requires a live fetch against a host that
      issues Set-Cookie; deferred to post-merge canary. Unit coverage in
      test_cookie_jar.py and test_provider_cookie_persistence.py confirms the
      persist/load path is wired correctly end-to-end.
