"""Compliance-only rescue for CAPTCHA bodies detected after RENDER."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ma_poc.discovery.contracts import CrawlTask, TaskReason
from ma_poc.fetch import fetcher as fetcher_module
from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode
from ma_poc.fetch.fetcher import Fetcher
from ma_poc.fetch.retry_policy import RetryDecision


class _Identities:
    def pick(self, sticky_key: str | None = None) -> SimpleNamespace:
        return SimpleNamespace(user_agent="fixed-compliance-ua")


class _ProxyPool:
    def pick(self, sticky_key: str | None = None) -> None:
        return None

    def mark_failure(self, proxy: str, outcome: str) -> None:
        return None

    def mark_success(self, proxy: str) -> None:
        return None


class _Robots:
    async def is_allowed(self, url: str, user_agent: str) -> bool:
        return True


class _Limiter:
    async def acquire(self, host: str) -> None:
        return None


class _Cache:
    def read(self, url: str) -> tuple[None, None]:
        return None, None

    def write(self, url: str, etag: object, last_modified: object) -> None:
        return None


class _NoRetry:
    def decide(
        self,
        outcome: FetchOutcome,
        attempt: int,
        retry_after_header: str | None = None,
    ) -> RetryDecision:
        return RetryDecision(False, 0, False)


def _fetcher() -> Fetcher:
    return Fetcher(
        proxy_pool=_ProxyPool(),  # type: ignore[arg-type]
        rate_limiter=_Limiter(),  # type: ignore[arg-type]
        robots=_Robots(),  # type: ignore[arg-type]
        cond_cache=_Cache(),  # type: ignore[arg-type]
        identities=_Identities(),  # type: ignore[arg-type]
        browsers=SimpleNamespace(),  # type: ignore[arg-type]
        retry=_NoRetry(),  # type: ignore[arg-type]
    )


def _task(pid: str) -> CrawlTask:
    return CrawlTask(
        url="https://vanity.example/",
        property_id=pid,
        priority=0,
        budget_ms=30_000,
        reason=TaskReason.MANUAL,
        render_mode=RenderMode.RENDER,
    )


def _result(body: bytes, *, status: int = 202) -> FetchResult:
    return FetchResult(
        url="https://vanity.example/",
        outcome=FetchOutcome.OK,
        status=status,
        body=body,
        headers={"content-type": "text/html"},
        render_mode=RenderMode.RENDER,
        final_url="https://operator.example/.well-known/sgcaptcha/",
        attempts=1,
        elapsed_ms=1,
    )


_SUCURI = b"<html><title>Robot Challenge Screen</title><script>sgchallenge</script></html>"


@pytest.mark.asyncio
async def test_compliance_late_captcha_uses_one_clean_bounded_rescue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COMPLIANCE_MODE", "1")
    monkeypatch.setenv("FETCH_BACKEND", "hyperbrowser")
    monkeypatch.setattr(fetcher_module, "_persist_raw_html", lambda *_args: None)
    fetcher = _fetcher()
    blocked = _result(_SUCURI)
    recovered = _result(b"<html>exact property inventory</html>", status=200)
    rescue = AsyncMock(return_value=recovered)
    monkeypatch.setattr(fetcher, "_do_request", AsyncMock(return_value=blocked))
    monkeypatch.setattr(fetcher, "_try_curl_cffi_fallback", rescue)

    result = await fetcher.fetch(_task("late-captcha-success"))

    assert result is recovered
    rescue.assert_awaited_once()
    assert rescue.await_args.kwargs == {
        "allow_probe_proxy": False,
        "hb_max_calls_per_property": 1,
    }


@pytest.mark.asyncio
async def test_compliance_empty_202_render_uses_same_clean_bounded_rescue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COMPLIANCE_MODE", "1")
    monkeypatch.setenv("FETCH_BACKEND", "hyperbrowser")
    monkeypatch.setattr(fetcher_module, "_persist_raw_html", lambda *_args: None)
    fetcher = _fetcher()
    empty_202 = _result(b"", status=202)
    recovered = _result(b"<html>exact property inventory</html>", status=200)
    rescue = AsyncMock(return_value=recovered)
    monkeypatch.setattr(fetcher, "_do_request", AsyncMock(return_value=empty_202))
    monkeypatch.setattr(fetcher, "_try_curl_cffi_fallback", rescue)

    result = await fetcher.fetch(_task("empty-202-success"))

    assert result is recovered
    rescue.assert_awaited_once()
    assert rescue.await_args.kwargs == {
        "allow_probe_proxy": False,
        "hb_max_calls_per_property": 1,
    }


def test_compliance_hyperbrowser_options_hard_disable_solver_and_stealth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ma_poc.fetch.hyperbrowser_backend import _session_options

    monkeypatch.setenv("COMPLIANCE_MODE", "1")
    monkeypatch.setenv("HB_USE_STEALTH", "1")
    options = _session_options("render")

    assert options["solveCaptchas"] is False
    assert options["useStealth"] is False


@pytest.mark.asyncio
async def test_non_captcha_render_is_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COMPLIANCE_MODE", "1")
    monkeypatch.setenv("FETCH_BACKEND", "hyperbrowser")
    monkeypatch.setattr(fetcher_module, "_persist_raw_html", lambda *_args: None)
    fetcher = _fetcher()
    ordinary = _result(b"<html>ordinary property page</html>", status=200)
    rescue = AsyncMock()
    monkeypatch.setattr(fetcher, "_do_request", AsyncMock(return_value=ordinary))
    monkeypatch.setattr(fetcher, "_try_curl_cffi_fallback", rescue)

    result = await fetcher.fetch(_task("late-captcha-control"))

    assert result is ordinary
    rescue.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_200_render_does_not_enter_precise_202_rescue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COMPLIANCE_MODE", "1")
    monkeypatch.setenv("FETCH_BACKEND", "hyperbrowser")
    fetcher = _fetcher()
    empty_200 = _result(b"", status=200)
    rescue = AsyncMock()
    monkeypatch.setattr(fetcher, "_do_request", AsyncMock(return_value=empty_200))
    monkeypatch.setattr(fetcher, "_try_curl_cffi_fallback", rescue)

    result = await fetcher.fetch(_task("empty-200-control"))

    assert result is empty_200
    rescue.assert_not_awaited()


@pytest.mark.asyncio
async def test_late_captcha_rescue_failure_remains_bot_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COMPLIANCE_MODE", "1")
    monkeypatch.setenv("FETCH_BACKEND", "hyperbrowser")
    fetcher = _fetcher()
    monkeypatch.setattr(fetcher, "_do_request", AsyncMock(return_value=_result(_SUCURI)))
    rescue = AsyncMock(return_value=None)
    monkeypatch.setattr(fetcher, "_try_curl_cffi_fallback", rescue)

    result = await fetcher.fetch(_task("late-captcha-failure"))

    assert result.outcome == FetchOutcome.BOT_BLOCKED
    assert result.captcha_detected is True
    rescue.assert_awaited_once()


@pytest.mark.asyncio
async def test_clean_helper_stops_after_direct_then_one_hyperbrowser_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, object]] = []

    class _ProbeResponse:
        status_code = 202
        text = _SUCURI.decode()
        url = "https://operator.example/.well-known/sgcaptcha/"

    def _probe_get(url: str, **kwargs: object) -> _ProbeResponse:
        calls.append(("direct", kwargs.get("proxies")))
        return _ProbeResponse()

    async def _hb_raw_get(
        url: str,
        property_id: str,
        **kwargs: object,
    ) -> tuple[int, str]:
        calls.append(("hb", kwargs.get("max_calls_per_property")))
        return 200, "<html>" + ("exact inventory " * 100) + "</html>"

    monkeypatch.setattr("ma_poc.pms.adapters._probe.probe_get", _probe_get)
    monkeypatch.setattr(
        "ma_poc.fetch.hyperbrowser_backend.hb_raw_get",
        _hb_raw_get,
    )
    monkeypatch.setattr("ma_poc.config.feature_flags.hb_enabled", lambda: True)
    monkeypatch.setenv("PROBE_PROXY_URL", "http://must-not-be-used.invalid:22225")

    result = await _fetcher()._try_curl_cffi_fallback(
        _task("clean-helper"),
        0,
        allow_probe_proxy=False,
        hb_max_calls_per_property=1,
    )

    assert result is not None and result.outcome == FetchOutcome.OK
    assert calls == [("direct", {}), ("hb", 1)]
