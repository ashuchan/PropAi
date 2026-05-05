"""ResidentialProvider — fetches via BrightData residential proxy.

Phase E4: real implementation.

Key constraints vs DcProxyProvider:
- Sticky sessions keyed on property_id (same IP for a property across retries)
- 0.5 RPS soft cap — we enforce this with a 2-second inter-request sleep
- BOT_BLOCKED returned immediately for escalator; TRANSIENT retried once
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from ma_poc.fetch.block_signatures import match_block_signature
from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode
from ma_poc.fetch.headers import chrome_header_set
from ma_poc.fetch.http_client import make_http_client
from ma_poc.fetch.proxy.base import ProxyTier
from ma_poc.fetch.proxy.brightdata import BrightDataProvider
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
    """

    tier_name: str = "RESIDENTIAL"

    def __init__(self) -> None:
        self._bd = BrightDataProvider()

    async def fetch(
        self,
        task: "CrawlTask",
        profile: "ScrapeProfile",
    ) -> FetchResult:
        start_ms = _now_ms()
        attempts: list[int] = []
        last_result: FetchResult | None = None

        # Sticky session: same property_id → same exit IP
        proxy_cfg = self._bd.get_config(
            tier=ProxyTier.RESIDENTIAL,
            canonical_id=task.property_id,
        )
        proxy_url = proxy_cfg.to_httpx_url()

        for attempt in range(1, _MAX_ATTEMPTS + 1):
            attempts.append(int(_TIER))
            result = await _single_attempt(task, proxy=proxy_url, attempt=attempt, start_ms=start_ms)
            last_result = result

            outcome = result.outcome
            if outcome in (FetchOutcome.OK, FetchOutcome.NOT_MODIFIED, FetchOutcome.HARD_FAIL):
                break
            if outcome == FetchOutcome.BOT_BLOCKED:
                # Don't retry on residential — escalate to unlocker
                break
            if attempt < _MAX_ATTEMPTS:
                # 0.5 RPS cap for residential traffic
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
    identity = _IDENTITIES.pick(sticky_key=task.property_id)
    cold_visit = not (task.etag or task.last_modified)
    headers = chrome_header_set(identity, cold_visit=cold_visit)
    if task.etag:
        headers["If-None-Match"] = task.etag
    if task.last_modified:
        headers["If-Modified-Since"] = task.last_modified

    client = make_http_client(_TIER, proxy)
    try:
        resp = await client.request(method, task.url, headers=headers, timeout=timeout_sec)
        resp_headers = resp.headers
        body = resp.content if method == "GET" else None
        body_head = body[:4096] if body else None
        outcome, sig = classify(resp.status_code, resp_headers, body_head)
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
