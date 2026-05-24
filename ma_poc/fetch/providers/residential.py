"""ResidentialProvider — fetches via BrightData residential proxy.

Phase E4: real implementation.

Key constraints vs DcProxyProvider:
- Sticky sessions keyed on property_id (same IP for a property across retries)
- 0.5 RPS soft cap — we enforce this with a 2-second inter-request sleep
- BOT_BLOCKED triggers one force-rotated session retry (2026-05-24);
  if that also fails, escalate to the unlocker

Session force-rotation (2026-05-24)
==================================
BrightData residential sessions pin one exit IP for ~30 min. Without
rotation, retries on a burned IP all fail identically. We now consult
``SessionBurnTracker`` before each attempt:

  * If the property's prior calls have crossed the burn threshold,
    request a higher ``session_salt`` so BrightData hands us a fresh IP.
  * On every BOT_BLOCKED, ``mark_failure(property_id)`` increments the
    counter and may advance the salt. The provider retries ONE more
    time with the new salt before giving up.
  * On OK, ``mark_success(property_id)`` resets the counter (the IP is
    working — keep it).

Across separate ``fetch()`` calls for the same property the tracker
state persists, so a property that crossed the threshold in run N
starts run N+1 already on the rotated salt.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from ma_poc.fetch._jar_helper import jar_for
from ma_poc.fetch.block_signatures import match_block_signature
from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode
from ma_poc.fetch.headers import chrome_header_set
from ma_poc.fetch.http_client import make_http_client
from ma_poc.fetch.proxy.base import ProxyTier
from ma_poc.fetch.proxy.brightdata import BrightDataProvider
from ma_poc.fetch.proxy.session_burn import SessionBurnTracker, get_default_tracker
from ma_poc.fetch.response_classifier import classify
from ma_poc.fetch.stealth import IdentityPool
from ma_poc.models.fetch_tier import FetchTier

if TYPE_CHECKING:
    from ma_poc.discovery.contracts import CrawlTask
    from ma_poc.models.scrape_profile import ScrapeProfile

log = logging.getLogger(__name__)

_MAX_ATTEMPTS = 2
# 0.5 RPS = 2 seconds between requests on residential to respect cost controls
_RESI_INTER_REQUEST_SLEEP_S = 2.0
_TIER = FetchTier.RESIDENTIAL
_IDENTITIES = IdentityPool()


class ResidentialProvider:
    """FetchProvider that routes requests through BrightData residential proxies.

    Sticky sessions ensure the same exit IP is used for retries on the same
    property_id, improving geo-accuracy for geo-gated listing pages.

    When the IP gets burned (consecutive 403s), the provider force-rotates
    the BrightData session-id so the next attempt lands on a fresh IP.
    See ``SessionBurnTracker`` for the burn-tracking semantics.
    """

    tier_name: str = "RESIDENTIAL"

    def __init__(
        self,
        *,
        burn_tracker: SessionBurnTracker | None = None,
    ) -> None:
        """Args:
            burn_tracker: Optional injected tracker for tests. Production
                code uses the process-wide default singleton.
        """
        self._bd = BrightDataProvider()
        self._burn = burn_tracker or get_default_tracker()

    async def fetch(
        self,
        task: "CrawlTask",
        profile: "ScrapeProfile",
    ) -> FetchResult:
        start_ms = _now_ms()
        attempts: list[int] = []
        last_result: FetchResult | None = None

        # Read the burn state once before the loop. If a prior fetch
        # already burned this property's session, we'll start on a
        # rotated salt — no wasted attempts on a known-bad IP.
        salt = self._burn.next_salt(task.property_id)

        for attempt in range(1, _MAX_ATTEMPTS + 1):
            attempts.append(int(_TIER))

            # Sticky-with-rotation: same property_id + current salt
            # → same exit IP for this attempt; bumping salt between
            # attempts forces BrightData to pick a different IP.
            proxy_cfg = self._bd.get_config(
                tier=ProxyTier.RESIDENTIAL,
                canonical_id=task.property_id,
                session_salt=salt,
            )
            proxy_url = proxy_cfg.to_httpx_url()
            result = await _single_attempt(
                task, proxy=proxy_url, attempt=attempt, start_ms=start_ms
            )
            last_result = result

            outcome = result.outcome
            if outcome in (FetchOutcome.OK, FetchOutcome.NOT_MODIFIED):
                # Success — reset the burn counter AND tell the tracker
                # what salt actually worked. If we got OK from a
                # rotated salt (in-call salt advance), pin that salt
                # so the next fetch on this property starts on the IP
                # we already know is good.
                self._burn.mark_success(
                    task.property_id, working_salt=salt
                )
                break
            if outcome == FetchOutcome.HARD_FAIL:
                # HARD_FAIL is terminal — TLS / 4xx-not-blocked / DNS.
                # No proxy rotation will help; escalate to unlocker.
                break
            if outcome == FetchOutcome.BOT_BLOCKED:
                # Record the failure (this may advance the tracker's
                # salt across the rotate threshold for next-run lookups).
                tracker_salt = self._burn.mark_failure(task.property_id)
                if attempt < _MAX_ATTEMPTS:
                    # Unconditional in-fetch rotation: the IP we just
                    # hit was 403'd, so any retry on the same session
                    # is wasted. Bump our local salt by at least 1 to
                    # force BrightData to pick a fresh IP. If the
                    # tracker already advanced further (cross-fetch
                    # burn carry), use that higher salt.
                    next_salt = max(salt + 1, tracker_salt)
                    log.info(
                        "residential.session_rotate property=%s "
                        "salt %d→%d after BOT_BLOCKED (sig=%s)",
                        task.property_id, salt, next_salt,
                        result.block_signature or result.error_signature,
                    )
                    salt = next_salt
                    # 0.5 RPS cap still applies between attempts.
                    await asyncio.sleep(_RESI_INTER_REQUEST_SLEEP_S)
                    continue
                # Budget exhausted → escalate to the unlocker tier.
                break
            if attempt < _MAX_ATTEMPTS:
                # TRANSIENT / RATE_LIMITED / other — retry on same
                # session after the cost-control sleep.
                await asyncio.sleep(_RESI_INTER_REQUEST_SLEEP_S)

        assert last_result is not None
        return _stamp(last_result, _TIER, attempts)


async def _single_attempt(
    task: "CrawlTask",
    proxy: str | None,
    attempt: int,
    start_ms: int,
) -> FetchResult:
    timeout_sec = min(task.budget_ms / 1000.0, 30.0)
    method = "HEAD" if task.render_mode == RenderMode.HEAD else "GET"
    identity = _IDENTITIES.pick_chrome_only(sticky_key=task.property_id)
    cold_visit = not (task.etag or task.last_modified)
    headers = chrome_header_set(identity, cold_visit=cold_visit)
    if task.etag:
        headers["If-None-Match"] = task.etag
    if task.last_modified:
        headers["If-Modified-Since"] = task.last_modified

    jar, host, cookies = jar_for(task, _IDENTITIES)
    client = make_http_client(_TIER, proxy)
    try:
        resp = await client.request(method, task.url, headers=headers, cookies=cookies, timeout=timeout_sec)
        resp_headers = resp.headers
        body = resp.content if method == "GET" else None
        body_head = body[:4096] if body else None
        outcome, sig = classify(resp.status_code, resp_headers, body_head)
        if resp.cookies:
            jar.update_from_response(host, resp.cookies)
        block_sig = None
        if outcome == FetchOutcome.BOT_BLOCKED:
            block_sig = match_block_signature(body_head or b"", resp_headers, resp.status_code)
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
            proxy_used=_redact(proxy),
            block_signature=block_sig,
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
            proxy_used=_redact(proxy),
        )
    finally:
        await client.aclose()


def _stamp(result: FetchResult, tier: FetchTier, attempts: list[int]) -> FetchResult:
    return FetchResult(
        url=result.url,
        outcome=result.outcome,
        status=result.status,
        body=result.body,
        headers=result.headers,
        render_mode=result.render_mode,
        final_url=result.final_url,
        attempts=result.attempts,
        elapsed_ms=result.elapsed_ms,
        network_log=result.network_log,
        etag=result.etag,
        last_modified=result.last_modified,
        error_signature=result.error_signature,
        proxy_used=result.proxy_used,
        fetch_tier_used=int(tier),
        fetch_tier_attempts=attempts,
        block_signature=result.block_signature,
    )


def _now_ms() -> int:
    return int(time.time() * 1000)


def _redact(url: str | None) -> str | None:
    if not url:
        return None
    import re
    return re.sub(r"://[^@]+@", "://***@", url)
