"""curl_cffi chrome120 BOT_BLOCKED bypass test (2026-05-23).

Pins the free Cloudflare-bypass fallback added to ``Fetcher._do_request``:
when a RENDER task comes back BOT_BLOCKED, the fetcher tries
``curl_cffi`` with ``impersonate="chrome120"`` FIRST (no flag gate, no
proxy, no API cost) before falling through to the paid Web Unlocker.

Background — 2026-05-23 canary diagnostic: 116 Cloudflare-walled
properties in the ``generic:no_body_short_circuit`` cohort. Probing
them with curl_cffi chrome120 yielded 12/12 (100%) bypass at 200 OK
with the full marketing HTML. The fix wires that bypass directly into
the L1 fetcher, ungated.
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


def _render_task() -> CrawlTask:
    return CrawlTask(
        url="https://www.example.com/",
        property_id="CF-001",
        priority=0,
        budget_ms=10_000,
        reason=TaskReason.SCHEDULED,
        render_mode=RenderMode.RENDER,
    )


def _result(outcome: FetchOutcome, body: bytes, status: int | None) -> FetchResult:
    return FetchResult(
        url="https://www.example.com/",
        outcome=outcome,
        status=status,
        body=body,
        headers={},
        render_mode=RenderMode.RENDER,
        final_url="https://www.example.com/",
        attempts=1,
        elapsed_ms=10,
    )


def _BLOCKED() -> FetchResult:
    return _result(FetchOutcome.BOT_BLOCKED, b"<html>Just a moment</html>", 403)


def _make_probe_response(status: int, text: str, url: str = "https://www.example.com/"):
    """Minimal shim mimicking curl_cffi's response object."""
    r = MagicMock()
    r.status_code = status
    r.text = text
    r.url = url
    return r


# ─── happy path: curl_cffi bypasses CF without any flag flips ────────


@pytest.mark.asyncio
async def test_render_bot_blocked_falls_back_to_curl_cffi_no_flags() -> None:
    """The KEY test: curl_cffi fallback fires WITHOUT requiring the
    ENABLE_TIER_ESCALATION / ENABLE_UNLOCKER_TIER flags."""
    fetcher = _make_fetcher()
    fetcher._do_render = AsyncMock(return_value=_BLOCKED())
    big_html = "<html><body>" + ("x" * 5000) + "</body></html>"
    with (
        patch("ma_poc.fetch.fetcher.ENABLE_TIER_ESCALATION", False),
        patch("ma_poc.fetch.fetcher.ENABLE_UNLOCKER_TIER", False),
        patch(
            "ma_poc.pms.adapters._probe.probe_get",
            return_value=_make_probe_response(200, big_html),
        ),
    ):
        result = await fetcher._do_request(
            _render_task(), _IDENTITY, None, None, None, 1, 0
        )
    assert result.outcome == FetchOutcome.OK
    assert result.status == 200
    assert result.body and b"x" * 100 in result.body
    assert result.error_signature == "curl_cffi_chrome120_bypass"


# ─── failure paths: curl_cffi miss → fall through ────────────────────


@pytest.mark.asyncio
async def test_curl_cffi_403_falls_through_to_unlocker() -> None:
    """When curl_cffi ALSO gets blocked (403), the Unlocker is tried
    next IF its flags are on."""
    fetcher = _make_fetcher()
    fetcher._do_render = AsyncMock(return_value=_BLOCKED())

    unlocker_inst = MagicMock()
    unlocker_inst.fetch = AsyncMock(
        return_value=_result(FetchOutcome.OK, b"<html>unlocked</html>", 200)
    )
    unlocker_cls = MagicMock(return_value=unlocker_inst)

    with (
        patch("ma_poc.fetch.fetcher.ENABLE_TIER_ESCALATION", True),
        patch("ma_poc.fetch.fetcher.ENABLE_UNLOCKER_TIER", True),
        patch(
            "ma_poc.pms.adapters._probe.probe_get",
            return_value=_make_probe_response(403, ""),
        ),
        patch(
            "ma_poc.fetch.providers.unlocker.UnlockerProvider", unlocker_cls
        ),
    ):
        result = await fetcher._do_request(
            _render_task(), _IDENTITY, None, None, None, 1, 0
        )
    assert result.outcome == FetchOutcome.OK
    assert b"unlocked" in result.body
    unlocker_cls.assert_called()


@pytest.mark.asyncio
async def test_curl_cffi_small_body_treated_as_fail() -> None:
    """A 200 OK with body < 1KB is treated as a residual CF stub and
    NOT accepted as a bypass — the BOT_BLOCKED result stands."""
    fetcher = _make_fetcher()
    fetcher._do_render = AsyncMock(return_value=_BLOCKED())
    with (
        patch("ma_poc.fetch.fetcher.ENABLE_TIER_ESCALATION", False),
        patch("ma_poc.fetch.fetcher.ENABLE_UNLOCKER_TIER", False),
        patch(
            "ma_poc.pms.adapters._probe.probe_get",
            return_value=_make_probe_response(200, "tiny"),
        ),
    ):
        result = await fetcher._do_request(
            _render_task(), _IDENTITY, None, None, None, 1, 0
        )
    assert result.outcome == FetchOutcome.BOT_BLOCKED


@pytest.mark.asyncio
async def test_curl_cffi_exception_falls_through_safely() -> None:
    """If curl_cffi itself raises, the fallback returns None (never
    raises). The original BOT_BLOCKED stands when no Unlocker flag."""
    fetcher = _make_fetcher()
    fetcher._do_render = AsyncMock(return_value=_BLOCKED())
    with (
        patch("ma_poc.fetch.fetcher.ENABLE_TIER_ESCALATION", False),
        patch("ma_poc.fetch.fetcher.ENABLE_UNLOCKER_TIER", False),
        patch(
            "ma_poc.pms.adapters._probe.probe_get",
            side_effect=RuntimeError("network gone"),
        ),
    ):
        result = await fetcher._do_request(
            _render_task(), _IDENTITY, None, None, None, 1, 0
        )
    assert result.outcome == FetchOutcome.BOT_BLOCKED


# ─── precedence: OK render skips fallback ────────────────────────────


@pytest.mark.asyncio
async def test_render_ok_does_not_invoke_curl_cffi() -> None:
    """A successful RENDER never touches the curl_cffi fallback."""
    fetcher = _make_fetcher()
    fetcher._do_render = AsyncMock(
        return_value=_result(FetchOutcome.OK, b"<html>ok</html>", 200)
    )
    with patch("ma_poc.pms.adapters._probe.probe_get") as probe_mock:
        result = await fetcher._do_request(
            _render_task(), _IDENTITY, None, None, None, 1, 0
        )
    assert result.outcome == FetchOutcome.OK
    probe_mock.assert_not_called()


# ─── direct unit test on the helper ──────────────────────────────────


@pytest.mark.asyncio
async def test_try_curl_cffi_fallback_returns_none_when_probe_unavailable() -> None:
    """The helper returns None (never raises) when probe_get's import
    or call path itself fails — defensive against curl_cffi missing."""
    fetcher = _make_fetcher()
    with patch(
        "ma_poc.pms.adapters._probe.probe_get",
        side_effect=ImportError("no curl_cffi"),
    ):
        out = await fetcher._try_curl_cffi_fallback(_render_task(), 0)
    assert out is None


@pytest.mark.asyncio
async def test_try_curl_cffi_fallback_constructs_ok_fetchresult() -> None:
    """When curl_cffi returns 200 + big body, the helper returns a
    well-formed FetchResult that downstream layers can consume."""
    fetcher = _make_fetcher()
    big_html = "<html>" + "y" * 5000 + "</html>"
    with patch(
        "ma_poc.pms.adapters._probe.probe_get",
        return_value=_make_probe_response(200, big_html),
    ):
        out = await fetcher._try_curl_cffi_fallback(_render_task(), 0)
    assert out is not None
    assert out.outcome == FetchOutcome.OK
    assert out.status == 200
    assert out.body and len(out.body) > 1000
    assert out.attempts == 1
    assert out.error_signature == "curl_cffi_chrome120_bypass"


# ─── TRANSIENT path: curl_cffi also fires on transient failures ──────


def _TRANSIENT() -> FetchResult:
    """RENDER timeout / connection reset — pre-fix this would have
    returned to caller as a hard fail. Post-fix: curl_cffi gets a
    chance to recover."""
    return _result(FetchOutcome.TRANSIENT, b"", None)


@pytest.mark.asyncio
async def test_render_transient_falls_back_to_curl_cffi() -> None:
    """The 2026-05-23 TRANSIENT-cohort fix: RENDER TRANSIENT now also
    triggers the curl_cffi fallback. Probed 9/15 (60%) bypass on the
    canary TRANSIENT sample — DNS/SSL failures stay TRANSIENT (correct)
    but recoverable timeouts get unlocked."""
    fetcher = _make_fetcher()
    fetcher._do_render = AsyncMock(return_value=_TRANSIENT())
    big_html = "<html>" + ("z" * 5000) + "</html>"
    with (
        patch("ma_poc.fetch.fetcher.ENABLE_TIER_ESCALATION", False),
        patch("ma_poc.fetch.fetcher.ENABLE_UNLOCKER_TIER", False),
        patch(
            "ma_poc.pms.adapters._probe.probe_get",
            return_value=_make_probe_response(200, big_html),
        ),
    ):
        result = await fetcher._do_request(
            _render_task(), _IDENTITY, None, None, None, 1, 0
        )
    assert result.outcome == FetchOutcome.OK
    assert b"z" * 100 in result.body


@pytest.mark.asyncio
async def test_render_transient_curl_cffi_miss_keeps_transient() -> None:
    """When curl_cffi also fails (DNS / SSL on a dead domain), the
    original TRANSIENT result stands — the Unlocker is NOT tried
    (would just waste API calls on terminal infrastructure failures)."""
    fetcher = _make_fetcher()
    fetcher._do_render = AsyncMock(return_value=_TRANSIENT())

    unlocker_inst = MagicMock()
    unlocker_inst.fetch = AsyncMock(
        return_value=_result(FetchOutcome.OK, b"<html>unlocked</html>", 200)
    )
    unlocker_cls = MagicMock(return_value=unlocker_inst)

    with (
        patch("ma_poc.fetch.fetcher.ENABLE_TIER_ESCALATION", True),
        patch("ma_poc.fetch.fetcher.ENABLE_UNLOCKER_TIER", True),
        patch(
            "ma_poc.pms.adapters._probe.probe_get",
            side_effect=RuntimeError("DNS resolve failed"),
        ),
        patch(
            "ma_poc.fetch.providers.unlocker.UnlockerProvider", unlocker_cls
        ),
    ):
        result = await fetcher._do_request(
            _render_task(), _IDENTITY, None, None, None, 1, 0
        )
    assert result.outcome == FetchOutcome.TRANSIENT
    # Unlocker MUST NOT be invoked for TRANSIENT — saves cost on
    # dead-domain DNS / SSL failures.
    unlocker_cls.assert_not_called()


def _HARD_FAIL() -> FetchResult:
    """Playwright SSL handshake / 4xx non-retriable. Some HARD_FAIL
    cases are RECOVERABLE via curl_cffi (different TLS stack handles
    cert chain edge cases Playwright trips on). Validated 2/4 of the
    canary HARD_FAIL cohort recover (toapts.com,
    marquettemanagement.reslisting.com — both 200 OK 100-356KB)."""
    return _result(FetchOutcome.HARD_FAIL, b"", None)


@pytest.mark.asyncio
async def test_render_hard_fail_falls_back_to_curl_cffi() -> None:
    """The 2026-05-23 HARD_FAIL fix: SSL-failing-in-Playwright sites
    often respond fine via curl_cffi's chrome120 TLS stack. Recover
    when possible; otherwise the HARD_FAIL stands."""
    fetcher = _make_fetcher()
    fetcher._do_render = AsyncMock(return_value=_HARD_FAIL())
    big_html = "<html>" + ("h" * 5000) + "</html>"
    with (
        patch("ma_poc.fetch.fetcher.ENABLE_TIER_ESCALATION", False),
        patch("ma_poc.fetch.fetcher.ENABLE_UNLOCKER_TIER", False),
        patch(
            "ma_poc.pms.adapters._probe.probe_get",
            return_value=_make_probe_response(200, big_html),
        ),
    ):
        result = await fetcher._do_request(
            _render_task(), _IDENTITY, None, None, None, 1, 0
        )
    assert result.outcome == FetchOutcome.OK
    assert b"h" * 100 in result.body


@pytest.mark.asyncio
async def test_render_hard_fail_curl_cffi_miss_keeps_hard_fail() -> None:
    """When curl_cffi also fails (401 auth wall, 409, etc. — site is
    genuinely unreachable), HARD_FAIL stands. Unlocker NOT invoked —
    paid path doesn't bypass HTTP auth or app-level rejections."""
    fetcher = _make_fetcher()
    fetcher._do_render = AsyncMock(return_value=_HARD_FAIL())

    unlocker_inst = MagicMock()
    unlocker_inst.fetch = AsyncMock(
        return_value=_result(FetchOutcome.OK, b"<html>unlocked</html>", 200)
    )
    unlocker_cls = MagicMock(return_value=unlocker_inst)

    with (
        patch("ma_poc.fetch.fetcher.ENABLE_TIER_ESCALATION", True),
        patch("ma_poc.fetch.fetcher.ENABLE_UNLOCKER_TIER", True),
        patch(
            "ma_poc.pms.adapters._probe.probe_get",
            return_value=_make_probe_response(401, "Unauthorized"),
        ),
        patch(
            "ma_poc.fetch.providers.unlocker.UnlockerProvider", unlocker_cls
        ),
    ):
        result = await fetcher._do_request(
            _render_task(), _IDENTITY, None, None, None, 1, 0
        )
    assert result.outcome == FetchOutcome.HARD_FAIL
    unlocker_cls.assert_not_called()


def _RATE_LIMITED() -> FetchResult:
    """Playwright/patchright rate-limited by the operator — same TLS
    fingerprint + UA combo getting throttled. curl_cffi's different
    fingerprint typically bypasses the per-fingerprint throttle."""
    return _result(FetchOutcome.RATE_LIMITED, b"", 429)


@pytest.mark.asyncio
async def test_render_rate_limited_falls_back_to_curl_cffi() -> None:
    """The 2026-05-23 Essex fix: 27 properties on
    essexapartmenthomes.com hit RATE_LIMITED via Playwright (same
    UA/IP fingerprint throttled within a canary shard). curl_cffi
    chrome120's different fingerprint bypasses the throttle on the
    same host — validated 3/3 end-to-end (HTML → Funnel API → units)
    on Essex sample 2026-05-23."""
    fetcher = _make_fetcher()
    fetcher._do_render = AsyncMock(return_value=_RATE_LIMITED())
    big_html = "<html>" + ("e" * 5000) + "</html>"
    with (
        patch("ma_poc.fetch.fetcher.ENABLE_TIER_ESCALATION", False),
        patch("ma_poc.fetch.fetcher.ENABLE_UNLOCKER_TIER", False),
        patch(
            "ma_poc.pms.adapters._probe.probe_get",
            return_value=_make_probe_response(200, big_html),
        ),
    ):
        result = await fetcher._do_request(
            _render_task(), _IDENTITY, None, None, None, 1, 0
        )
    assert result.outcome == FetchOutcome.OK
    assert b"e" * 100 in result.body


@pytest.mark.asyncio
async def test_render_rate_limited_curl_cffi_miss_keeps_rate_limited() -> None:
    """If curl_cffi ALSO gets rate-limited (operator throttles by IP
    not fingerprint), the RATE_LIMITED stands. Unlocker is NOT tried —
    hitting the same throttled host via the paid path would only
    deepen the rate-limit and isn't worth the cost."""
    fetcher = _make_fetcher()
    fetcher._do_render = AsyncMock(return_value=_RATE_LIMITED())

    unlocker_inst = MagicMock()
    unlocker_inst.fetch = AsyncMock(
        return_value=_result(FetchOutcome.OK, b"<html>unlocked</html>", 200)
    )
    unlocker_cls = MagicMock(return_value=unlocker_inst)

    with (
        patch("ma_poc.fetch.fetcher.ENABLE_TIER_ESCALATION", True),
        patch("ma_poc.fetch.fetcher.ENABLE_UNLOCKER_TIER", True),
        patch(
            "ma_poc.pms.adapters._probe.probe_get",
            return_value=_make_probe_response(429, ""),
        ),
        patch(
            "ma_poc.fetch.providers.unlocker.UnlockerProvider", unlocker_cls
        ),
    ):
        result = await fetcher._do_request(
            _render_task(), _IDENTITY, None, None, None, 1, 0
        )
    assert result.outcome == FetchOutcome.RATE_LIMITED
    unlocker_cls.assert_not_called()


@pytest.mark.asyncio
async def test_render_bot_blocked_unlocker_still_invoked_when_cffi_misses() -> None:
    """Regression guard for the BOT_BLOCKED path: when curl_cffi misses
    on BOT_BLOCKED (still gets 403), the Unlocker is invoked as before."""
    fetcher = _make_fetcher()
    fetcher._do_render = AsyncMock(return_value=_BLOCKED())

    unlocker_inst = MagicMock()
    unlocker_inst.fetch = AsyncMock(
        return_value=_result(FetchOutcome.OK, b"<html>unlocked</html>", 200)
    )
    unlocker_cls = MagicMock(return_value=unlocker_inst)

    with (
        patch("ma_poc.fetch.fetcher.ENABLE_TIER_ESCALATION", True),
        patch("ma_poc.fetch.fetcher.ENABLE_UNLOCKER_TIER", True),
        patch(
            "ma_poc.pms.adapters._probe.probe_get",
            return_value=_make_probe_response(403, ""),
        ),
        patch(
            "ma_poc.fetch.providers.unlocker.UnlockerProvider", unlocker_cls
        ),
    ):
        result = await fetcher._do_request(
            _render_task(), _IDENTITY, None, None, None, 1, 0
        )
    assert result.outcome == FetchOutcome.OK
    assert b"unlocked" in result.body
    unlocker_cls.assert_called()
