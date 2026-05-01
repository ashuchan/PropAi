"""DcProxyProvider — fetches via BrightData datacenter proxy.

Delegates actual HTTP work to the same _single_attempt helper used by
DirectProvider, but injects a BrightData DC proxy URL.  If BrightData
credentials are absent the provider raises RuntimeError at construction time
(operators must set the env vars listed in brightdata.py before enabling the
DC_PROXY tier).

TRANSIENT errors retry twice within this provider.
BOT_BLOCKED is returned immediately for the escalator to promote further.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

import httpx

from ma_poc.fetch.block_signatures import match_block_signature
from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode
from ma_poc.fetch.proxy.base import ProxyTier
from ma_poc.fetch.proxy.brightdata import BrightDataProvider
from ma_poc.fetch.response_classifier import classify
from ma_poc.models.fetch_tier import FetchTier

if TYPE_CHECKING:
    from ma_poc.discovery.contracts import CrawlTask
    from ma_poc.models.scrape_profile import ScrapeProfile

log = logging.getLogger(__name__)

_MAX_ATTEMPTS = 2
_BASE_BACKOFF_MS = 800
_TIER = FetchTier.DC_PROXY


class DcProxyProvider:
    """FetchProvider that routes requests through BrightData datacenter proxies."""

    tier_name: str = "DC_PROXY"

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

        proxy_cfg = self._bd.get_config(
            tier=ProxyTier.DATACENTER,
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
                break
            if attempt < _MAX_ATTEMPTS:
                wait_s = _BASE_BACKOFF_MS * (2 ** (attempt - 1)) / 1000.0
                await asyncio.sleep(wait_s)

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
    headers = {"User-Agent": "Mozilla/5.0 (compatible; PropAi/1.0)"}

    try:
        async with httpx.AsyncClient(
            proxy=proxy,
            timeout=timeout_sec,
            follow_redirects=True,
        ) as client:
            resp = await client.request(method, task.url, headers=headers)
            resp_headers = {k.lower(): v for k, v in resp.headers.items()}
            body = resp.content if method == "GET" else None
            body_head = body[:4096] if body else None
            outcome, sig = classify(resp.status_code, resp_headers, body_head)
            block_sig = None
            if outcome == FetchOutcome.BOT_BLOCKED:
                block_sig = match_block_signature(body_head or b"", resp_headers, resp.status_code)
            proxy_display = _redact(proxy) if proxy else None
            return FetchResult(
                url=task.url,
                outcome=outcome,
                status=resp.status_code,
                body=body,
                headers=resp_headers,
                render_mode=task.render_mode,
                final_url=str(resp.url),
                attempts=attempt,
                elapsed_ms=_now_ms() - start_ms,
                etag=resp_headers.get("etag"),
                last_modified=resp_headers.get("last-modified"),
                error_signature=sig,
                proxy_used=proxy_display,
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
            proxy_used=_redact(proxy) if proxy else None,
        )


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
