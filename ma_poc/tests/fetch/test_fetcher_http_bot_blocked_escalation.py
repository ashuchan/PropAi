"""HTTP-GET path BOT_BLOCKED → curl_cffi auto-escalation (2026-05-24).

Pins the L1 GET-path auto-escalation added to ``Fetcher._do_request``:
when the plain httpx DIRECT fetch returns BOT_BLOCKED (Cloudflare /
Imperva 403 from a vanity-site WAF that fingerprints httpx's TLS
stack), the fetcher retries once with curl_cffi chrome120 before
returning the failure.

Background — 2026-05-24 random-sample probe of 50 FAILED_UNREACHABLE
properties in the focused-3886351 canary cohort:
  * httpx: 9/10 = 90% 403 (BOT_BLOCKED)
  * curl_cffi chrome120: 10/10 = 100% 200 OK
  * 29/50 are Entrata Prospect Portal sites that our Template A/B/C
    adapters would extract from immediately once unblocked

Same trigger as the RENDER path, just routed from the GET branch.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ma_poc.discovery.contracts import CrawlTask, TaskReason
from ma_poc.fetch.browser_pool import BrowserContextPool
from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode
from ma_poc.fetch.fetcher import Fetcher
from ma_poc.fetch.proxy_pool import ProxyPool
from ma_poc.fetch.stealth import Identity, IdentityPool

_IDENTITY = Identity(
    user_agent="Mozilla/5.0 (Test)",
    accept_language="en-US,en;q=0.9",
    viewport=(1280, 720),
    platform="Windows",
)


def _make_fetcher() -> Fetcher:
    return Fetcher(
        proxy_pool=ProxyPool([]),
        rate_limiter=MagicMock(),
        robots=MagicMock(),
        cond_cache=MagicMock(),
        identities=IdentityPool(),
        browsers=BrowserContextPool(max_contexts=1),
        retry=MagicMock(),
    )


def _get_task(url: str = "https://www.example.com/") -> CrawlTask:
    return CrawlTask(
        url=url,
        property_id="GET-001",
        priority=0,
        budget_ms=10_000,
        reason=TaskReason.SCHEDULED,
        render_mode=RenderMode.GET,
    )


def _make_probe_response(status: int, text: str, url: str = "https://www.example.com/"):
    r = MagicMock()
    r.status_code = status
    r.text = text
    r.url = url
    return r


def _make_httpx_response(status: int, body: bytes, headers: dict | None = None):
    """Shape that matches _AdapterResponse from http_client.py."""
    r = MagicMock()
    r.status_code = status
    r.content = body
    r.headers = headers or {"content-type": "text/html"}
    r.final_url = "https://www.example.com/"
    r.cookies = {}
    return r


# ─── happy path: httpx BOT_BLOCKED → curl_cffi recovers ───────────────


@pytest.mark.asyncio
async def test_get_bot_blocked_falls_back_to_curl_cffi() -> None:
    """The KEY new test: httpx GET returns 403 (CF block) on a
    Cloudflare-fronted Entrata vanity site. curl_cffi chrome120
    bypass kicks in automatically, returns the real HTML.

    No env flag required — this is the production lift path."""
    fetcher = _make_fetcher()
    # httpx returns 403 (CF block-page body)
    cf_block_body = (b"<html><head><title>Just a moment...</title></head>"
                     b"<body>Checking your browser before accessing"
                     + b" x" * 5000 + b"</body></html>")
    big_html = "<html><body>" + ("y" * 5000) + "</body></html>"

    # Pre-stage: tell rate_limiter to no-op
    fetcher._rate_limiter.acquire = AsyncMock()
    fetcher._robots.is_allowed = AsyncMock(return_value=True)
    fetcher._cond_cache.get = MagicMock(return_value=(None, None))

    with (
        patch(
            "ma_poc.fetch.fetcher.make_http_client"
        ) as mock_client_factory,
        patch(
            "ma_poc.pms.adapters._probe.probe_get",
            return_value=_make_probe_response(200, big_html),
        ),
    ):
        mock_client = MagicMock()
        mock_client.request = AsyncMock(
            return_value=_make_httpx_response(403, cf_block_body)
        )
        mock_client.aclose = AsyncMock()
        mock_client_factory.return_value = mock_client

        result = await fetcher._do_request(
            _get_task(), _IDENTITY, None, None, None, 1, 0
        )

    assert result.outcome == FetchOutcome.OK, (
        f"expected curl_cffi to recover but got {result.outcome!r}; "
        f"sig={result.error_signature!r}"
    )
    assert result.status == 200
    assert result.body and b"y" * 100 in result.body
    assert result.error_signature == "curl_cffi_chrome120_bypass"


# ─── failure path: curl_cffi also blocked → original BOT_BLOCKED ──────


@pytest.mark.asyncio
async def test_get_bot_blocked_curl_cffi_miss_keeps_block() -> None:
    """When httpx 403s AND curl_cffi also returns 403 (genuinely
    blocked even by chrome120 fingerprint), the original BOT_BLOCKED
    stands — no false-positive OK."""
    fetcher = _make_fetcher()
    cf_block_body = b"<html>Just a moment" + b" x" * 5000 + b"</html>"

    fetcher._rate_limiter.acquire = AsyncMock()
    fetcher._robots.is_allowed = AsyncMock(return_value=True)
    fetcher._cond_cache.get = MagicMock(return_value=(None, None))

    with (
        patch("ma_poc.fetch.fetcher.make_http_client") as mock_client_factory,
        patch(
            "ma_poc.pms.adapters._probe.probe_get",
            return_value=_make_probe_response(403, ""),
        ),
    ):
        mock_client = MagicMock()
        mock_client.request = AsyncMock(
            return_value=_make_httpx_response(403, cf_block_body)
        )
        mock_client.aclose = AsyncMock()
        mock_client_factory.return_value = mock_client

        result = await fetcher._do_request(
            _get_task(), _IDENTITY, None, None, None, 1, 0
        )

    assert result.outcome == FetchOutcome.BOT_BLOCKED


# ─── precedence: httpx OK skips fallback ──────────────────────────────


@pytest.mark.asyncio
async def test_get_ok_does_not_invoke_curl_cffi() -> None:
    """A successful httpx GET (200 with real body) never touches the
    curl_cffi fallback — no cost on healthy sites."""
    fetcher = _make_fetcher()
    big_html = b"<html><body>real content " + b"z" * 5000 + b"</body></html>"

    fetcher._rate_limiter.acquire = AsyncMock()
    fetcher._robots.is_allowed = AsyncMock(return_value=True)
    fetcher._cond_cache.get = MagicMock(return_value=(None, None))

    with (
        patch("ma_poc.fetch.fetcher.make_http_client") as mock_client_factory,
        patch("ma_poc.pms.adapters._probe.probe_get") as probe_mock,
    ):
        mock_client = MagicMock()
        mock_client.request = AsyncMock(
            return_value=_make_httpx_response(200, big_html)
        )
        mock_client.aclose = AsyncMock()
        mock_client_factory.return_value = mock_client

        result = await fetcher._do_request(
            _get_task(), _IDENTITY, None, None, None, 1, 0
        )

    assert result.outcome == FetchOutcome.OK
    probe_mock.assert_not_called()


# ─── precedence: only DIRECT (no proxy) escalates ─────────────────────


@pytest.mark.asyncio
async def test_get_bot_blocked_with_proxy_does_not_escalate() -> None:
    """When a proxy is configured (DC_PROXY+ tier), the BOT_BLOCKED
    stays — the tier escalator handles paid tiers; the curl_cffi
    DIRECT fallback only fires when no proxy is in play."""
    fetcher = _make_fetcher()
    cf_block_body = b"<html>403</html>"

    fetcher._rate_limiter.acquire = AsyncMock()
    fetcher._robots.is_allowed = AsyncMock(return_value=True)
    fetcher._cond_cache.get = MagicMock(return_value=(None, None))

    with (
        patch("ma_poc.fetch.fetcher.make_http_client") as mock_client_factory,
        patch("ma_poc.pms.adapters._probe.probe_get") as probe_mock,
    ):
        mock_client = MagicMock()
        mock_client.request = AsyncMock(
            return_value=_make_httpx_response(403, cf_block_body)
        )
        mock_client.aclose = AsyncMock()
        mock_client_factory.return_value = mock_client

        result = await fetcher._do_request(
            _get_task(),
            _IDENTITY,
            "http://user:pass@proxy:8080",  # proxy set
            None, None, 1, 0,
        )

    assert result.outcome == FetchOutcome.BOT_BLOCKED
    # Critical: do NOT invoke curl_cffi fallback when proxy was already
    # in play — the escalator owns that path.
    probe_mock.assert_not_called()


# ─── exception path: httpx raises → curl_cffi recovers ────────────────


@pytest.mark.asyncio
async def test_get_httpx_exception_falls_back_to_curl_cffi() -> None:
    """When httpx raises (TLS handshake abort, HTTP/2 protocol error,
    connection reset) — the classifier turns the exception into a
    HARD_FAIL or BOT_BLOCKED — the curl_cffi fallback fires.

    Validated 2026-05-24: probe of TLS-failing-in-httpx sites shows
    curl_cffi's chrome120 stack negotiates successfully where plain
    httpx's default ALPN/cipher set is rejected."""
    fetcher = _make_fetcher()
    big_html = "<html>" + "h" * 5000 + "</html>"

    fetcher._rate_limiter.acquire = AsyncMock()
    fetcher._robots.is_allowed = AsyncMock(return_value=True)
    fetcher._cond_cache.get = MagicMock(return_value=(None, None))

    with (
        patch("ma_poc.fetch.fetcher.make_http_client") as mock_client_factory,
        patch(
            "ma_poc.pms.adapters._probe.probe_get",
            return_value=_make_probe_response(200, big_html),
        ),
        patch(
            "ma_poc.fetch.fetcher.classify",
            return_value=(FetchOutcome.HARD_FAIL, "TLS_HANDSHAKE_FAIL"),
        ),
    ):
        # httpx raises — classify() (patched above) turns it into HARD_FAIL
        mock_client = MagicMock()
        mock_client.request = AsyncMock(
            side_effect=ConnectionResetError("TLS handshake reset")
        )
        mock_client.aclose = AsyncMock()
        mock_client_factory.return_value = mock_client

        result = await fetcher._do_request(
            _get_task(), _IDENTITY, None, None, None, 1, 0
        )

    assert result.outcome == FetchOutcome.OK
    assert result.body and b"h" * 100 in result.body


# ─── HEAD method: should NOT trigger fallback (cheap probe, save cost)─


@pytest.mark.asyncio
async def test_head_bot_blocked_does_not_escalate() -> None:
    """HEAD requests are cheap probes (e.g. ETag check). When they hit
    a 403, the curl_cffi fallback (which always does GET) would waste
    a round trip without giving us a body to extract from. Skip it."""
    fetcher = _make_fetcher()
    cf_block_body = b"<html>403</html>"

    fetcher._rate_limiter.acquire = AsyncMock()
    fetcher._robots.is_allowed = AsyncMock(return_value=True)
    fetcher._cond_cache.get = MagicMock(return_value=(None, None))

    head_task = CrawlTask(
        url="https://www.example.com/",
        property_id="HEAD-001",
        priority=0,
        budget_ms=10_000,
        reason=TaskReason.SCHEDULED,
        render_mode=RenderMode.HEAD,
    )

    with (
        patch("ma_poc.fetch.fetcher.make_http_client") as mock_client_factory,
        patch("ma_poc.pms.adapters._probe.probe_get") as probe_mock,
    ):
        mock_client = MagicMock()
        mock_client.request = AsyncMock(
            return_value=_make_httpx_response(403, cf_block_body)
        )
        mock_client.aclose = AsyncMock()
        mock_client_factory.return_value = mock_client

        result = await fetcher._do_request(
            head_task, _IDENTITY, None, None, None, 1, 0
        )

    assert result.outcome == FetchOutcome.BOT_BLOCKED
    probe_mock.assert_not_called()
