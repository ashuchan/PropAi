"""Fetcher orchestrator — the top-level fetch() function.

Assembles all fetch-layer components: retry, proxy, stealth, rate limiting,
conditional GET, robots.txt, CAPTCHA detection, and Playwright rendering.

Never raises on transient errors. Always returns a FetchResult.
"""
from __future__ import annotations

import asyncio
import gzip
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from ..discovery.contracts import CrawlTask
from ..observability.events import EventKind, emit
from .browser_pool import BrowserContextPool

_MA_POC_ROOT = Path(__file__).resolve().parent.parent  # ma_poc/
_DEFAULT_DATA_DIR = str(_MA_POC_ROOT / "data")
from .captcha_detect import looks_like_captcha
from .conditional import ConditionalCache
from .contracts import FetchOutcome, FetchResult, RenderMode
from .proxy_pool import ProxyPool
from .rate_limiter import HostRateLimiter
from .response_classifier import classify
from .retry_policy import RetryDecision, RetryPolicy
from .robots import RobotsConsumer
from .stealth import Identity, IdentityPool

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
    ) -> None:
        self._proxy_pool = proxy_pool
        self._rate_limiter = rate_limiter
        self._robots = robots
        self._cond_cache = cond_cache
        self._identities = identities
        self._browsers = browsers
        self._retry = retry

    async def fetch(self, task: CrawlTask) -> FetchResult:
        """Top-level entry. Never raises on transient errors.

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

        Returns:
            A FetchResult. Never raises.
        """
        start_ms = _now_ms()
        host = urlparse(task.url).netloc
        identity = self._identities.pick(sticky_key=task.property_id)
        proxy = self._proxy_pool.pick(sticky_key=task.property_id)

        emit(EventKind.FETCH_STARTED, task.property_id,
             url=task.url, render_mode=task.render_mode.value, attempt=1)

        # 1. robots check
        try:
            allowed = await self._robots.is_allowed(task.url, identity.user_agent)
            if not allowed:
                return FetchResult(
                    url=task.url, outcome=FetchOutcome.HARD_FAIL,
                    status=None, body=None, headers={},
                    render_mode=task.render_mode, final_url=task.url,
                    attempts=0, elapsed_ms=_now_ms() - start_ms,
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
                await asyncio.wait_for(
                    self._rate_limiter.acquire(host), timeout=30.0
                )
            except asyncio.TimeoutError:
                pass

            # 4-5. Issue request
            try:
                result = await self._do_request(
                    task, identity, proxy, cached_etag, cached_lm, attempt, start_ms,
                )
            except Exception as exc:
                outcome, sig = classify(None, {}, None, exception=exc)
                result = FetchResult(
                    url=task.url, outcome=outcome, status=None,
                    body=None, headers={}, render_mode=task.render_mode,
                    final_url=task.url, attempts=attempt,
                    elapsed_ms=_now_ms() - start_ms,
                    error_signature=sig, proxy_used=_redact_proxy(proxy),
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

            emit(EventKind.FETCH_COMPLETED, task.property_id,
                 outcome=result.outcome.value, status=result.status,
                 elapsed_ms=result.elapsed_ms, attempt=attempt,
                 error_signature=result.error_signature,
                 final_url=result.final_url,
                 body_bytes=body_bytes_len,
                 content_type=content_type,
                 captcha_detected=captcha_detected,
                 captcha_provider=captcha_provider,
                 proxy_used=result.proxy_used,
                 identity_ua_hash=_short_hash(identity.user_agent),
                 render_mode=result.render_mode.value)

            if captcha_detected:
                emit(EventKind.FETCH_CAPTCHA_DETECTED, task.property_id,
                     provider=captcha_provider, url=task.url, attempt=attempt)

            last_result = result

            # 6. Check if we got a good result
            if result.outcome in (FetchOutcome.OK, FetchOutcome.NOT_MODIFIED,
                                  FetchOutcome.HARD_FAIL):
                break

            if result.outcome == FetchOutcome.BOT_BLOCKED:
                emit(EventKind.FETCH_BOT_BLOCKED, task.property_id,
                     url=task.url, attempt=attempt)

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
                emit(EventKind.FETCH_RETRY, task.property_id,
                     wait_ms=0, reason="TRANSIENT_RENDER_TIMEOUT_CAP_2",
                     skipped_further_retries=True,
                     error_signature=result.error_signature)
                break

            if not decision.should_retry:
                break

            emit(EventKind.FETCH_RETRY, task.property_id,
                 wait_ms=decision.wait_ms, reason=result.outcome.value)

            if decision.rotate_identity:
                self._identities.rotate(task.property_id)
                identity = self._identities.pick(sticky_key=task.property_id)
                if proxy:
                    self._proxy_pool.mark_failure(proxy, result.outcome.value)
                proxy = self._proxy_pool.pick(sticky_key=None)  # Fresh proxy
                emit(EventKind.FETCH_ROTATED_IDENTITY, task.property_id)

            if decision.wait_ms > 0:
                await asyncio.sleep(decision.wait_ms / 1000.0)

        assert last_result is not None

        # 8. Update cond cache on success
        if last_result.ok():
            try:
                if last_result.etag or last_result.last_modified:
                    self._cond_cache.write(
                        task.url, last_result.etag, last_result.last_modified
                    )
            except Exception as exc:
                log.warning("Failed to write cond cache: %s", exc)

            if proxy:
                self._proxy_pool.mark_success(proxy)

            # Persist raw HTML for replay
            if last_result.render_mode == RenderMode.RENDER and last_result.body:
                _persist_raw_html(task.property_id, last_result.body)

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

        # HEAD or GET via httpx
        headers: dict[str, str] = {
            "User-Agent": identity.user_agent,
            "Accept-Language": identity.accept_language,
        }
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified

        timeout_sec = min(task.budget_ms / 1000.0, 30.0)
        method = "HEAD" if task.render_mode == RenderMode.HEAD else "GET"

        try:
            async with httpx.AsyncClient(
                proxy=proxy,
                timeout=timeout_sec,
                follow_redirects=True,
                verify=True,
            ) as client:
                resp = await client.request(method, task.url, headers=headers)

                resp_headers = {k.lower(): v for k, v in resp.headers.items()}
                body = resp.content if method == "GET" else None
                body_head = body[:4096] if body else None

                outcome, sig = classify(resp.status_code, resp_headers, body_head)

                return FetchResult(
                    url=task.url, outcome=outcome, status=resp.status_code,
                    body=body, headers=resp_headers,
                    render_mode=task.render_mode,
                    final_url=str(resp.url),
                    attempts=attempt, elapsed_ms=_now_ms() - start_ms,
                    etag=resp_headers.get("etag"),
                    last_modified=resp_headers.get("last-modified"),
                    error_signature=sig,
                    proxy_used=_redact_proxy(proxy),
                )
        except Exception as exc:
            outcome, sig = classify(None, {}, None, exception=exc)
            return FetchResult(
                url=task.url, outcome=outcome, status=None,
                body=None, headers={}, render_mode=task.render_mode,
                final_url=task.url, attempts=attempt,
                elapsed_ms=_now_ms() - start_ms,
                error_signature=sig, proxy_used=_redact_proxy(proxy),
            )

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
        page = await self._browsers.acquire(identity, proxy)
        network_log: list[dict[str, Any]] = []

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
                        network_log.append({
                            "url": url,
                            "status": response.status,
                            "content_type": content_type,
                            "body_size": len(body),
                            "body": body.decode("utf-8", errors="replace")[:10000],
                        })
                except Exception:
                    pass

            page.on("response", _on_response)

            # Cap per-attempt navigation at 35s (down from 60s). Today's
            # TRANSIENT bucket showed attempts running 32-78s and still
            # failing — longer waits almost never flipped the outcome and
            # inflated the total per-property budget by 3x.
            timeout_ms = min(task.budget_ms, 35000)
            resp = await page.goto(task.url, wait_until="networkidle", timeout=timeout_ms)
            await asyncio.sleep(2.0)

            body = (await page.content()).encode("utf-8")
            status = resp.status if resp else 200
            final_url = page.url
            resp_headers = {k.lower(): v for k, v in (resp.headers if resp else {}).items()}
            body_head = body[:4096]

            outcome, sig = classify(status, resp_headers, body_head)

            return FetchResult(
                url=task.url, outcome=outcome, status=status,
                body=body, headers=resp_headers,
                render_mode=RenderMode.RENDER,
                final_url=final_url, attempts=attempt,
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
                url=task.url, outcome=outcome, status=None,
                body=None, headers={}, render_mode=RenderMode.RENDER,
                final_url=task.url, attempts=attempt,
                elapsed_ms=_now_ms() - start_ms,
                network_log=network_log,
                error_signature=sig,
                proxy_used=_redact_proxy(proxy),
            )
        finally:
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


def _persist_raw_html(property_id: str, body: bytes) -> None:
    """Write raw HTML to disk for replay. Fails silently.

    Args:
        property_id: The property's canonical ID.
        body: Raw HTML bytes.
    """
    try:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
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

        _default = Fetcher(
            proxy_pool=ProxyPool(proxy_urls),
            rate_limiter=HostRateLimiter(),
            robots=RobotsConsumer(),
            cond_cache=ConditionalCache(cache_dir / "conditional.sqlite"),
            identities=IdentityPool(),
            browsers=BrowserContextPool(
                max_contexts=int(os.getenv("MAX_CONCURRENT_BROWSERS", "5"))
            ),
            retry=RetryPolicy(),
        )
    return _default
