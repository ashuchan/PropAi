"""Tests confirming the proxy URL flows from ProxyPool → http_client / browser_pool.

These tests mock at the boundary where the proxy string is actually used —
make_http_client for GET/HEAD fetches and BrowserContextPool.acquire for
RENDER fetches — verifying that the BrightData URL reaches the right call
intact and that proxy_used in FetchResult is redacted.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

from ma_poc.discovery.contracts import CrawlTask, TaskReason
from ma_poc.fetch.browser_pool import BrowserContextPool
from ma_poc.fetch.contracts import RenderMode
from ma_poc.fetch.fetcher import Fetcher
from ma_poc.fetch.http_client import _AdapterResponse
from ma_poc.fetch.proxy_pool import ProxyPool
from ma_poc.fetch.stealth import Identity, IdentityPool

_BD_URL = "http://brd-customer-hl_6785472d-zone-residential_proxy1:0owuh5392unq@brd.superproxy.io:33335"

_IDENTITY = Identity(
    user_agent="Mozilla/5.0 (Test)",
    accept_language="en-US,en;q=0.9",
    viewport=(1280, 720),
    platform="Windows",
)


def _make_mock_adapter(url: str) -> AsyncMock:
    """Return a mock that looks like _HttpxAdapter / _CurlCffiAdapter."""
    adapter = AsyncMock()
    adapter.request = AsyncMock(return_value=_AdapterResponse(
        status_code=200,
        headers={},
        content=b"<html></html>",
        final_url=url,
        cookies={},
    ))
    adapter.aclose = AsyncMock()
    return adapter


def _make_fetcher(proxy_url: str | None = _BD_URL) -> Fetcher:
    urls = [proxy_url] if proxy_url else []
    pool = ProxyPool(urls)
    browsers = BrowserContextPool(max_contexts=1)
    return Fetcher(
        proxy_pool=pool,
        rate_limiter=MagicMock(),
        robots=MagicMock(),
        cond_cache=MagicMock(),
        identities=IdentityPool(),
        browsers=browsers,
        retry=MagicMock(),
    )


def _get_task(render_mode: RenderMode = RenderMode.GET) -> CrawlTask:
    return CrawlTask(
        url="https://example.com/apartments",
        property_id="test-prop-01",
        priority=0,
        budget_ms=10_000,
        reason=TaskReason.SCHEDULED,
        render_mode=render_mode,
    )


# ── GET / HEAD path — make_http_client proxy kwarg ────────────────────────────


def test_get_request_passes_proxy_to_httpx() -> None:
    """make_http_client must receive the BrightData proxy URL."""
    fetcher = _make_fetcher(_BD_URL)
    task = _get_task(RenderMode.GET)
    captured: dict = {}

    def _fake_make_client(tier: object, proxy: object) -> AsyncMock:
        captured["proxy"] = proxy
        return _make_mock_adapter(task.url)

    async def _go() -> None:
        with patch("ma_poc.fetch.fetcher.make_http_client", side_effect=_fake_make_client):
            await fetcher._do_request(task, _IDENTITY, _BD_URL, None, None, 1, int(time.time() * 1000))

    asyncio.run(_go())
    assert captured.get("proxy") == _BD_URL


def test_get_no_proxy_passes_none_to_httpx() -> None:
    """When proxy=None, make_http_client must receive proxy=None."""
    fetcher = _make_fetcher(None)
    task = _get_task(RenderMode.GET)
    captured: dict = {}

    def _fake_make_client(tier: object, proxy: object) -> AsyncMock:
        captured["proxy"] = proxy
        return _make_mock_adapter(task.url)

    async def _go() -> None:
        with patch("ma_poc.fetch.fetcher.make_http_client", side_effect=_fake_make_client):
            await fetcher._do_request(task, _IDENTITY, None, None, None, 1, int(time.time() * 1000))

    asyncio.run(_go())
    assert captured.get("proxy") is None


def test_proxy_used_field_is_redacted_in_result() -> None:
    """FetchResult.proxy_used must not contain the plaintext password."""
    fetcher = _make_fetcher(_BD_URL)
    task = _get_task(RenderMode.GET)

    async def _go() -> object:
        with patch("ma_poc.fetch.fetcher.make_http_client", return_value=_make_mock_adapter(task.url)):
            return await fetcher._do_request(task, _IDENTITY, _BD_URL, None, None, 1, int(time.time() * 1000))

    result = asyncio.run(_go())
    assert result.proxy_used is not None
    assert "0owuh5392unq" not in result.proxy_used, "password must be redacted"
    assert "***" in result.proxy_used


# ── RENDER path — BrowserContextPool.acquire proxy arg ────────────────────────


def test_render_request_passes_proxy_to_browser_acquire() -> None:
    """_do_render must forward the proxy URL to BrowserContextPool.acquire."""
    fetcher = _make_fetcher(_BD_URL)
    task = _get_task(RenderMode.RENDER)
    acquired_with: list = []

    mock_page = MagicMock()
    mock_page.context = MagicMock()
    mock_page.goto = AsyncMock(return_value=MagicMock(status=200))
    mock_page.on = MagicMock()
    mock_page.content = AsyncMock(return_value="<html></html>")
    mock_page.url = task.url

    async def _fake_acquire(identity: object, proxy: object) -> MagicMock:
        acquired_with.append(proxy)
        return mock_page

    async def _go() -> None:
        with patch.object(fetcher._browsers, "acquire", side_effect=_fake_acquire):
            await fetcher._do_render(task, _IDENTITY, _BD_URL, 1, int(time.time() * 1000))

    asyncio.run(_go())
    assert len(acquired_with) == 1
    assert acquired_with[0] == _BD_URL


def test_render_no_proxy_passes_none_to_acquire() -> None:
    """When no proxy is configured, acquire must receive proxy=None."""
    fetcher = _make_fetcher(None)
    task = _get_task(RenderMode.RENDER)
    acquired_with: list = []

    mock_page = MagicMock()
    mock_page.context = MagicMock()
    mock_page.goto = AsyncMock(return_value=MagicMock(status=200))
    mock_page.on = MagicMock()
    mock_page.content = AsyncMock(return_value="<html></html>")
    mock_page.url = task.url

    async def _fake_acquire(identity: object, proxy: object) -> MagicMock:
        acquired_with.append(proxy)
        return mock_page

    async def _go() -> None:
        with patch.object(fetcher._browsers, "acquire", side_effect=_fake_acquire):
            await fetcher._do_render(task, _IDENTITY, None, 1, int(time.time() * 1000))

    asyncio.run(_go())
    assert len(acquired_with) == 1
    assert acquired_with[0] is None


# ── ProxyPool.pick() integration — proxy flows from pool to request ────────────


def test_proxy_pool_with_brightdata_url_returns_it() -> None:
    """ProxyPool.pick() must return the BrightData URL unchanged."""
    pool = ProxyPool([_BD_URL])
    picked = pool.pick(sticky_key="prop-01")
    assert picked == _BD_URL


def test_empty_proxy_pool_returns_none() -> None:
    """An empty ProxyPool must return None so fetcher skips the proxy kwarg."""
    pool = ProxyPool([])
    assert pool.pick(sticky_key="prop-01") is None


# ── L1 direct-first + CF-escalation contract (2026-05-23 incident fix) ────
#
# Production regression: with ``PROXY_POOL_URLS`` set, the L1 fetcher used
# to call ``proxy_pool.pick(...)`` unconditionally on every fetch, routing
# even the first entry-URL attempt through the proxy. When that proxy was
# unhealthy the entire run collapsed (90% → 14% on 2026-05-23).
#
# New contract:
#   * Attempt 1 is ALWAYS direct (proxy=None), regardless of pool state.
#   * Escalation to proxy is admitted ONLY on BOT_BLOCKED (CF / WAF).
#   * The admit decision goes through ``proxy_gate.decide_l1_escalate``.
#   * One escalation per fetch task — second BOT_BLOCKED does not retry.
#
# The tests below exercise the public ``Fetcher.fetch`` method end-to-end
# with ``_do_request`` patched so we can pin the contract without invoking
# real network code.


from ma_poc.fetch.contracts import FetchOutcome, FetchResult  # noqa: E402
from ma_poc.fetch.rate_limiter import HostRateLimiter  # noqa: E402
from ma_poc.fetch.retry_policy import RetryPolicy  # noqa: E402
from ma_poc.fetch.robots import RobotsConsumer  # noqa: E402


def _make_full_fetcher(proxy_url: str | None = _BD_URL) -> Fetcher:
    """Fetcher with real RetryPolicy + lightweight mocks for everything else.

    Distinct from ``_make_fetcher`` above which uses ``retry=MagicMock()``;
    we need a real retry policy because the new escalation logic interacts
    with the policy's BOT_BLOCKED → should_retry=False semantics.
    """
    urls = [proxy_url] if proxy_url else []
    pool = ProxyPool(urls)
    rate_limiter = MagicMock(spec=HostRateLimiter)
    rate_limiter.acquire = AsyncMock(return_value=None)
    rate_limiter.on_rate_limited = MagicMock(return_value=None)
    robots = MagicMock(spec=RobotsConsumer)
    robots.is_allowed = AsyncMock(return_value=True)
    cond_cache = MagicMock()
    cond_cache.read = MagicMock(return_value=(None, None))
    cond_cache.write = MagicMock(return_value=None)
    return Fetcher(
        proxy_pool=pool,
        rate_limiter=rate_limiter,
        robots=robots,
        cond_cache=cond_cache,
        identities=IdentityPool(),
        browsers=BrowserContextPool(max_contexts=1),
        retry=RetryPolicy(max_attempts=3, base_ms=1),  # tiny base — fast tests
    )


def _ok_result(task: CrawlTask, attempt: int) -> FetchResult:
    return FetchResult(
        url=task.url,
        outcome=FetchOutcome.OK,
        status=200,
        body=b"<html>ok</html>",
        headers={},
        render_mode=task.render_mode,
        final_url=task.url,
        attempts=attempt,
        elapsed_ms=100,
    )


def _bot_blocked_result(task: CrawlTask, attempt: int) -> FetchResult:
    return FetchResult(
        url=task.url,
        outcome=FetchOutcome.BOT_BLOCKED,
        status=403,
        body=b"<html>CF challenge</html>",
        headers={},
        render_mode=task.render_mode,
        final_url=task.url,
        attempts=attempt,
        elapsed_ms=200,
        error_signature="CF_CHALLENGE",
    )


def _transient_result(task: CrawlTask, attempt: int) -> FetchResult:
    return FetchResult(
        url=task.url,
        outcome=FetchOutcome.TRANSIENT,
        status=None,
        body=None,
        headers={},
        render_mode=task.render_mode,
        final_url=task.url,
        attempts=attempt,
        elapsed_ms=100,
        error_signature="timeout",
    )


def test_fetch_uses_no_proxy_on_first_attempt_even_with_pool() -> None:
    """Direct-first contract. Even when ``PROXY_POOL_URLS`` is set, the
    first ``_do_request`` call MUST receive ``proxy=None``. Yesterday's
    behaviour (proxy injected from line 249) was the source of the
    2026-05-23 production regression."""
    fetcher = _make_full_fetcher(_BD_URL)
    task = _get_task(RenderMode.GET)
    proxies_seen: list[object] = []

    async def _fake_do_request(t, ident, proxy, *_args, **_kw):  # noqa: ANN001
        proxies_seen.append(proxy)
        return _ok_result(t, attempt=1)

    async def _go() -> None:
        with patch.object(fetcher, "_do_request", side_effect=_fake_do_request):
            await fetcher.fetch(task)

    asyncio.run(_go())
    assert len(proxies_seen) == 1, "OK should not trigger any retry"
    assert proxies_seen[0] is None, (
        "First attempt must be direct; pool must not be consulted "
        "before a BOT_BLOCKED outcome justifies escalation."
    )


def test_fetch_escalates_to_proxy_on_bot_blocked() -> None:
    """Escalation contract. When attempt 1 returns BOT_BLOCKED and the
    pool is non-empty, attempt 2 MUST receive the pool's proxy URL."""
    fetcher = _make_full_fetcher(_BD_URL)
    task = _get_task(RenderMode.GET)
    proxies_seen: list[object] = []
    outcomes = iter([
        _bot_blocked_result(task, 1),
        _ok_result(task, 2),
    ])

    async def _fake_do_request(t, ident, proxy, *_args, **_kw):  # noqa: ANN001
        proxies_seen.append(proxy)
        return next(outcomes)

    async def _go() -> None:
        with patch.object(fetcher, "_do_request", side_effect=_fake_do_request), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            return await fetcher.fetch(task)

    result = asyncio.run(_go())
    assert len(proxies_seen) == 2, "Expected one retry after BOT_BLOCKED → 2 attempts"
    assert proxies_seen[0] is None, "Attempt 1 must be direct"
    assert proxies_seen[1] == _BD_URL, (
        "Attempt 2 must use the proxy from the pool because direct "
        "got CF-blocked and the L1 escalation gate admitted."
    )
    assert result.outcome == FetchOutcome.OK


def test_fetch_does_not_escalate_on_transient() -> None:
    """TRANSIENT failures have their own retry path — they must NOT
    trigger CF-escalation. Otherwise a flaky network for 100 sites
    would burn 100 proxy hops needlessly."""
    fetcher = _make_full_fetcher(_BD_URL)
    task = _get_task(RenderMode.GET)
    proxies_seen: list[object] = []
    outcomes = iter([
        _transient_result(task, 1),
        _transient_result(task, 2),
        _ok_result(task, 3),
    ])

    async def _fake_do_request(t, ident, proxy, *_args, **_kw):  # noqa: ANN001
        proxies_seen.append(proxy)
        return next(outcomes)

    async def _go() -> object:
        with patch.object(fetcher, "_do_request", side_effect=_fake_do_request), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            return await fetcher.fetch(task)

    asyncio.run(_go())
    # All 3 attempts must be direct — TRANSIENT does not trigger
    # CF-escalation. The retry policy may rotate IDENTITY but the proxy
    # stays None unless RATE_LIMITED (separate path) fires.
    assert all(p is None for p in proxies_seen), (
        f"All attempts on TRANSIENT must be direct; saw {proxies_seen!r}"
    )


def test_fetch_does_not_escalate_when_pool_empty() -> None:
    """No ``PROXY_POOL_URLS`` configured → no escalation possible.
    Property fails-fast as FAILED_UNREACHABLE without burning retries."""
    fetcher = _make_full_fetcher(proxy_url=None)
    task = _get_task(RenderMode.GET)
    proxies_seen: list[object] = []

    async def _fake_do_request(t, ident, proxy, *_args, **_kw):  # noqa: ANN001
        proxies_seen.append(proxy)
        return _bot_blocked_result(t, attempt=1)

    async def _go() -> object:
        with patch.object(fetcher, "_do_request", side_effect=_fake_do_request), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            return await fetcher.fetch(task)

    result = asyncio.run(_go())
    # RetryPolicy returns should_retry=False on BOT_BLOCKED, and the
    # escalation gate denies with NO_PROXY_CONFIGURED → only one attempt.
    assert len(proxies_seen) == 1
    assert proxies_seen[0] is None
    assert result.outcome == FetchOutcome.BOT_BLOCKED


def test_fetch_escalates_only_once_on_repeated_bot_block() -> None:
    """One-shot escalation. Even if attempt 2 (via proxy) ALSO returns
    BOT_BLOCKED, the gate denies the second escalation (hop budget
    exhausted) and the fetch terminates rather than burning hops on a
    proxy that's also being CF-walled."""
    fetcher = _make_full_fetcher(_BD_URL)
    task = _get_task(RenderMode.GET)
    proxies_seen: list[object] = []
    outcomes = iter([
        _bot_blocked_result(task, 1),
        _bot_blocked_result(task, 2),
    ])

    async def _fake_do_request(t, ident, proxy, *_args, **_kw):  # noqa: ANN001
        proxies_seen.append(proxy)
        return next(outcomes)

    async def _go() -> object:
        with patch.object(fetcher, "_do_request", side_effect=_fake_do_request), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            return await fetcher.fetch(task)

    result = asyncio.run(_go())
    # Direct attempt + 1 proxy escalation = 2 attempts. NOT 3.
    assert len(proxies_seen) == 2
    assert proxies_seen[0] is None
    assert proxies_seen[1] == _BD_URL
    assert result.outcome == FetchOutcome.BOT_BLOCKED
