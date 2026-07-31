"""Render-retry-on-transient in Fetcher._do_request (2026-07-31).

A client-XHR-dependent SPA (Entrata ProspectPortal) whose render TIMES OUT
with no captured XHR would otherwise fall to the curl_cffi static shell, which
OK-returns a 200 carrying zero client XHR -> 0 units. A static fetch can never
run the client XHR; only a re-render can. When ``ENABLE_RENDER_RETRY_ON_TRANSIENT``
is on, the render is retried ONCE, gated to that exact failure mode
(timeout-class TRANSIENT + empty network_log + first attempt), before the
static fall pre-empts it. Default OFF must be byte-for-byte the prior path.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ma_poc.discovery.contracts import CrawlTask, TaskReason
from ma_poc.fetch.browser_pool import BrowserContextPool
from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode
from ma_poc.fetch.fetcher import Fetcher
from ma_poc.fetch.proxy_pool import ProxyPool
from ma_poc.fetch.stealth import Identity, IdentityPool


class _StillBlockedResponse:
    """curl_cffi shim the static fallback declines (status != 200), so a
    non-recovered render result stands after the retry path."""

    def __init__(self, url: str) -> None:
        self.status_code = 403
        self.text = "<html>Just a moment...</html>"
        self.content = self.text.encode()
        self.headers: dict[str, str] = {}
        self.url = url


@pytest.fixture(autouse=True)
def _stub_probe_seam(monkeypatch: pytest.MonkeyPatch) -> None:
    from ma_poc.pms.adapters import _probe

    def _blocked_probe_get(url: str, **_kw: Any) -> _StillBlockedResponse:
        return _StillBlockedResponse(url)

    monkeypatch.setattr(_probe, "probe_get", _blocked_probe_get)


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


def _render_task() -> CrawlTask:
    return CrawlTask(
        url="https://www.example.com/x/conventional/",
        property_id="ENT-001",
        priority=0,
        budget_ms=10_000,
        reason=TaskReason.SCHEDULED,
        render_mode=RenderMode.RENDER,
    )


def _result(
    outcome: FetchOutcome,
    *,
    body: bytes = b"",
    status: int | None = None,
    error_signature: str | None = None,
    network_log: list[dict[str, Any]] | None = None,
) -> FetchResult:
    return FetchResult(
        url="https://www.example.com/x/conventional/",
        outcome=outcome,
        status=status,
        body=body,
        headers={},
        render_mode=RenderMode.RENDER,
        final_url="https://www.example.com/x/conventional/",
        attempts=1,
        elapsed_ms=10,
        error_signature=error_signature,
        network_log=network_log if network_log is not None else [],
    )


def _TIMEOUT_TRANSIENT() -> FetchResult:
    # The render timed out with no captured XHR — the exact failure mode.
    return _result(FetchOutcome.TRANSIENT, error_signature="timeout")


def _OK_WITH_UNITS() -> FetchResult:
    return _result(
        FetchOutcome.OK,
        body=b"<html><div class='fp-card'>$1,500</div></html>",
        status=200,
        network_log=[{"url": "/floorplans/", "status": 200}],
    )


async def _run(fetcher: Fetcher) -> FetchResult:
    return await fetcher._do_request(_render_task(), _IDENTITY, None, None, None, 1, 0)


@pytest.mark.asyncio
async def test_flag_on_retries_render_once_and_recovers() -> None:
    fetcher = _make_fetcher()
    fetcher._do_render = AsyncMock(side_effect=[_TIMEOUT_TRANSIENT(), _OK_WITH_UNITS()])
    with patch("ma_poc.fetch.fetcher._RENDER_RETRY_ON_TRANSIENT", True):
        result = await _run(fetcher)
    assert result.outcome == FetchOutcome.OK
    assert result.body and b"fp-card" in result.body
    assert fetcher._do_render.call_count == 2  # one retry


@pytest.mark.asyncio
async def test_flag_off_does_not_retry() -> None:
    """Default OFF: exactly one render, no retry — prior behaviour."""
    fetcher = _make_fetcher()
    fetcher._do_render = AsyncMock(side_effect=[_TIMEOUT_TRANSIENT(), _OK_WITH_UNITS()])
    with patch("ma_poc.fetch.fetcher._RENDER_RETRY_ON_TRANSIENT", False):
        result = await _run(fetcher)
    assert fetcher._do_render.call_count == 1
    assert result.outcome == FetchOutcome.TRANSIENT  # static 403 stub declined


@pytest.mark.asyncio
async def test_flag_on_but_network_log_present_does_not_retry() -> None:
    """A render that captured XHR is not the empty-shell failure mode."""
    fetcher = _make_fetcher()
    captured = _result(
        FetchOutcome.TRANSIENT,
        error_signature="timeout",
        network_log=[{"url": "/x", "status": 200}],
    )
    fetcher._do_render = AsyncMock(side_effect=[captured, _OK_WITH_UNITS()])
    with patch("ma_poc.fetch.fetcher._RENDER_RETRY_ON_TRANSIENT", True):
        await _run(fetcher)
    assert fetcher._do_render.call_count == 1


@pytest.mark.asyncio
async def test_flag_on_non_timeout_signature_does_not_retry() -> None:
    """Dead-domain DNS/SSL etc. carry a different signature — excluded, so a
    render is never wasted on them."""
    fetcher = _make_fetcher()
    non_timeout = _result(FetchOutcome.TRANSIENT, error_signature="no_response")
    fetcher._do_render = AsyncMock(side_effect=[non_timeout, _OK_WITH_UNITS()])
    with patch("ma_poc.fetch.fetcher._RENDER_RETRY_ON_TRANSIENT", True):
        await _run(fetcher)
    assert fetcher._do_render.call_count == 1


@pytest.mark.asyncio
async def test_flag_on_retry_also_fails_is_bounded_to_one_extra_render() -> None:
    """If the retry render also fails, it is NOT retried again — at most one
    extra render, then the static/unlocker fallbacks proceed."""
    fetcher = _make_fetcher()
    fetcher._do_render = AsyncMock(
        side_effect=[_TIMEOUT_TRANSIENT(), _TIMEOUT_TRANSIENT()]
    )
    with patch("ma_poc.fetch.fetcher._RENDER_RETRY_ON_TRANSIENT", True):
        result = await _run(fetcher)
    assert fetcher._do_render.call_count == 2  # bounded — never a third
    assert result.outcome == FetchOutcome.TRANSIENT
