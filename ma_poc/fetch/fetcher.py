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

from ..config.feature_flags import ENABLE_TIER_ESCALATION, ENABLE_UNLOCKER_TIER
from ..discovery.contracts import CrawlTask
from ..observability.events import EventKind, emit
from .browser_pool import BrowserContextPool
from .camoufox_pool import get_browser_pool
from .captcha_detect import looks_like_captcha
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
    body_size: int | None
    if isinstance(body, bytes):
        head = body[:4096]
        body_size = len(body)
    elif isinstance(body, str):
        head = body[:4096].encode("utf-8", errors="replace")
        body_size = len(body)
    else:
        head = None
        body_size = None
    return classify(
        status_code, headers or {}, head, exception=error, body_size=body_size
    )

_MA_POC_ROOT = Path(__file__).resolve().parent.parent  # ma_poc/
_DEFAULT_DATA_DIR = str(_MA_POC_ROOT / "data")

# Safety: hard per-fetch transfer cap. Independent of the (default)
# image/font/media resource-blocking — this is a runaway-bandwidth
# circuit-breaker that matters most on a proxied fleet run ($/GB
# residential egress). Once cumulative response bytes for a single
# render exceed the cap, all further requests are aborted (the page
# keeps whatever it already loaded, so extraction still proceeds on the
# captured DOM/network_log). Generous default (16 MB) so it only trips
# on pathological sites, never normal ones. ``MAX_FETCH_BYTES=0``
# disables the cap.
_MAX_FETCH_BYTES = int(os.getenv("MAX_FETCH_BYTES", str(16_000_000)))

log = logging.getLogger(__name__)

# Cookie-mint reuse (option b): exact names + prefixes of the bot-wall
# clearance cookies worth reusing. cf_clearance/__cf_bm = Cloudflare,
# datadome/__ddg* = DataDome, incap_ses*/visid_incap* = Imperva/Incapsula.
# Anything else from the context is session noise we deliberately drop
# (smaller jar, no cross-property identity bleed).
_CLEARANCE_EXACT = {"cf_clearance", "__cf_bm", "datadome"}
_CLEARANCE_PREFIX = ("__ddg", "incap_ses", "visid_incap", "nlbi_")


async def _harvest_clearance_cookies(page: Any) -> dict[str, str]:
    """Return ``{name: value}`` of bot-wall clearance cookies from *page*.

    Reads the live Playwright browser context (post-challenge). Returns
    only the CF/DataDome/Incapsula clearance cookies — never the full
    cookie set — so reuse can't leak a session/identity cookie into the
    cheap curl_cffi probe. Best-effort: any error yields ``{}`` (callers
    then behave exactly as before option b).
    """
    try:
        cookies = await page.context.cookies()
    except Exception:
        return {}
    out: dict[str, str] = {}
    for c in cookies or []:
        name = c.get("name") or ""
        if name in _CLEARANCE_EXACT or name.startswith(_CLEARANCE_PREFIX):
            val = c.get("value")
            if val:
                out[name] = val
    return out


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
        # 2026-05-21 fetch-layer regression fix: the tier_escalator uses
        # httpx (DIRECT) + curl_cffi (DC_PROXY / RESIDENTIAL) providers —
        # none of them execute JavaScript. For tasks that need
        # ``RenderMode.RENDER`` (AppFolio / Avalon / Knock / RentCafe SPA
        # marketing sites where the unit data lives in JS-rendered DOM),
        # routing through the escalator returns empty bodies on every tier
        # → ladder exhausted → ``generic:no_body_short_circuit``.
        #
        # Validated 2026-05-21 against 50 known-passing controls: baseline
        # image (4046a2a) fetched all 50 RENDER tasks successfully via the
        # patchright path below; the post-merge HEAD routed them through
        # the escalator and got 0/50 strict units. Restrict escalator to
        # non-RENDER tasks (HEAD/GET); RENDER falls through to the
        # patchright renderer.
        if (
            ENABLE_TIER_ESCALATION
            and profile is not None
            and task.render_mode != RenderMode.RENDER
        ):
            from .tier_escalator import fetch_with_escalation
            return await fetch_with_escalation(task, profile)
        start_ms = _now_ms()
        host = urlparse(task.url).netloc
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

        return last_result

    async def _try_unlocker_fallback(
        self, task: CrawlTask, start_ms: int
    ) -> FetchResult | None:
        """Last-resort Web Unlocker fetch for a RENDER task blocked by CF.

        Returns the FetchResult on a successful unlock, or None when the
        Unlocker tier is unavailable or itself fails. Never raises — a
        failure here just leaves the original BOT_BLOCKED result standing.
        """
        try:
            from .providers.unlocker import UnlockerProvider

            provider = UnlockerProvider()
        except Exception as exc:
            log.warning("unlocker fallback unavailable: %s", exc)
            return None
        try:
            # UnlockerProvider ignores the profile arg on every transport
            # path; None is safe and avoids threading a profile this deep.
            result = await provider.fetch(task, None)  # type: ignore[arg-type]
            emit(
                EventKind.FETCH_TIER_ESCALATED,
                task.property_id,
                tier="UNLOCKER",
                reason="render_bot_blocked",
            )
            return result
        except Exception as exc:
            log.warning(
                "unlocker fallback failed for %s: %s", task.property_id, exc
            )
            return None

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
            render_result = await self._do_render(
                task, identity, proxy, attempt, start_ms
            )
            # RENDER tasks bypass the tier escalator (commit 0e85fbf — the
            # escalator's plain-GET lower tiers serve un-rendered HTML for
            # SPA pages, which broke 50/50 controls). But a Cloudflare
            # bot-fight block has no rendering remedy — patchright simply
            # gets 403'd. The Web Unlocker is the only escape: it returns
            # SOLVED, real HTML, which is sufficient for server-rendered
            # pages (e.g. Entrata /conventional/ — data is in the initial
            # HTML). When a RENDER task is BOT_BLOCKED and the Unlocker
            # tier is enabled, fall back to JUST the Unlocker. This does
            # NOT re-introduce 0e85fbf's regression — the plain-GET lower
            # tiers (DIRECT/DC_PROXY) are never engaged here.
            if (
                render_result.outcome == FetchOutcome.BOT_BLOCKED
                and ENABLE_TIER_ESCALATION
                and ENABLE_UNLOCKER_TIER
            ):
                unlocked = await self._try_unlocker_fallback(task, start_ms)
                if unlocked is not None and unlocked.outcome == FetchOutcome.OK:
                    return unlocked
            return render_result

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
            body_size = len(body) if body else None

            outcome, sig = classify(
                resp.status_code, resp_headers, body_head, body_size=body_size
            )

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
        page = await self._browsers.acquire(identity, proxy)
        network_log: list[dict[str, Any]] = []
        # Per-fetch transfer-byte circuit breaker (see _MAX_FETCH_BYTES).
        _xfer = {"bytes": 0, "tripped": False}

        async def _abort_all(route: Any) -> None:
            try:
                await route.abort()
            except Exception:
                ...

        try:
            # Intercept network requests
            async def _on_response(response: Any) -> None:
                try:
                    # Bandwidth circuit breaker — count every response's
                    # transfer size (content-length is free; no .body()
                    # needed and it covers JS/CSS/binary too). Once over
                    # the cap, abort all further requests so a runaway /
                    # proxied site can't burn $/GB. Best-effort; the
                    # already-captured DOM + network_log still extract.
                    if _MAX_FETCH_BYTES and not _xfer["tripped"]:
                        try:
                            _cl = response.headers.get("content-length")
                            _xfer["bytes"] += int(_cl) if _cl and _cl.isdigit() else 0
                        except Exception:
                            ...
                        if _xfer["bytes"] >= _MAX_FETCH_BYTES:
                            _xfer["tripped"] = True
                            try:
                                await page.route("**/*", _abort_all)
                            except Exception:
                                ...
                            log.warning(
                                "fetch.byte_cap_exceeded url=%s bytes=%d cap=%d "
                                "proxy=%s — aborting further requests",
                                task.url, _xfer["bytes"], _MAX_FETCH_BYTES,
                                bool(proxy),
                            )
                            # Structured event so a (proxied) fleet run can
                            # quantify cap hits + $/GB impact from
                            # events.jsonl, not just grep logs.
                            try:
                                emit(
                                    EventKind.FETCH_BYTE_CAP_EXCEEDED,
                                    task.property_id,
                                    url=task.url,
                                    bytes=_xfer["bytes"],
                                    cap=_MAX_FETCH_BYTES,
                                    proxied=bool(proxy),
                                    attempt=attempt,
                                )
                            except Exception:
                                ...
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
            try:
                resp = await page.goto(
                    task.url,
                    wait_until="domcontentloaded",
                    timeout=timeout_ms,
                )
            except Exception as exc:
                nav_exc = exc

            # Post-load settle: give SPAs a beat to hydrate. Next/Nuxt and
            # similar frameworks finish client-side rendering after
            # domcontentloaded fires.
            try:
                await asyncio.sleep(2.0)
            except Exception:
                pass

            # Interaction-driven CTA-hop (2026-05-18, env-gated, default OFF).
            # Some clusters only expose their real unit-data source AFTER a
            # click: funnel/UDR -> RealPage OLL wizard, resman -> myresman
            # availability portal, entrata -> the real widget. Static/
            # passive render never reaches it (validated: the source is not
            # in static HTML). When INTERACTION_CTA_HOP is truthy, click the
            # Apply/Check-Availability/Floor-Plans CTA(s); the response
            # handler registered above keeps capturing XHR into
            # ``network_log`` -> flows to ctx._api_responses -> existing
            # OLL/resman/entrata adapters. Strictly bounded + never raises,
            # so off it is a no-op and on it adds <=~8s with no SLO risk on
            # passive sites (gate is opt-in, prod default unset).
            if os.getenv("INTERACTION_CTA_HOP", "").strip().lower() in (
                "1", "true", "yes", "on"
            ):
                try:
                    await _drive_cta_hop(page)
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
            import re as _re_scroll

            _scroll_triggered = False
            if (
                task.render_mode == RenderMode.RENDER
                and body_text is not None
                and len(body_text) >= 50_000
                and not _re_scroll.search(r"\$\s*\d{1,3}(?:[,]\d{3})*", body_text)
            ):
                # Progressive scroll: step through 25%→50%→75%→100% of page height
                # with 200ms pauses at each stop. Real users don't jump instantly to
                # the bottom; single-jump scrolls are a detectable bot signal for
                # IntersectionObserver-based sites. After reaching 100%, wait 1.5s
                # for XHR round-trips to complete.
                _body_before_scroll = len(body_text)
                try:
                    await page.evaluate("""() => {
                        const h = document.body.scrollHeight;
                        const steps = [0.25, 0.5, 0.75, 1.0];
                        let i = 0;
                        function step() {
                            if (i >= steps.length) return;
                            window.scrollTo(0, Math.round(h * steps[i]));
                            i++;
                            if (i < steps.length) setTimeout(step, 200);
                        }
                        step();
                    }""")
                    # 4 steps × 200ms + 600ms headroom for the last step + 1.5s XHR wait
                    await asyncio.sleep(2.5)
                    _body_after_scroll_text = await page.content()
                    _body_after_size = len(_body_after_scroll_text or "")
                    _body_grew = _body_after_size > _body_before_scroll
                    _rent_appeared = bool(
                        _re_scroll.search(
                            r"\$\s*\d{1,3}(?:[,]\d{3})*",
                            _body_after_scroll_text or "",
                        )
                    )
                    if _body_after_scroll_text:
                        body_text = _body_after_scroll_text
                    _scroll_triggered = True
                    log.info(
                        "fetch.scroll_trigger url=%s body_before=%d body_after=%d"
                        " grew=%s rent_appeared=%s",
                        task.url,
                        _body_before_scroll,
                        _body_after_size,
                        _body_grew,
                        _rent_appeared,
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
                import re as _re_cf
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

                if portal_match or learned_wait:
                    import re as _re_q

                    has_dollar_rent = bool(
                        _re_q.search(r"\$\s?\d{3,4}(?:[,.]\d{3})?", body_text)
                    )
                    if not has_dollar_rent:
                        try:
                            wait_sec = 12.0 if portal_match else 5.0
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

            # Cookie-mint reuse (option b): harvest the clearance cookies the
            # patchright context just earned by passing the CF/DataDome
            # challenge, so the cheap curl_cffi active-fetch in the API
            # adapters can reuse the solved clearance instead of hitting the
            # wall again. Best-effort; failure just yields the pre-(b)
            # blocked-probe behaviour.
            clearance_cookies = await _harvest_clearance_cookies(page)
            if clearance_cookies:
                log.info(
                    "fetch.clearance_cookies_minted url=%s names=%s",
                    task.url, ",".join(sorted(clearance_cookies)),
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
                clearance_cookies=clearance_cookies,
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
            await self._browsers.release(page)


_CTA_CLICK_JS = r"""
(maxClicks) => {
  // Strict CTA matcher: the controls that hop to the real unit-data
  // source. NOT contact/tour/login/resident (those trigger CAPTCHA or
  // dead-end at a sign-in form — the exact false-positive trap).
  const GOOD = /(check\s*availab|view\s*pricing|see\s*availab|view\s*availab|all[-\s]?in\s*price|floor\s*plans?\s*(&|and)\s*pricing|see\s*floor\s*plans?|view\s*floor\s*plans?|apply\s*now|get\s*pricing|shop\s*(now|units?))/i;
  const BAD  = /(contact|schedule|tour|sign\s*in|log\s*in|login|resident|application_authentication|careers|privacy|terms)/i;
  const els = [...document.querySelectorAll('a,button,[role=button],[onclick]')];
  const picked = [];
  const seen = new Set();
  for (const e of els) {
    const t = ((e.innerText || e.textContent || '') + ' ' +
               (e.getAttribute && (e.getAttribute('aria-label') || '') || '')).trim();
    const href = (e.getAttribute && e.getAttribute('href')) || '';
    if (!t && !href) continue;
    if (BAD.test(t) || BAD.test(href)) continue;
    if (!(GOOD.test(t) || GOOD.test(href))) continue;
    const key = t.slice(0, 40) + '|' + href.slice(0, 60);
    if (seen.has(key)) continue;
    seen.add(key);
    picked.push(e);
    if (picked.length >= maxClicks) break;
  }
  picked.forEach((e) => { try { e.scrollIntoView(); e.click(); } catch (x) {} });
  return picked.length;
}
"""


async def _drive_cta_hop(page: Any) -> None:
    """Click the unit-data CTA(s) so post-interaction XHR is captured.

    Env-gated by the caller (``INTERACTION_CTA_HOP``). The page's
    ``response`` handler is already registered, so any XHR the click
    triggers (RealPage OLL appstate, myresman availability, the entrata
    widget…) lands in ``network_log`` -> ``ctx._api_responses`` -> the
    existing adapters. Strictly time-bounded; every step is best-effort
    and swallowed so this can never fail a render.
    """
    try:
        n = await asyncio.wait_for(page.evaluate(_CTA_CLICK_JS, 2), timeout=6.0)
    except Exception:
        return
    if not n:
        return
    # Bounded settle for the post-click navigation/XHR to fire and be
    # captured. Cap total added time so the 95%-success SLO is unaffected.
    try:
        await asyncio.sleep(5.0)
    except Exception:
        pass


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

        _default = Fetcher(
            proxy_pool=ProxyPool(proxy_urls),
            rate_limiter=HostRateLimiter(),
            robots=RobotsConsumer(),
            cond_cache=ConditionalCache(cache_dir / "conditional.sqlite"),
            identities=IdentityPool(),
            # Camoufox escalation rung: get_browser_pool() returns the
            # patchright BrowserContextPool unless ENABLE_CAMOUFOX=true AND
            # camoufox is importable, in which case it returns the
            # structurally-identical CamoufoxPool (Firefox/Gecko — passes
            # some CF JS challenges patchright fails). Flag-off ⇒ byte-
            # identical to the prior direct construction (zero blast
            # radius). Composes with cookie-mint (b): camoufox passes the
            # wall, (b) harvests its clearance cookie for cheap reuse.
            browsers=get_browser_pool(max_contexts=int(os.getenv("MAX_CONCURRENT_BROWSERS", "5"))),
            retry=RetryPolicy(),
        )
    return _default
