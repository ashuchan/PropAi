"""UnlockerProvider — fetches via BrightData Web Unlocker.

Phase E5: real implementation.

Uses a dedicated BrightData Unlocker zone with auto-unblocking. This tier
is the last resort before DLQ_PARK — it's expensive but reliably bypasses
most bot-detection systems.

Two transport modes (auto-selected at construction):
  * proxy mode — the Unlocker proxy zone. Env:
        BRIGHTDATA_CUSTOMER_ID / BRIGHTDATA_UNLOCKER_ZONE /
        BRIGHTDATA_UNLOCKER_PASSWORD
  * api mode   — the Web Unlocker REST API (api.brightdata.com/request).
        Env: WEB_UNLOCKER_KEY (+ optional WEB_UNLOCKER_ZONE). Fallback
        when the proxy-zone creds are absent.

No retries: a single attempt is made. If BOT_BLOCKED even here, the escalator
routes to DLQ_PARK.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import urllib.request
from typing import TYPE_CHECKING
from urllib.parse import quote

from ma_poc.fetch._jar_helper import jar_for
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

_TIER = FetchTier.UNLOCKER
_BRIGHTDATA_HOST = os.environ.get("BRIGHTDATA_HOST", "brd.superproxy.io")
_BRIGHTDATA_PORT = int(os.environ.get("BRIGHTDATA_PORT", "33335"))
_IDENTITIES = IdentityPool()

# BrightData Web Unlocker REST API — the API-token transport. Used when the
# proxy-zone creds (BRIGHTDATA_UNLOCKER_ZONE / _PASSWORD) are not provisioned
# but a WEB_UNLOCKER_KEY token is. 2026-05-22: verified this path solves the
# Cloudflare bot-fight on Entrata /conventional/ pages that the proxy/RENDER
# path 403s — see investigations/2026-05-22-validation-grind.
_WU_API = "https://api.brightdata.com/request"


def _web_unlocker_key() -> str:
    """BrightData Web Unlocker API token from WEB_UNLOCKER_KEY, or ''."""
    return os.environ.get("WEB_UNLOCKER_KEY", "").strip()


def _web_unlocker_zone() -> str:
    """Web Unlocker zone name (WEB_UNLOCKER_ZONE), default web_unlocker1."""
    return os.environ.get("WEB_UNLOCKER_ZONE", "web_unlocker1").strip() or "web_unlocker1"


def _web_unlocker_api_fetch(url: str, timeout: int) -> tuple[int, bytes]:
    """Fetch *url* via the BrightData Web Unlocker REST API.

    Returns ``(status, body)``. ``status`` is 200 on a solved page, or 0
    on transport/auth error (caller treats 0 as a failed fetch). The Web
    Unlocker auto-solves Cloudflare managed challenges server-side.
    """
    key = _web_unlocker_key()
    if not key:
        return 0, b""
    payload = json.dumps(
        {"zone": _web_unlocker_zone(), "url": url, "format": "raw"}
    ).encode()
    req = urllib.request.Request(  # noqa: S310 — fixed BrightData API host
        _WU_API,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return 200, resp.read()
    except Exception as exc:  # noqa: BLE001 — caller treats 0 as empty
        log.warning(
            "web_unlocker_api.error url=%s err=%s: %s",
            url, type(exc).__name__, str(exc)[:120],
        )
        return 0, b""


async def _api_attempt(task: CrawlTask, start_ms: int) -> FetchResult:
    """Single fetch via the Web Unlocker REST API, as a FetchResult."""
    budget = getattr(task, "budget_ms", None)
    timeout_s = int(min(budget / 1000.0, 120.0)) if budget else 120
    status, body = await asyncio.to_thread(
        _web_unlocker_api_fetch, task.url, timeout_s
    )
    body_head = body[:4096] if body else None
    norm_status = status or None  # 0 -> None so classify reads it as a failure
    outcome, sig = classify(norm_status, {}, body_head)
    block_sig = None
    if outcome == FetchOutcome.BOT_BLOCKED:
        block_sig = match_block_signature(body_head or b"", {}, status)
    return FetchResult(
        url=task.url,
        outcome=outcome,
        status=norm_status,
        body=body or None,
        headers={},
        render_mode=task.render_mode,
        final_url=task.url,
        attempts=1,
        elapsed_ms=_now_ms() - start_ms,
        error_signature=sig,
        proxy_used="***unlocker-api***",
        block_signature=block_sig,
    )


class UnlockerProvider:
    """FetchProvider that routes requests through BrightData Web Unlocker.

    Two transport modes, selected at construction:
      * ``proxy`` — the Unlocker proxy zone. Needs BRIGHTDATA_CUSTOMER_ID
        / _UNLOCKER_ZONE / _UNLOCKER_PASSWORD.
      * ``api``   — the Web Unlocker REST API (api.brightdata.com/request),
        keyed on WEB_UNLOCKER_KEY alone. Fallback when the proxy-zone creds
        are absent — many deployments provision only the API token.

    Raises RuntimeError only when NEITHER credential set is available, so
    the escalator cleanly skips the tier rather than crashing the run.
    """

    tier_name: str = "UNLOCKER"

    def __init__(self) -> None:
        try:
            self._proxy_url: str | None = _build_proxy_url()
            self._mode = "proxy"
        except RuntimeError:
            # Proxy-zone creds absent — fall back to the API-token path.
            if _web_unlocker_key():
                self._proxy_url = None
                self._mode = "api"
            else:
                raise

    async def fetch(
        self,
        task: CrawlTask,
        profile: ScrapeProfile,
    ) -> FetchResult:
        start_ms = _now_ms()
        attempts = [int(_TIER)]
        if self._mode == "api":
            result = await _api_attempt(task, start_ms)
        else:
            assert self._proxy_url is not None
            result = await _single_attempt(
                task, proxy=self._proxy_url, attempt=1, start_ms=start_ms
            )
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
    task: CrawlTask,
    proxy: str,
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
