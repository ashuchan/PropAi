"""UnlockerProvider — fetches via BrightData Web Unlocker.

Phase E5: real implementation.

Uses a dedicated BrightData Unlocker zone with auto-unblocking. This tier
is the last resort before DLQ_PARK — it's expensive but reliably bypasses
most bot-detection systems.

Required env vars:
    BRIGHTDATA_CUSTOMER_ID
    BRIGHTDATA_UNLOCKER_ZONE
    BRIGHTDATA_UNLOCKER_PASSWORD

No retries: a single attempt is made. If BOT_BLOCKED even here, the escalator
routes to DLQ_PARK.
"""

from __future__ import annotations

import logging
import os
import time
from typing import TYPE_CHECKING
from urllib.parse import quote

import httpx

from ma_poc.fetch.block_signatures import match_block_signature
from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode
from ma_poc.fetch.response_classifier import classify
from ma_poc.models.fetch_tier import FetchTier

if TYPE_CHECKING:
    from ma_poc.discovery.contracts import CrawlTask
    from ma_poc.models.scrape_profile import ScrapeProfile

log = logging.getLogger(__name__)

_TIER = FetchTier.UNLOCKER
_BRIGHTDATA_HOST = os.environ.get("BRIGHTDATA_HOST", "brd.superproxy.io")
_BRIGHTDATA_PORT = int(os.environ.get("BRIGHTDATA_PORT", "33335"))


class UnlockerProvider:
    """FetchProvider that routes requests through BrightData Web Unlocker."""

    tier_name: str = "UNLOCKER"

    def __init__(self) -> None:
        self._proxy_url = _build_proxy_url()

    async def fetch(
        self,
        task: "CrawlTask",
        profile: "ScrapeProfile",
    ) -> FetchResult:
        start_ms = _now_ms()
        attempts = [int(_TIER)]
        result = await _single_attempt(task, proxy=self._proxy_url, attempt=1, start_ms=start_ms)
        return _stamp(result, _TIER, attempts)


def _build_proxy_url() -> str:
    """Construct the BrightData Unlocker proxy URL from env vars."""
    customer_id = _require("BRIGHTDATA_CUSTOMER_ID")
    zone = _require("BRIGHTDATA_UNLOCKER_ZONE")
    password = _require("BRIGHTDATA_UNLOCKER_PASSWORD")

    username = f"brd-customer-{customer_id}-zone-{zone}"
    user_enc = quote(username, safe="")
    pwd_enc = quote(password, safe="")
    return f"http://{user_enc}:{pwd_enc}@{_BRIGHTDATA_HOST}:{_BRIGHTDATA_PORT}"


def _require(key: str) -> str:
    val = os.environ.get(key)
    if not val:
        raise RuntimeError(
            f"{key} is required for UnlockerProvider. "
            "Set it via Secret Manager in prod or .env in dev."
        )
    return val


async def _single_attempt(
    task: "CrawlTask",
    proxy: str,
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
                proxy_used="***unlocker***",
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
            proxy_used="***unlocker***",
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
