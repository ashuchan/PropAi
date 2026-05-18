"""Fetcher orchestrator — the top-level fetch() function.

Assembles all fetch-layer components: retry, proxy, stealth, rate limiting,
conditional GET, robots.txt, CAPTCHA detection, and Playwright rendering.

Never raises on transient errors. Always returns a FetchResult.
"""

from __future__ import annotations

import asyncio
import dataclasses
import gzip
import logging
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

if TYPE_CHECKING:
    from ..models.scrape_profile import ScrapeProfile

from ..config.feature_flags import ENABLE_TIER_ESCALATION
from ..discovery.contracts import CrawlTask
from ..observability.events import EventKind, emit
from .browser_pool import BrowserContextPool
from .captcha_detect import looks_like_captcha
from .clearance_jar import (
    CLEARANCE_COOKIE_NAMES,
    ClearanceJar,
    _PROVIDER_DEFAULT_TTL,
    _FALLBACK_TTL,
    _extract_clearance_from_set_cookie,
    ua_hash as _ua_hash,
)
from .conditional import ConditionalCache
from .contracts import FetchOutcome, FetchResult, FetchTier, RenderMode
from .cookie_jar import PropertyCookieJar
from .headers import chrome_header_set
from .http_client import make_http_client
from .proxy_pool import ProxyPool
from .rate_limiter import HostRateLimiter
from .response_classifier import (
    _has_cloudflare_signature,
    _is_silent_block,
    classify,
)
from .retry_policy import RetryPolicy
from .robots import RobotsConsumer
from .stealth import Identity, IdentityPool


# F3 — re-exported so the spec-aligned test path
# (tests/fetch/test_silent_403_classification.py) imports them from
# ma_poc.fetch.fetcher. Single source of truth still lives in
# response_classifier.py.
__all__ = [
    "Fetcher",
    "FetchOutcome",
    "_classify_fetch_outcome",
    "_has_cloudflare_signature",
    "_is_silent_block",
    "get_default_fetcher",
]


# Fix 7 — in-memory per-host learning of which hosts need late-render
# re-capture. Populated when a portal-aware re-capture (Fix B + Fix 5)
# successfully grew the body. Subsequent fetches to the same host get
# a 5-sec preemptive wait without needing portal-host detection. Set
# is per-shard (process-local); each shard learns its own slice.
_LATE_RENDER_HOSTS: set[str] = set()


# Bug #5 Layer 2 (2026-05-18) — after this many consecutive RATE_LIMITED
# responses from one host, quarantine the host for the rest of the shard
# run. Single conservative threshold: 3 attempts gives the rate-limiter
# (Layer 1) time to decay the rps and gives proxy rotation a fair shot
# before we give up on the host. Tunable via FETCH_DOMAIN_QUARANTINE_429
# env var for staging experiments.
_DOMAIN_QUARANTINE_THRESHOLD: int = int(
    os.environ.get("FETCH_DOMAIN_QUARANTINE_429", "3") or "3"
)

# C1 — in-flight XHR quiescence: URLs matching these patterns carry no
# apartment unit data and must not block the quiescence counter.
_C1_STATIC_SUFFIXES: frozenset[str] = frozenset({
    ".js", ".css", ".woff", ".woff2", ".ttf", ".otf",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico",
    ".webp", ".mp4", ".m4v", ".map",
})
_C1_ANALYTICS_SUBSTRINGS: tuple[str, ...] = (
    "google-analytics", "googletagmanager", "facebook.com/tr",
    "doubleclick", "hotjar.com", "segment.io", "mixpanel.com",
    "heap.io", "intercom", "drift.com", "hubspot.com",
    "crisp.chat", "zopim", "tawk.to", "chatlio",
    "sentry.io", "bugsnag.com", "rollbar.com",
    "fonts.googleapis.com", "fonts.gstatic.com",
)
_C1_QUIESCE_IDLE_S: float = 0.5    # seconds of silence → network done
_C1_QUIESCE_MAX_S: float = 8.0     # absolute hard cap


def _c1_is_relevant_request(url: str) -> bool:
    """Return True if this request could carry apartment unit data.

    Filters out static assets and analytics/tracking services that fire
    indefinitely and would otherwise prevent the quiescence counter from
    reaching zero.
    """
    lower = url.lower().split("?")[0]
    suffix = "." + lower.rsplit(".", 1)[-1] if "." in lower else ""
    if suffix in _C1_STATIC_SUFFIXES:
        return False
    return not any(s in lower for s in _C1_ANALYTICS_SUBSTRINGS)


def _classify_fetch_outcome(
    status_code: int | None,
    headers: dict[str, str] | None,
    body: bytes | str | None,
    error: Exception | None,
) -> tuple[FetchOutcome, str | None]:
    """F3 — thin wrapper around ``classify`` for the silent-403 test path.

    Differs from ``classify`` only in argument order/naming so the spec
    test reads naturally. ``body`` may be the full response body (str or
    bytes) — the underlying classifier only inspects the head.
    """
    head: bytes | None
    if isinstance(body, bytes):
        head = body[:4096]
    elif isinstance(body, str):
        head = body[:4096].encode("utf-8", errors="replace")
    else:
        head = None
    return classify(status_code, headers or {}, head, exception=error)

_MA_POC_ROOT = Path(__file__).resolve().parent.parent  # ma_poc/
_DEFAULT_DATA_DIR = str(_MA_POC_ROOT / "data")

log = logging.getLogger(__name__)


class Fetcher:
    """Top-level fetch orchestrator.

    Composes all L1 components. Never raises on transient errors.

    Args:
        proxy_pool: Pool of proxies with health tracking.
        rate_limiter: Per-host token bucket rate limiter.
        robots: robots.txt consumer.
        cond_cache: Conditional GET cache (ETag/Last-Modified).
        identities: Browser identity pool.
        browsers: Playwright context pool (for RENDER mode).
        retry: Retry policy.
    """

    def __init__(
        self,
        proxy_pool: ProxyPool,
        rate_limiter: HostRateLimiter,
        robots: RobotsConsumer,
        cond_cache: ConditionalCache,
        identities: IdentityPool,
        browsers: BrowserContextPool,
        retry: RetryPolicy,
        clearance_jar: ClearanceJar | None = None,
    ) -> None:
        self._proxy_pool = proxy_pool
        self._rate_limiter = rate_limiter
        self._robots = robots
        self._cond_cache = cond_cache
        self._identities = identities
        self._browsers = browsers
        self._retry = retry
        # Phase 2 (stealth plan): optional WAF clearance cookie store.
        # When set, clearance cookies are injected before requests and
        # captured opportunistically from Set-Cookie headers after 200s.
        self._clearance_jar = clearance_jar
        # Bug #5 Layer 2 (2026-05-18): per-shard in-job domain quarantine.
        # When a host returns N consecutive RATE_LIMITED responses, the
        # host is parked for the REST of this shard's run. Any subsequent
        # fetch to a quarantined host returns RATE_LIMITED immediately
        # (no actual request fires), letting downstream property emit
        # FAILED_UNREACHABLE with the correct verdict without burning
        # proxy/rate-limit budget on a hopeless retry. Cheaper than DLQ
        # parking (which assumes hourly retry — useless inside a 20-min
        # job). Reset implicit at next run-startup (the fetcher is
        # constructed fresh per shard process).
        self._domain_429_consecutive: dict[str, int] = {}
        self._domain_quarantined: set[str] = set()

    async def fetch(self, task: CrawlTask, profile: "ScrapeProfile | None" = None) -> FetchResult:
        """Top-level entry. Never raises on transient errors.

        When ENABLE_TIER_ESCALATION is True and a profile is supplied, delegates
        to fetch_with_escalation() which runs the full tier ladder.  Otherwise
        falls through to the existing single-tier fetch loop.

        Flow:
          1. robots allow-check
          2. cond cache lookup -> if match, return NOT_MODIFIED
          3. rate-limiter acquire(host)
          4. identity + proxy selection (sticky on property_id)
          5. issue request: HEAD / GET / RENDER
          6. classify response
          7. on transient/bot/proxy: retry with rotation
          8. on OK: write etag+last_modified to cond cache
          9. build and return FetchResult

        Args:
            task: The CrawlTask describing what to fetch.
            profile: ScrapeProfile for tier-escalation routing (optional).

        Returns:
            A FetchResult. Never raises.
        """
        if ENABLE_TIER_ESCALATION and profile is not None:
            from .tier_escalator import fetch_with_escalation
            return await fetch_with_escalation(task, profile)
        start_ms = _now_ms()
        host = urlparse(task.url).netloc

        # Bug #5 Layer 2 (2026-05-18): in-job domain quarantine. If this
        # host has already racked up DOMAIN_QUARANTINE_THRESHOLD consecutive
        # RATE_LIMITED responses earlier in the same shard run, short-circuit
        # to RATE_LIMITED without issuing the actual request. Saves proxy
        # budget + shard wallclock on hopeless retries; Essex's 27 PIDs on
        # one shard previously each took ~3 attempts × ~5s = 15s+ before
        # giving up.
        if host in self._domain_quarantined:
            emit(
                EventKind.FETCH_COMPLETED,
                task.property_id,
                url=task.url,
                outcome=FetchOutcome.RATE_LIMITED.value,
                elapsed_ms=0,
                body_bytes=0,
                error_signature="DOMAIN_QUARANTINED_IN_RUN",
            )
            return FetchResult(
                url=task.url,
                outcome=FetchOutcome.RATE_LIMITED,
                status_code=429,
                headers={},
                body=None,
                final_url=task.url,
                elapsed_ms=0,
                render_mode=task.render_mode,
                identity_key=None,
                proxy_label=None,
                error_signature="DOMAIN_QUARANTINED_IN_RUN",
                etag=None,
                last_modified=None,
            )

        identity = self._identities.pick(sticky_key=task.property_id)
        proxy = self._proxy_pool.pick(sticky_key=task.property_id)

        emit(
            EventKind.FETCH_STARTED,
            task.property_id,
            url=task.url,
            render_mode=task.render_mode.value,
            attempt=1,
        )

        # 1. robots check
        try:
            allowed = await self._robots.is_allowed(task.url, identity.user_agent)
            if not allowed:
                return FetchResult(
                    url=task.url,
                    outcome=FetchOutcome.HARD_FAIL,
                    status=None,
                    body=None,
                    headers={},
                    render_mode=task.render_mode,
                    final_url=task.url,
                    attempts=0,
                    elapsed_ms=_now_ms() - start_ms,
                    error_signature="ROBOTS_DISALLOWED",
                )
        except Exception:
            pass  # Default to allow on robots error

        # 2. conditional cache check for HEAD/GET
        if task.render_mode in (RenderMode.HEAD, RenderMode.GET):
            try:
                cached_etag, cached_lm = self._cond_cache.read(task.url)
                if task.etag:
                    cached_etag = task.etag
                if task.last_modified:
                    cached_lm = task.last_modified
            except Exception:
                cached_etag, cached_lm = None, None
        else:
            cached_etag, cached_lm = None, None

        # Retry loop
        attempt = 0
        last_result: FetchResult | None = None
        while True:
            attempt += 1
            # 3. Rate limit
            try:
                await asyncio.wait_for(self._rate_limiter.acquire(host), timeout=30.0)
            except TimeoutError:
                pass

            # 4-5. Issue request
            #
            # RC-A (2026-05-15 PM): catch asyncio.CancelledError so we emit a
            # synthetic FETCH_COMPLETED before the cancellation propagates up
            # to the per-property guard at scripts/runners/jugnu.py. Without
            # this, when `asyncio.wait_for(_process_property, 600s)` fires
            # while we're parked inside Playwright IPC (page.goto, page.content,
            # the 20s CF auto-solve sleep, or a wedged `context.close()`),
            # CancelledError flies past the bare `except Exception` below
            # (BaseException since Python 3.8) and the FETCH_COMPLETED emit
            # 40 lines down is never reached. The PID then shows only
            # `fetch.started` in events.jsonl — invisible to the analyzer.
            # Shard 64 on 2026-05-15 had 29/50 PIDs killed this way (see
            # data/reports/cloud_run_2026-05-15/TRIAGE.md RC-A). On
            # cancellation we synthesise a FetchResult with outcome=CANCELLED
            # so the existing telemetry path runs, then re-raise.
            try:
                result = await self._do_request(
                    task,
                    identity,
                    proxy,
                    cached_etag,
                    cached_lm,
                    attempt,
                    start_ms,
                )
            except asyncio.CancelledError:
                cancel_result = FetchResult(
                    url=task.url,
                    outcome=FetchOutcome.CANCELLED,
                    status=None,
                    body=None,
                    headers={},
                    render_mode=task.render_mode,
                    final_url=task.url,
                    attempts=attempt,
                    elapsed_ms=_now_ms() - start_ms,
                    error_signature="per_property_timeout_or_cancel",
                    proxy_used=_redact_proxy(proxy),
                )
                try:
                    emit(
                        EventKind.FETCH_COMPLETED,
                        task.property_id,
                        outcome=cancel_result.outcome.value,
                        status=None,
                        elapsed_ms=cancel_result.elapsed_ms,
                        attempt=attempt,
                        error_signature=cancel_result.error_signature,
                        final_url=cancel_result.final_url,
                        body_bytes=0,
                        content_type="",
                        captcha_detected=False,
                        captcha_provider=None,
                        proxy_used=cancel_result.proxy_used,
                        identity_ua_hash=_short_hash(identity.user_agent),
                        render_mode=cancel_result.render_mode.value,
                    )
                except Exception:
                    pass  # never let telemetry mask the cancel
                raise
            except Exception as exc:
                outcome, sig = classify(None, {}, None, exception=exc)
                result = FetchResult(
                    url=task.url,
                    outcome=outcome,
                    status=None,
                    body=None,
                    headers={},
                    render_mode=task.render_mode,
                    final_url=task.url,
                    attempts=attempt,
                    elapsed_ms=_now_ms() - start_ms,
                    error_signature=sig,
                    proxy_used=_redact_proxy(proxy),
                )

            # Telemetry B + F: emit diagnostic-rich FETCH_COMPLETED so the
            # report/report can distinguish TLS vs timeout vs CAPTCHA vs
            # bot-wall, and so we can see which proxy+identity was used.
            body_bytes_len = len(result.body) if result.body else 0
            content_type = (result.headers or {}).get("content-type", "")
            captcha_detected = False
            captcha_provider: str | None = None
            if result.body:
                try:
                    captcha_detected, captcha_provider = looks_like_captcha(result.body)
                except Exception:
                    captcha_detected, captcha_provider = False, None

            # F1.2 (2026-05-08 plan): propagate captcha_detected onto the
            # FetchResult itself so the orchestrator's rescue gate at
            # pms/scraper.py:574 can short-circuit on interstitial bodies
            # without re-running the detector. FetchResult is frozen, so we
            # rebuild via dataclasses.replace. Pre-F1.2 the value lived only
            # in this local + the FETCH_COMPLETED event payload — making the
            # rescue gate's getattr() always read False in production.
            if captcha_detected and not result.captcha_detected:
                result = dataclasses.replace(result, captcha_detected=True)

            emit(
                EventKind.FETCH_COMPLETED,
                task.property_id,
                outcome=result.outcome.value,
                status=result.status,
                elapsed_ms=result.elapsed_ms,
                attempt=attempt,
                error_signature=result.error_signature,
                final_url=result.final_url,
                body_bytes=body_bytes_len,
                content_type=content_type,
                captcha_detected=captcha_detected,
                captcha_provider=captcha_provider,
                proxy_used=result.proxy_used,
                identity_ua_hash=_short_hash(identity.user_agent),
                render_mode=result.render_mode.value,
            )

            if captcha_detected:
                emit(
                    EventKind.FETCH_CAPTCHA_DETECTED,
                    task.property_id,
                    provider=captcha_provider,
                    url=task.url,
                    attempt=attempt,
                )

            last_result = result

            # 6. Check if we got a good result
            # EMPTY_BODY is terminal like HARD_FAIL — no retry, the server
            # deliberately returned nothing and a second request won't help.
            if result.outcome in (
                FetchOutcome.OK, FetchOutcome.NOT_MODIFIED,
                FetchOutcome.HARD_FAIL, FetchOutcome.EMPTY_BODY,
                FetchOutcome.DEAD_URL,
            ):
                break

            if result.outcome == FetchOutcome.BOT_BLOCKED:
                emit(EventKind.FETCH_BOT_BLOCKED, task.property_id, url=task.url, attempt=attempt)

            # 7. Retry decision
            retry_after = result.headers.get("retry-after")
            decision = self._retry.decide(result.outcome, attempt, retry_after)

            # RENDER + TRANSIENT split by error class:
            #   • TimeoutError (site not rendering in 35 s) → retry 3 won't
            #     help. Cap at 2 attempts, saving ~35 s on doomed fetches.
            #   • HTTP_5xx (server-side flake) → attempt 3 often recovers
            #     (observed ~50% on embarcatwestjordan.com during validation).
            #     Keep the full 3 attempts.
            #   • Other TRANSIENT (DNS flake, etc.) → keep default policy.
            sig = (result.error_signature or "").upper()
            is_timeout_class = "TIMEOUT" in sig
            if (
                decision.should_retry
                and result.outcome == FetchOutcome.TRANSIENT
                and task.render_mode == RenderMode.RENDER
                and attempt >= 2
                and is_timeout_class
            ):
                emit(
                    EventKind.FETCH_RETRY,
                    task.property_id,
                    wait_ms=0,
                    reason="TRANSIENT_RENDER_TIMEOUT_CAP_2",
                    skipped_further_retries=True,
                    error_signature=result.error_signature,
                )
                break

            if not decision.should_retry:
                break

            # Bug #5 Layer 1 (2026-05-18): on every observed RATE_LIMITED,
            # halve the host's effective rps. Each shard learns
            # independently — no shared state. Compounds across the
            # retry loop: 2 -> 1 -> 0.5 -> 0.25 rps after 3 retries.
            # Floored at 0.1 rps by the rate limiter itself.
            if result.outcome == FetchOutcome.RATE_LIMITED:
                try:
                    self._rate_limiter.on_rate_limited(host)
                except Exception as _rl_exc:
                    log.debug("rate-limiter decay failed for %s: %s", host, _rl_exc)
                # Bug #5 Layer 2 (2026-05-18): increment the consecutive-429
                # counter for this host. At threshold, mark the host as
                # quarantined; subsequent fetches in this shard short-circuit
                # at the top of fetch().
                self._domain_429_consecutive[host] = (
                    self._domain_429_consecutive.get(host, 0) + 1
                )
                if (
                    self._domain_429_consecutive[host] >= _DOMAIN_QUARANTINE_THRESHOLD
                    and host not in self._domain_quarantined
                ):
                    self._domain_quarantined.add(host)
                    log.warning(
                        "Domain %s quarantined for rest of shard run: "
                        "%d consecutive 429s",
                        host, self._domain_429_consecutive[host],
                    )
                    emit(
                        EventKind.FETCH_RETRY,
                        task.property_id,
                        wait_ms=0,
                        reason="DOMAIN_QUARANTINE_TRIGGERED",
                        host=host,
                        consecutive_429s=self._domain_429_consecutive[host],
                    )
            elif result.outcome == FetchOutcome.OK:
                # Reset the consecutive-429 counter on any OK response so
                # transient rate-limits don't accumulate to quarantine
                # over the lifetime of the shard.
                if host in self._domain_429_consecutive:
                    self._domain_429_consecutive[host] = 0

            emit(
                EventKind.FETCH_RETRY, task.property_id, wait_ms=decision.wait_ms, reason=result.outcome.value
            )

            if decision.rotate_identity:
                self._identities.rotate(task.property_id)
                identity = self._identities.pick(sticky_key=task.property_id)
                if proxy:
                    self._proxy_pool.mark_failure(proxy, result.outcome.value)
                proxy = self._proxy_pool.pick(sticky_key=None)  # Fresh proxy
                emit(EventKind.FETCH_ROTATED_IDENTITY, task.property_id)
            elif (
                # Bug 6 (2026-05-09 deep-dive): if we hit RATE_LIMITED with
                # ``proxy is None`` (run started without a sticky proxy and
                # the retry policy didn't request rotation), force-engage a
                # proxy from the pool on the next attempt. Without this,
                # all 3 retries hit the host from the same direct IP and
                # the property dies in FAILED_UNREACHABLE territory.
                result.outcome == FetchOutcome.RATE_LIMITED
                and proxy is None
            ):
                forced = self._proxy_pool.pick(sticky_key=None)
                if forced:
                    proxy = forced
                    emit(
                        EventKind.FETCH_ROTATED_IDENTITY,
                        task.property_id,
                        reason="rate_limited_proxy_escalation",
                    )

            if decision.wait_ms > 0:
                await asyncio.sleep(decision.wait_ms / 1000.0)

        assert last_result is not None

        # 8. Update cond cache on success
        if last_result.ok():
            try:
                if last_result.etag or last_result.last_modified:
                    self._cond_cache.write(task.url, last_result.etag, last_result.last_modified)
            except Exception as exc:
                log.warning("Failed to write cond cache: %s", exc)

            if proxy:
                self._proxy_pool.mark_success(proxy)

            # Persist raw HTML for replay
            if last_result.render_mode == RenderMode.RENDER and last_result.body:
                _persist_raw_html(task.property_id, last_result.body)

            # Phase 2 (stealth plan): opportunistically capture WAF clearance
            # cookies from Set-Cookie headers on successful responses.  Only
            # cookies whose names appear in CLEARANCE_COOKIE_NAMES are stored;
            # general session cookies are ignored.
            if self._clearance_jar is not None:
                try:
                    _sc_header = (last_result.headers or {}).get("set-cookie", "")
                    _clearance_found = _extract_clearance_from_set_cookie(
                        _sc_header, last_result.headers or {}
                    )
                    if _clearance_found:
                        _c_host = urlparse(task.url).netloc
                        _c_proxy_ip = _extract_proxy_ip(proxy)
                        _c_ua_hash = _ua_hash(identity.user_agent, identity.accept_language)
                        # Determine provider from the last captcha_provider we saw.
                        _c_provider = captcha_provider or "unknown"
                        _c_ttl = _PROVIDER_DEFAULT_TTL.get(_c_provider, _FALLBACK_TTL)
                        self._clearance_jar.store(
                            _c_host, _c_proxy_ip, _c_ua_hash,
                            _c_provider, _clearance_found, _c_ttl,
                        )
                except Exception as _cj_exc:
                    log.debug("clearance_jar opportunistic store failed: %s", _cj_exc)

        return last_result

    async def _do_request(
        self,
        task: CrawlTask,
        identity: Identity,
        proxy: str | None,
        etag: str | None,
        last_modified: str | None,
        attempt: int,
        start_ms: int,
    ) -> FetchResult:
        """Execute a single HTTP request or Playwright render.

        Args:
            task: The crawl task.
            identity: Browser identity to use.
            proxy: Proxy URL or None.
            etag: Cached ETag for conditional request.
            last_modified: Cached Last-Modified for conditional request.
            attempt: Current attempt number.
            start_ms: Timestamp when the overall fetch started.

        Returns:
            FetchResult for this attempt.
        """
        if task.render_mode == RenderMode.RENDER:
            return await self._do_render(task, identity, proxy, attempt, start_ms)

        # HEAD or GET via tier-aware HTTP client (S2/S3/S4).
        # This path is only reached when ENABLE_TIER_ESCALATION is False;
        # DC_PROXY+ tiers are handled by providers/ when escalation is on.
        tier = FetchTier.DIRECT

        # S3 — full Chrome-equivalent header set (cold visit = no prior etag/lm).
        cold_visit = etag is None and last_modified is None
        headers = chrome_header_set(identity, cold_visit=cold_visit)
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified

        # S4 — per-property cookie persistence.
        host = urlparse(task.url).netloc
        identity_slot = self._identities.current_slot(task.property_id)
        jar = PropertyCookieJar(task.property_id, identity_slot)
        cookies = jar.cookies_for_host(host)

        # Phase 2 (stealth plan): inject WAF clearance cookies when available.
        # Clearance cookies are keyed by (host, proxy_ip, ua_hash); they win
        # on collision with property-session cookies because the WAF checks the
        # clearance token first.
        if self._clearance_jar is not None:
            try:
                _proxy_ip = _extract_proxy_ip(proxy)
                _ua_hash_val = _ua_hash(identity.user_agent, identity.accept_language)
                _clearance = self._clearance_jar.lookup(host, _proxy_ip, _ua_hash_val)
                if _clearance:
                    cookies = {**cookies, **_clearance}
                    emit(
                        EventKind.CLEARANCE_CACHE_HIT,
                        task.property_id,
                        host=host,
                        cookies=list(_clearance.keys()),
                    )
            except Exception as _cj_exc:
                log.debug("clearance_jar.lookup failed: %s", _cj_exc)

        timeout_sec = min(task.budget_ms / 1000.0, 30.0)
        method = "HEAD" if task.render_mode == RenderMode.HEAD else "GET"

        # S2 — tier-aware client (httpx for DIRECT, curl_cffi for DC_PROXY+).
        client = make_http_client(tier, proxy)
        try:
            resp = await client.request(
                method, task.url, headers=headers, cookies=cookies, timeout=timeout_sec
            )

            resp_headers = resp.headers
            body = resp.content if method == "GET" else None
            body_head = body[:4096] if body else None

            outcome, sig = classify(resp.status_code, resp_headers, body_head)

            # RC5: HTTP GET 200 with an empty or trivially-small body (< 16 bytes).
            # classify() returns OK for 200, but a body this small cannot contain
            # meaningful unit data. Override to EMPTY_BODY so the short-circuit in
            # scraper.py emits a distinct FAILED_FETCH_EMPTY verdict.
            if method == "GET" and outcome == FetchOutcome.OK and len(body or b"") < 16:
                outcome = FetchOutcome.EMPTY_BODY
                sig = "EMPTY_BODY_200"

            # S4 — persist any cookies the server issued (HEAD can also set cookies).
            if resp.cookies:
                jar.update_from_response(host, resp.cookies)

            return FetchResult(
                url=task.url,
                outcome=outcome,
                status=resp.status_code,
                body=body,
                headers=resp_headers,
                render_mode=task.render_mode,
                final_url=resp.final_url,
                attempts=attempt,
                elapsed_ms=_now_ms() - start_ms,
                etag=resp_headers.get("etag"),
                last_modified=resp_headers.get("last-modified"),
                error_signature=sig,
                proxy_used=_redact_proxy(proxy),
            )
        except Exception as exc:
            outcome, sig = classify(None, {}, None, exception=exc)
            return FetchResult(
                url=task.url,
                outcome=outcome,
                status=None,
                body=None,
                headers={},
                render_mode=task.render_mode,
                final_url=task.url,
                attempts=attempt,
                elapsed_ms=_now_ms() - start_ms,
                error_signature=sig,
                proxy_used=_redact_proxy(proxy),
            )
        finally:
            await client.aclose()

    async def _do_render(
        self,
        task: CrawlTask,
        identity: Identity,
        proxy: str | None,
        attempt: int,
        start_ms: int,
    ) -> FetchResult:
        """Render a page with Playwright, capturing network requests.

        Args:
            task: The crawl task.
            identity: Browser identity.
            proxy: Proxy URL or None.
            attempt: Current attempt number.
            start_ms: Overall fetch start timestamp.

        Returns:
            FetchResult with body and network_log populated.
        """
        # Pattern A: if the caller supplied an existing Playwright page, reuse it
        # so that session cookies, referrer chain, and XHR listeners carry over
        # into the hop.  The caller owns the context; we must NOT release it.
        _page_owned = task.reuse_page is None
        if task.reuse_page is not None:
            page = task.reuse_page
        else:
            page = await self._browsers.acquire(identity, proxy)
        network_log: list[dict[str, Any]] = []

        # Phase 2 (stealth plan): inject WAF clearance cookies into the
        # Playwright context before navigation so the WAF skips re-challenging.
        if self._clearance_jar is not None:
            try:
                _render_host = urlparse(task.url).netloc
                _render_proxy_ip = _extract_proxy_ip(proxy)
                _render_ua_hash = _ua_hash(identity.user_agent, identity.accept_language)
                _render_clearance = self._clearance_jar.lookup(
                    _render_host, _render_proxy_ip, _render_ua_hash
                )
                if _render_clearance:
                    await page.context.add_cookies([
                        {"name": name, "value": value, "domain": _render_host, "path": "/"}
                        for name, value in _render_clearance.items()
                    ])
                    emit(
                        EventKind.CLEARANCE_CACHE_HIT,
                        task.property_id,
                        host=_render_host,
                        cookies=list(_render_clearance.keys()),
                        render_mode="RENDER",
                    )
            except Exception as _cj_exc:
                log.debug("clearance_jar inject (render) failed: %s", _cj_exc)

        try:
            # Intercept network requests
            async def _on_response(response: Any) -> None:
                try:
                    url = response.url
                    content_type = response.headers.get("content-type", "")
                    if any(t in content_type for t in ["json", "xml", "html", "text"]):
                        try:
                            body = await response.body()
                        except Exception:
                            body = b""
                        # Body cap — content-type aware:
                        #   JSON/XML: 512 KB. The SightMap REST API
                        #   (/app/api/v1/{key}/sightmaps/{id}) returns ~278 KB
                        #   of gzip-decompressed JSON. The previous 256 KB cap
                        #   truncated it mid-JSON, causing json.loads to fail
                        #   and the unit-signal gate to reject the response
                        #   as a string. JSON APIs are text-only so doubling
                        #   the cap has negligible memory impact.
                        #   HTML/text: 256 KB (unchanged — page captures
                        #   are already capped to prevent huge DOM bodies
                        #   from filling the shard's memory budget).
                        _is_json_xml = "json" in content_type or "xml" in content_type
                        _body_cap = 524_288 if _is_json_xml else 262_144
                        #
                        # F1.2 (2026-05-08 plan): tag each entry with
                        # captcha_detected so the LLM rescue's
                        # _filter_candidates can drop responses captured
                        # behind a CF/recaptcha interstitial. Detection runs
                        # on the first 8KB only (looks_like_captcha is a
                        # cheap signature scan) so we don't double the cost
                        # of every captured response.
                        entry_captcha = False
                        if body:
                            try:
                                detected, _ = looks_like_captcha(body[:8192])
                                entry_captcha = bool(detected)
                            except Exception:
                                entry_captcha = False
                        network_log.append(
                            {
                                "url": url,
                                "status": response.status,
                                "content_type": content_type,
                                "body_size": len(body),
                                "body": body.decode("utf-8", errors="replace")[:_body_cap],
                                "captcha_detected": entry_captcha,
                            }
                        )
                except Exception:
                    pass

            page.on("response", _on_response)

            # C1 — in-flight quiescence tracking.
            # _in_flight counts relevant (non-static, non-analytics) requests
            # that have been dispatched but not yet completed.  When the count
            # reaches 0 AND 500ms of silence have elapsed, all data we can
            # capture has arrived — the asyncio.sleep(2.0) settle is replaced
            # by a deterministic check rather than a fixed guess.
            _in_flight: int = 0
            _last_relevant_ts: list[float] = [time.monotonic()]  # mutable cell

            def _on_request_c1(req: Any) -> None:
                nonlocal _in_flight
                if _c1_is_relevant_request(req.url):
                    _in_flight += 1

            def _on_finish_c1(req: Any) -> None:
                nonlocal _in_flight
                if _c1_is_relevant_request(req.url):
                    _in_flight = max(0, _in_flight - 1)
                    _last_relevant_ts[0] = time.monotonic()

            page.on("request",         _on_request_c1)
            page.on("requestfinished", _on_finish_c1)
            page.on("requestfailed",   _on_finish_c1)

            # Cap per-attempt navigation at 20s. Probe (ma_poc/data/probe_runs/
            # 20260419T175926Z) showed that every timeout property rendered full
            # HTML at domcontentloaded in 4-12s but then blocked waiting on
            # analytics trackers that keep firing, so networkidle never
            # settles. Switching to wait_until="domcontentloaded" + post-load
            # settle sleep captures the same body as networkidle would have,
            # in a fraction of the time, without relying on salvage.
            timeout_ms = min(task.budget_ms, 20000)
            resp: Any = None
            nav_exc: Exception | None = None
            # Shard_84 fix (2026-05-16): wrap page.goto in asyncio.wait_for
            # with a hard 5s overhead beyond Playwright's own timeout. When
            # the Chromium renderer wedges in IPC (observed on shard_84:
            # 26 of 50 PIDs had ``page.goto`` never raise, never return,
            # the call simply parked until the 600s per-property wallclock
            # killed the task), Playwright's internal timeout never fires
            # because the timeout-watcher coroutine waits on the same
            # broken IPC channel. asyncio.wait_for is a host-level guard
            # that interrupts the await regardless of Playwright's state.
            try:
                resp = await asyncio.wait_for(
                    page.goto(
                        task.url,
                        wait_until="domcontentloaded",
                        timeout=timeout_ms,
                    ),
                    timeout=(timeout_ms / 1000.0) + 5.0,
                )
            except asyncio.TimeoutError as exc:
                # IPC wedge — raise the same exception shape page.goto
                # would have used so the salvage path treats both the
                # same.
                nav_exc = exc
                log.warning(
                    "page.goto host-level timeout for %s — Chromium IPC "
                    "likely wedged; release() will force browser restart",
                    task.url,
                )
            except Exception as exc:
                nav_exc = exc

            # C1 — quiescence wait: replace the fixed 2s settle with an
            # event-driven loop.  Exit when no relevant request has been
            # in-flight for _C1_QUIESCE_IDLE_S (500ms), or after
            # _C1_QUIESCE_MAX_S (8s) regardless.  If domcontentloaded raised
            # (nav_exc set), fall through immediately — the salvage logic below
            # will handle any body we did capture.
            if nav_exc is None:
                try:
                    _deadline = time.monotonic() + _C1_QUIESCE_MAX_S
                    while time.monotonic() < _deadline:
                        await asyncio.sleep(0.2)
                        _idle_for = time.monotonic() - _last_relevant_ts[0]
                        if _in_flight == 0 and _idle_for >= _C1_QUIESCE_IDLE_S:
                            break
                except Exception:
                    pass

            # Always-salvage: even when page.goto() timed out, page.content()
            # typically returns a usable DOM (probe: charterclubapts.com timed
            # out on networkidle but had 144KB of rendered HTML with rent data
            # already visible). Only emit TRANSIENT when we couldn't pull any
            # body at all.
            # Salvage: read the page DOM even if goto() timed out.
            # Use a tight 8-second timeout — any DOM that loaded before
            # the navigation timeout is immediately available; we only
            # need time for the IPC round-trip, not for new network
            # activity. Without this cap, page.content() inherits
            # DEFAULT_PAGE_TIMEOUT_MS (60s) and hangs for a full minute
            # on CF "Under Attack Mode" pages that are mid-JS-challenge.
            body_text: str | None = None
            try:
                body_text = await asyncio.wait_for(page.content(), timeout=8.0)
            except Exception as exc:
                if nav_exc is None:
                    nav_exc = exc

            # IntersectionObserver scroll-trigger (2026-05-12):
            # Some sites (Jonah Digital, some Entrata / RealPage custom-domain
            # properties) use IntersectionObserver to gate unit-data loading.
            # The floor-plan section only loads when it enters the browser
            # viewport — without a scroll event that never happens in headless
            # mode, so the _fp-renderable XHR never fires and both the DOM and
            # the network_log stay empty of unit data.
            #
            # Trigger conditions (all must hold):
            #   • RENDER mode (has a live Playwright page)
            #   • Initial body ≥ 50 KB (has real content, not an error page)
            #   • Zero rent signals ($NNN) in the initial body (units not
            #     pre-rendered — deferred loading is likely)
            #
            # On trigger: scroll to the bottom of the page (fires all pending
            # IntersectionObservers), wait 1.5 s for XHR round-trips, then
            # re-read page.content(). The network_log handler was registered
            # before page.goto() and stays active, so any XHRs that fire
            # during the scroll window are automatically captured.
            #
            # Always log the outcome so we can analyse effectiveness and tune
            # the condition over time.
            from ma_poc.pms.signal_engine.floor_plan_signals import (
                has_floor_plan_signals as _has_fp_signals_fetch,
                SIGNAL_THRESHOLD_ANY as _FP_THRESHOLD_ANY,
            )

            _scroll_triggered = False
            if (
                task.render_mode == RenderMode.RENDER
                and body_text is not None
                and len(body_text) >= 50_000
                and not _has_fp_signals_fetch(body_text, _FP_THRESHOLD_ANY)
            ):
                # Slow mouse-wheel scroll: 2 seconds of continuous scrolling via
                # page.mouse.wheel() instead of window.scrollTo() jumps.
                #
                # WHY mouse.wheel NOT window.scrollTo:
                #   window.scrollTo() sets an absolute position instantly — the
                #   element was never "in view" during the jump, so
                #   IntersectionObserver callbacks never fire.  page.mouse.wheel()
                #   sends real WheelEvents that the browser propagates through the
                #   scroll pipeline, correctly triggering IntersectionObserver on
                #   every element that passes through the viewport.
                #
                # Strategy: 20 incremental steps over 2 s (100 ms / step),
                # distributing the scroll distance evenly across the page height.
                # Mouse is centred in the viewport so wheel events hit the page
                # rather than an overflow-hidden child.
                _body_before_scroll = len(body_text)
                try:
                    # Get page dimensions.
                    _dims = await page.evaluate("""() => ({
                        scrollHeight: document.body.scrollHeight,
                        viewportHeight: window.innerHeight,
                        viewportWidth: window.innerWidth
                    })""")
                    _scroll_h = int(_dims.get("scrollHeight") or 0)
                    _vp_h = int(_dims.get("viewportHeight") or 1080)
                    _vp_w = int(_dims.get("viewportWidth") or 1920)
                    _total_scroll = max(0, _scroll_h - _vp_h)

                    # Centre the mouse in the viewport so wheel events hit the
                    # main document scroll area (not a fixed sidebar or overlay).
                    await page.mouse.move(_vp_w // 2, _vp_h // 2)

                    # 20 wheel steps over 2 seconds = 100 ms / step.
                    _SCROLL_STEPS = 20
                    _STEP_DELAY_S = 0.10        # 100 ms between each step
                    _step_delta = max(120, _total_scroll // _SCROLL_STEPS)

                    for _ in range(_SCROLL_STEPS):
                        await page.mouse.wheel(0, _step_delta)
                        await asyncio.sleep(_STEP_DELAY_S)

                    # Allow 1.5 s for IntersectionObserver callbacks to fire and
                    # any newly triggered XHRs to complete.
                    await asyncio.sleep(1.5)

                    _body_after_scroll_text = await page.content()
                    _body_after_size = len(_body_after_scroll_text or "")
                    _body_grew = _body_after_size > _body_before_scroll
                    _fp_appeared = _has_fp_signals_fetch(
                        _body_after_scroll_text or "", _FP_THRESHOLD_ANY
                    )
                    if _body_after_scroll_text:
                        body_text = _body_after_scroll_text
                    _scroll_triggered = True
                    log.info(
                        "fetch.scroll_trigger url=%s body_before=%d body_after=%d"
                        " grew=%s fp_appeared=%s",
                        task.url,
                        _body_before_scroll,
                        _body_after_size,
                        _body_grew,
                        _fp_appeared,
                    )
                except Exception as _scroll_exc:
                    log.debug("fetch.scroll_trigger failed for %s: %s", task.url, _scroll_exc)

            # CF JS challenge auto-solve (2026-05-12):
            # Cloudflare's Tier-1 "Just a moment..." protection is a JS
            # challenge that runs for ~5-10 seconds then redirects to the real
            # page. patchright handles navigator.webdriver, so the challenge
            # CAN solve itself if we wait long enough.
            #
            # Detection: body contains "Just a moment" OR "challenge-platform"
            # AND current body < 20KB (CF challenge pages are ~10KB).
            # Action: wait 20 more seconds, re-read — if body grew and CF
            # patterns are gone, challenge solved. If still blocked, return
            # BOT_BLOCKED so the tier escalator can escalate.
            #
            # Only fires on JS_CHALLENGE pages, NOT WAF "Attention Required"
            # (which shows "Sorry, you have been blocked" and can't auto-solve).
            if body_text and 512 <= len(body_text) <= 20_000:
                _CF_JS_PATTERNS = (
                    b"Just a moment", b"challenge-platform", b"__cf_chl_",
                    b"Checking your browser",
                )
                _body_bytes_check = body_text.encode("utf-8", errors="replace")[:1024]
                _is_cf_js_challenge = any(p in _body_bytes_check for p in _CF_JS_PATTERNS)
                # Don't trigger on WAF blocks — they show "Sorry, you have been blocked"
                _is_cf_waf = b"Attention Required" in _body_bytes_check or b"Sorry, you have been blocked" in _body_bytes_check
                if _is_cf_js_challenge and not _is_cf_waf:
                    log.info(
                        "fetch.cf_js_challenge_detected url=%s body=%d -- waiting 20s for auto-solve",
                        task.url, len(body_text),
                    )
                    try:
                        await asyncio.sleep(20.0)
                        _body_after_challenge = await asyncio.wait_for(page.content(), timeout=8.0)
                        if _body_after_challenge and len(_body_after_challenge) > len(body_text):
                            _still_cf = any(
                                p in _body_after_challenge.encode("utf-8", errors="replace")[:1024]
                                for p in _CF_JS_PATTERNS
                            )
                            if not _still_cf:
                                body_text = _body_after_challenge
                                log.info(
                                    "fetch.cf_js_challenge_solved url=%s body_after=%d",
                                    task.url, len(body_text),
                                )
                            else:
                                log.info(
                                    "fetch.cf_js_challenge_unsolved url=%s still_blocked=True",
                                    task.url,
                                )
                    except Exception as _cf_exc:
                        log.debug("fetch.cf_challenge_wait failed for %s: %s", task.url, _cf_exc)

            # 2026-05 Fix B + Fix 5 + Fix 7 — portal-aware late-render wait
            # with extended portal list and in-memory per-host learning.
            #
            # Trigger: landed URL is on a known portal host (XHR loads units
            # ~5-12 sec after page-load fires) OR host has been observed
            # before in this run as needing late-render. Extend the wait
            # to 12 sec on portal hosts; keep 8 sec for learned-hosts.
            #
            # NOTE: a broader "any small JS-heavy body" trigger (Fix A in
            # batch-6) was tested and reverted — it fired on too many pages,
            # cumulative 5-sec waits pushed shards past the per-task timeout,
            # and recovery DROPPED -18 vs v5. Keep the trigger narrow.
            if body_text is not None and len(body_text) >= 512:
                portal_match = False
                landed_host = ""
                try:
                    landed = page.url or task.url
                    landed_lower = landed.lower() if landed else ""
                    # Extract host for per-host tracking
                    try:
                        from urllib.parse import urlparse as _urlparse_q
                        landed_host = _urlparse_q(landed_lower).netloc
                    except Exception:
                        landed_host = ""
                    portal_match = any(
                        m in landed_lower
                        for m in (
                            "sightmap.com",
                            ".onlineleasing.realpage.com",
                            ".appfolio.com",
                            ".rentcafe.com",  # Fix 5 — added per cluster analysis
                            "rlets.com",  # Fix 5 — Hyly's portal CDN
                            "my.hy.ly",  # Fix 5 — Hyly portal
                            # Entrata ProspectPortal: the SightMap widget initialises
                            # asynchronously (~3-4s after page load). The standard 2s
                            # settle is not enough for the SightMap REST API call to fire.
                            "prospectportal.com",
                            # Funnel / FortressTech: React SPA shells that fetch
                            # inventory via XHR ~8-12 sec after mount. PID 1713
                            # brooklaneapts.com 2026-05-15: portal.fortresstech.io
                            # returned 476K body / 1.4K visible text → LLM rejected.
                            ".fortresstech.io",
                            "funnelleasing.com",
                            # Wix Visual Data widget: AngularJS SPA rendering Wix
                            # Collection rows as a <table>. Initial GET returns the
                            # AngularJS shell; the table populates after the SDK
                            # parses the page query params (pageId/compId/siteRevision)
                            # and fetches the collection. PIDs 46179 + 118965.
                            "wix-visual-data.appspot.com",
                            # Cross Street leasing widget — React SPA. PID 292955.
                            "yourcrossstreet.com",
                        )
                    )
                except Exception:
                    portal_match = False
                    landed_host = ""

                # Fix 7 — per-host learned-wait (in-memory, per-shard)
                # Hosts marked as "needed late-render" on a previous fetch
                # this run get a shorter 5-sec preemptive wait next time.
                # Module-level set; survives across fetches in the same
                # shard process but not across shards. That's fine — each
                # shard learns its own slice.
                learned_wait = landed_host in _LATE_RENDER_HOSTS if landed_host else False

                # Generic SPA-shell detector — extends the late-render path
                # beyond the host whitelist. Catches React/Vue/Angular SPAs
                # we haven't explicitly listed by looking at the body itself:
                # (a) high body/text ratio (>25), (b) at least one SPA-framework
                # marker, (c) low anchor count, (d) no structural fp signals.
                # When all four hold, the body is a JS-heavy shell that hasn't
                # rendered its inventory yet — wait 6s (between learned_wait
                # 5s and portal_match 12s) so the SPA can hydrate.
                # See data/canary/local_runs/fix-validation-2026-05-15/
                # UNCHANGED_FAIL_ANALYSIS.md PID 1713 — FortressTech is a
                # React shell; whitelist now covers it but the heuristic
                # catches other vendors we haven't catalogued yet.
                spa_shell = False
                if body_text is not None and len(body_text) >= 50_000 and not portal_match and not learned_wait:
                    try:
                        import re as _re_spa
                        # Cheap visible-text length estimate (strip tags only).
                        _visible = _re_spa.sub(r"<[^>]+>", " ", body_text)
                        _visible = _re_spa.sub(r"\s+", " ", _visible).strip()
                        _ratio = len(body_text) / max(len(_visible), 1)
                        _anchor_count = len(_re_spa.findall(r"<a\s+[^>]*href=", body_text, _re_spa.IGNORECASE))
                        # SPA framework markers — broad on purpose, false positives
                        # only cost an extra 6s wait, false negatives lose data.
                        _spa_markers = (
                            'id="root"',
                            "id='root'",
                            'id="__next"',
                            'data-reactroot',
                            'ng-app',
                            'ng-version',
                            '__NEXT_DATA__',
                            '__NUXT__',
                            'data-v-app',  # Vue 3
                            'data-server-rendered',  # Nuxt
                            'window.__INITIAL_STATE__',
                            'wix-warmup-data',  # Wix
                            '/static/js/main.',  # CRA bundle
                            'webpack-runtime',
                        )
                        _has_marker = any(m in body_text for m in _spa_markers)
                        # Trigger when: ratio is high AND framework marker present
                        # AND nav links not yet rendered AND no fp signals.
                        if _ratio > 25 and _has_marker and _anchor_count < 20:
                            from ma_poc.pms.signal_engine.floor_plan_signals import (
                                has_floor_plan_signals as _has_fp_spa,
                                SIGNAL_THRESHOLD_STRUCTURAL as _FP_STRUCTURAL_SPA,
                            )
                            if not _has_fp_spa(body_text, _FP_STRUCTURAL_SPA):
                                spa_shell = True
                    except Exception:
                        spa_shell = False

                if portal_match or learned_wait or spa_shell:
                    # B1: wait only when the page lacks structural floor-plan data.
                    # SIGNAL_THRESHOLD_STRUCTURAL (≥2 types) means genuinely rich.
                    from ma_poc.pms.signal_engine.floor_plan_signals import (
                        has_floor_plan_signals as _has_fp_portal,
                        SIGNAL_THRESHOLD_STRUCTURAL as _FP_STRUCTURAL,
                    )
                    if not _has_fp_portal(body_text, _FP_STRUCTURAL):
                        try:
                            wait_sec = 12.0 if portal_match else (6.0 if spa_shell else 5.0)
                            await asyncio.sleep(wait_sec)
                            body_text_2 = await page.content()
                            if body_text_2 and len(body_text_2) > len(body_text):
                                body_text = body_text_2
                                # Fix 7 — record this host for future fetches
                                if landed_host:
                                    _LATE_RENDER_HOSTS.add(landed_host)
                        except Exception:
                            pass

            # Bug 8 (2026-05-09 deep-dive): SPA-shell salvage retry. When the
            # raw body is non-trivial but the visible text (HTML tags stripped)
            # is absurdly small, the SPA hadn't finished hydrating before we
            # called page.content() — adapters and the LLM gate will both
            # reject this near-empty shell as LLM_GATE_NO_BODY. Sleep 6s and
            # re-read once. Gated tightly on visible-text < 500 bytes so it
            # doesn't fire on the broad late-render path that batch-6 reverted.
            if body_text is not None and len(body_text) >= 512:
                import re as _re_b8

                visible = _re_b8.sub(r"<[^>]+>", " ", body_text)
                visible = _re_b8.sub(r"\s+", " ", visible).strip()
                if len(visible) < 500:
                    try:
                        await asyncio.sleep(6.0)
                        body_text_b8 = await page.content()
                        if body_text_b8 and len(body_text_b8) > len(body_text):
                            body_text = body_text_b8
                    except Exception:
                        pass

            # Anchor-link DOM stability gate (2026-05-12):
            # React SPAs with code-split routes lazy-load navigation components
            # AFTER networkidle / domcontentloaded fires.  React Router detects
            # the current route and requests a separate JS chunk; that chunk
            # renders the navigation links (including leasing-portal hrefs like
            # securecafe.com / onlineleasing paths).  The prior heuristics
            # (scroll-trigger, portal wait, Bug-8 salvage) all check body SIZE
            # or rent-signal text — they can't tell whether the navigation DOM
            # has finished rendering.
            #
            # The right signal: count <a href> elements.  If the count grows
            # between two samples, React is still hydrating route components;
            # wait and recheck.  Once stable (or budget exhausted), re-read.
            #
            # Trigger: page body < 40 KB AND anchor count < 20 (the SPA shell
            # typically ships 3-10 skeleton links; a fully rendered site has 20+
            # navigation links).  This targets exactly the tiny-shell case
            # without firing on already-rendered pages.
            #
            # Max budget: 4 × 1.5 s = 6 s additional wait.  Safe because it
            # only fires when the DOM is actively changing — if the first two
            # samples are equal the loop exits immediately (0 extra wait).
            if (
                task.render_mode == RenderMode.RENDER
                and body_text is not None
                and len(body_text) < 40_000
            ):
                try:
                    _link_count_prev = await page.evaluate(
                        "document.querySelectorAll('a[href]').length"
                    )
                    if _link_count_prev < 20:
                        _link_stable = False
                        _max_stability_rounds = 4
                        for _round in range(_max_stability_rounds):
                            await asyncio.sleep(1.5)
                            _link_count_now = await page.evaluate(
                                "document.querySelectorAll('a[href]').length"
                            )
                            if _link_count_now == _link_count_prev:
                                _link_stable = True
                                break
                            _link_count_prev = _link_count_now
                        body_text_nav = await page.content()
                        if body_text_nav and len(body_text_nav) > len(body_text):
                            body_text = body_text_nav
                            log.info(
                                "fetch.anchor_stable url=%s links=%d stable=%s body=%d",
                                task.url,
                                _link_count_prev,
                                _link_stable,
                                len(body_text),
                            )
                except Exception:
                    pass

            if body_text is None or len(body_text) < 512:
                _body_len = len(body_text) if body_text else 0
                # RC5: HTTP 200 with a trivially-small body (< 16 bytes) is
                # classified as EMPTY_BODY, not TRANSIENT. The server returned
                # a 200 but no content — retrying is unlikely to help, and
                # TRANSIENT would silently carry forward stale data on future
                # runs. EMPTY_BODY is terminal so dashboards can track it
                # separately from real connectivity errors.
                _resp_status = None
                try:
                    _resp_status = resp.status if resp is not None else None
                except Exception:
                    pass
                if (
                    nav_exc is None
                    and _resp_status == 200
                    and _body_len < 16
                ):
                    return FetchResult(
                        url=task.url,
                        outcome=FetchOutcome.EMPTY_BODY,
                        status=200,
                        body=None,
                        headers={},
                        render_mode=RenderMode.RENDER,
                        final_url=page.url,
                        attempts=attempt,
                        elapsed_ms=_now_ms() - start_ms,
                        network_log=network_log,
                        error_signature="EMPTY_BODY_200",
                        proxy_used=_redact_proxy(proxy),
                    )
                outcome, sig = classify(
                    resp.status if resp else None,
                    {},
                    None,
                    exception=nav_exc,
                )
                return FetchResult(
                    url=task.url,
                    outcome=outcome,
                    status=resp.status if resp else None,
                    body=None,
                    headers={},
                    render_mode=RenderMode.RENDER,
                    final_url=page.url if nav_exc is None else task.url,
                    attempts=attempt,
                    elapsed_ms=_now_ms() - start_ms,
                    network_log=network_log,
                    error_signature=sig,
                    proxy_used=_redact_proxy(proxy),
                )

            body = body_text.encode("utf-8", errors="replace")
            final_url = page.url
            resp_headers = {k.lower(): v for k, v in (resp.headers if resp else {}).items()}
            body_head = body[:4096]

            # SGCaptcha early exit: HTTP 202 + redirect to /.well-known/sgcaptcha/
            # The entire interstitial page (11–12 KB) is a bot-wall with zero unit
            # data. Skip all tier cascade — returning BOT_BLOCKED here saves ~25 s
            # of wasted extraction per property.
            if "/.well-known/sgcaptcha/" in (final_url or ""):
                log.debug(
                    "_do_render %s: sgcaptcha wall detected via final_url — marking BOT_BLOCKED",
                    task.url,
                )
                return FetchResult(
                    url=task.url,
                    outcome=FetchOutcome.BOT_BLOCKED,
                    status=resp.status if resp else 202,
                    body=body,
                    headers=resp_headers,
                    render_mode=RenderMode.RENDER,
                    final_url=final_url,
                    attempts=attempt,
                    elapsed_ms=_now_ms() - start_ms,
                    network_log=network_log,
                    error_signature="SGCAPTCHA_WALL",
                    proxy_used=_redact_proxy(proxy),
                )

            if nav_exc is not None:
                # Timeout/abort but body salvaged. If the salvaged page looks
                # like a Cloudflare/reCAPTCHA interstitial, mark BOT_BLOCKED so
                # the outer retry rotates identity+proxy. Otherwise treat as OK
                # so adapters can run against the rendered DOM + captured
                # network_log; tag the signature so reports can distinguish
                # salvage from clean.
                try:
                    is_captcha, provider = looks_like_captcha(body_head)
                except Exception:
                    is_captcha, provider = False, None
                status = resp.status if resp else 200
                if is_captcha:
                    outcome = FetchOutcome.BOT_BLOCKED
                    sig = (
                        "CF_CHALLENGE"
                        if provider == "cloudflare"
                        else f"CAPTCHA_{(provider or 'unknown').upper()}"
                    )
                else:
                    outcome = FetchOutcome.OK
                    sig = "TIMEOUT_SALVAGED"
            else:
                status = resp.status if resp else 200
                outcome, sig = classify(status, resp_headers, body_head)

            return FetchResult(
                url=task.url,
                outcome=outcome,
                status=status,
                body=body,
                headers=resp_headers,
                render_mode=RenderMode.RENDER,
                final_url=final_url,
                attempts=attempt,
                elapsed_ms=_now_ms() - start_ms,
                network_log=network_log,
                etag=resp_headers.get("etag"),
                last_modified=resp_headers.get("last-modified"),
                error_signature=sig,
                proxy_used=_redact_proxy(proxy),
            )
        except Exception as exc:
            outcome, sig = classify(None, {}, None, exception=exc)
            return FetchResult(
                url=task.url,
                outcome=outcome,
                status=None,
                body=None,
                headers={},
                render_mode=RenderMode.RENDER,
                final_url=task.url,
                attempts=attempt,
                elapsed_ms=_now_ms() - start_ms,
                network_log=network_log,
                error_signature=sig,
                proxy_used=_redact_proxy(proxy),
            )
        finally:
            if _page_owned:
                await self._browsers.release(page)


def _now_ms() -> int:
    """Current time in milliseconds since epoch."""
    return int(time.time() * 1000)


def _redact_proxy(proxy: str | None) -> str | None:
    """Redact credentials from proxy URL."""
    if proxy is None:
        return None
    import re

    return re.sub(r"://[^@]+@", "://***@", proxy)


def _short_hash(value: str | None) -> str | None:
    """Short stable hash for correlating identity usage without leaking UA strings."""
    if not value:
        return None
    import hashlib

    return hashlib.sha256(value.encode("utf-8", errors="ignore")).hexdigest()[:10]


def _extract_proxy_ip(proxy: str | None) -> str:
    """Extract the host/IP portion of a proxy URL for use as a jar key.

    Returns an empty string for direct (non-proxy) connections so the jar
    key remains stable and distinct from any real IP.
    """
    if not proxy:
        return ""
    try:
        parsed = urlparse(proxy)
        return parsed.hostname or ""
    except Exception:
        return ""


def _persist_raw_html(property_id: str, body: bytes) -> None:
    """Write raw HTML to disk for replay. Fails silently.

    Args:
        property_id: The property's canonical ID.
        body: Raw HTML bytes.
    """
    try:
        date_str = datetime.now(UTC).strftime("%Y-%m-%d")
        data_dir = Path(os.getenv("DATA_DIR", _DEFAULT_DATA_DIR))
        out_dir = data_dir / "raw_html" / date_str
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{property_id}.html.gz"
        out_path.write_bytes(gzip.compress(body))
    except Exception as exc:
        log.debug("Failed to persist raw HTML for %s: %s", property_id, exc)


# Module-level singleton factory
_default: Fetcher | None = None


def get_default_fetcher() -> Fetcher:
    """Get or create the default Fetcher singleton.

    Returns:
        A configured Fetcher instance.
    """
    global _default
    if _default is None:
        proxy_urls = os.getenv("PROXY_POOL_URLS", "").split(",")
        proxy_urls = [u.strip() for u in proxy_urls if u.strip()]
        cache_dir = Path(os.getenv("DATA_DIR", _DEFAULT_DATA_DIR)) / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)

        state_dir = Path(os.getenv("DATA_DIR", _DEFAULT_DATA_DIR)) / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        _default = Fetcher(
            proxy_pool=ProxyPool(proxy_urls),
            rate_limiter=HostRateLimiter(),
            robots=RobotsConsumer(),
            cond_cache=ConditionalCache(cache_dir / "conditional.sqlite"),
            identities=IdentityPool(),
            browsers=BrowserContextPool(max_contexts=int(os.getenv("MAX_CONCURRENT_BROWSERS", "5"))),
            retry=RetryPolicy(),
            clearance_jar=ClearanceJar(state_dir / "clearance_jar.sqlite"),
        )
    return _default
