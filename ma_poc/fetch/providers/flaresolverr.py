"""FlareSolverrProvider — bypasses CF JS challenges using a local FlareSolverr instance.

FlareSolverr (https://github.com/FlareSolverr/FlareSolverr) runs a real
undetected Chrome browser that can solve Cloudflare JS challenges and Turnstile
widgets. It exposes a local HTTP API that we call with the target URL; it
returns the solved cookies + response body.

Only handles Tier-1 JS challenges. Does NOT bypass WAF "Attention Required"
blocks (which are IP-reputation based — nothing but a different IP helps).

Setup:
    docker run -d -p 8191:8191 ghcr.io/flaresolverr/flaresolverr:latest

OR without Docker:
    pip install flaresolverr
    flaresolverr  # starts on :8191

Enable via env var:
    FLARESOLVERR_URL=http://localhost:8191  (default, also accepts remote)

Cost: ~5-15s per request (FlareSolverr solves the challenge synchronously).
Only invoked when the standard fetch returns BOT_BLOCKED with a JS challenge body.
"""
from __future__ import annotations

import logging
import os
import time
from typing import TYPE_CHECKING

import httpx

from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode
from ma_poc.models.fetch_tier import FetchTier

if TYPE_CHECKING:
    from ma_poc.discovery.contracts import CrawlTask
    from ma_poc.models.scrape_profile import ScrapeProfile

log = logging.getLogger(__name__)

_FLARESOLVERR_URL = os.getenv("FLARESOLVERR_URL", "http://localhost:8191")
_TIMEOUT_S = 90  # FlareSolverr can take up to 60s to solve
_TIER = FetchTier.FLARESOLVERR  # type: ignore[attr-defined]  # added to FetchTier enum

# CF JS challenge fingerprints — only invoke FlareSolverr for these
_CF_CHALLENGE_BYTES = (
    b"Just a moment",
    b"challenge-platform",
    b"__cf_chl_",
    b"Checking your browser",
    b"cf-browser-verification",
)

_CF_WAF_BYTES = (
    b"Attention Required",
    b"Sorry, you have been blocked",
)


def is_cf_js_challenge(body: bytes | str | None) -> bool:
    """True if body looks like a solvable CF JS challenge (not a WAF block)."""
    if not body:
        return False
    if isinstance(body, str):
        body = body[:4096].encode("utf-8", errors="replace")
    head = body[:4096]
    if any(p in head for p in _CF_WAF_BYTES):
        return False  # WAF block — FlareSolverr won't help
    return any(p in head for p in _CF_CHALLENGE_BYTES)


def is_available() -> bool:
    """Check if FlareSolverr service is reachable."""
    try:
        r = httpx.get(f"{_FLARESOLVERR_URL}/health", timeout=3.0)
        return r.status_code == 200
    except Exception:
        return False


class FlareSolverrProvider:
    """FetchProvider that routes through FlareSolverr for CF JS challenge bypass."""

    tier_name: str = "FLARESOLVERR"

    async def fetch(
        self,
        task: "CrawlTask",
        profile: "ScrapeProfile",
    ) -> FetchResult:
        start_ms = int(time.time() * 1000)

        if not is_available():
            log.warning("FlareSolverr not available at %s", _FLARESOLVERR_URL)
            return FetchResult(
                url=task.url,
                outcome=FetchOutcome.HARD_FAIL,
                status=None,
                body=None,
                headers={},
                render_mode=task.render_mode,
                final_url=task.url,
                attempts=1,
                elapsed_ms=int(time.time() * 1000) - start_ms,
                error_signature="FLARESOLVERR_UNAVAILABLE",
            )

        payload = {
            "cmd": "request.get",
            "url": task.url,
            "maxTimeout": int(_TIMEOUT_S * 1000),
        }

        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT_S + 5) as client:
                resp = await client.post(
                    f"{_FLARESOLVERR_URL}/v1",
                    json=payload,
                )
            data = resp.json()
        except Exception as exc:
            return FetchResult(
                url=task.url,
                outcome=FetchOutcome.TRANSIENT,
                status=None,
                body=None,
                headers={},
                render_mode=task.render_mode,
                final_url=task.url,
                attempts=1,
                elapsed_ms=int(time.time() * 1000) - start_ms,
                error_signature=f"FLARESOLVERR_REQUEST_ERROR:{exc}",
            )

        status_fs = data.get("status", "error")
        if status_fs != "ok":
            msg = data.get("message", "unknown")
            log.warning("FlareSolverr returned status=%s message=%s", status_fs, msg)
            return FetchResult(
                url=task.url,
                outcome=FetchOutcome.BOT_BLOCKED,
                status=None,
                body=None,
                headers={},
                render_mode=task.render_mode,
                final_url=task.url,
                attempts=1,
                elapsed_ms=int(time.time() * 1000) - start_ms,
                error_signature=f"FLARESOLVERR_FAILED:{msg[:80]}",
            )

        solution = data.get("solution", {})
        body_text = solution.get("response", "")
        final_url = solution.get("url", task.url)
        response_status = solution.get("status", 200)
        cookies = {c["name"]: c["value"] for c in solution.get("cookies", [])}

        body_bytes = body_text.encode("utf-8", errors="replace") if body_text else b""
        cf_cookie = cookies.get("cf_clearance")
        if cf_cookie:
            log.info(
                "FlareSolverr solved CF challenge for %s — cf_clearance obtained",
                task.url,
            )

        # Save cf_clearance to the property's cookie jar for future requests
        if cf_cookie and task.property_id:
            try:
                from urllib.parse import urlparse as _up
                from ma_poc.fetch.cookie_jar import PropertyCookieJar
                from ma_poc.fetch.stealth import IdentityPool

                _ids = IdentityPool()
                slot = _ids.current_slot(task.property_id)
                host = _up(task.url).netloc
                jar = PropertyCookieJar(task.property_id, slot)
                jar.update_from_response(host, cookies)
            except Exception as _jar_exc:
                log.debug("Failed to persist FlareSolverr cookies: %s", _jar_exc)

        return FetchResult(
            url=task.url,
            outcome=FetchOutcome.OK if response_status < 400 else FetchOutcome.BOT_BLOCKED,
            status=response_status,
            body=body_bytes,
            headers={},
            render_mode=task.render_mode,
            final_url=final_url,
            attempts=1,
            elapsed_ms=int(time.time() * 1000) - start_ms,
            error_signature=None if response_status < 400 else f"HTTP_{response_status}",
        )
