"""DirectProvider — fetches using a plain httpx client with no proxy.

TRANSIENT errors are retried up to MAX_ATTEMPTS within this provider.
BOT_BLOCKED is returned immediately so the tier_escalator can escalate.
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
from ma_poc.fetch.response_classifier import classify
from ma_poc.fetch.stealth import IdentityPool
from ma_poc.models.fetch_tier import FetchTier

if TYPE_CHECKING:
    from ma_poc.discovery.contracts import CrawlTask
    from ma_poc.models.scrape_profile import ScrapeProfile

log = logging.getLogger(__name__)

_MAX_ATTEMPTS = 3
_BASE_BACKOFF_MS = 500
_TIER = FetchTier.DIRECT
_IDENTITIES = IdentityPool()


class DirectProvider:
    """FetchProvider that uses a plain httpx client — no proxy."""

    tier_name: str = "DIRECT"

    async def fetch(
        self,
        task: "CrawlTask",
        profile: "ScrapeProfile",
    ) -> FetchResult:
        start_ms = _now_ms()
        attempts: list[int] = []
        last_result: FetchResult | None = None

        for attempt in range(1, _MAX_ATTEMPTS + 1):
            attempts.append(int(_TIER))
            result = await _single_attempt(task, proxy=None, attempt=attempt, start_ms=start_ms)
            last_result = result

            outcome = result.outcome
            if outcome in (FetchOutcome.OK, FetchOutcome.NOT_MODIFIED, FetchOutcome.HARD_FAIL):
                break
            if outcome == FetchOutcome.BOT_BLOCKED:
                # Let escalator decide what to do
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
            proxy_used=None,
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
        )
    finally:
        await client.aclose()


def _stamp(result: FetchResult, tier: FetchTier, attempts: list[int]) -> FetchResult:
    """Return a new FetchResult with fetch_tier_used and fetch_tier_attempts set."""
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
