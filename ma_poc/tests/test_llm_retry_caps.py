"""Tests for the dual-cap retry behavior in llm.base.LLMProvider._with_retry.

Rate-limit (429) errors get the full MAX_RETRIES budget; timeouts get the
tighter MAX_TIMEOUT_RETRIES cap so a regional outage cannot burn
5 × LLM_REQUEST_TIMEOUT_SECONDS on a single property.
"""

from __future__ import annotations

from typing import Any

import pytest

from llm import base as llm_base


class _StubProvider(llm_base.LLMProvider):
    """Minimal concrete provider whose _complete_once raises a configurable exception.

    Lets us assert exactly how many times the retry loop calls the underlying
    completion before giving up.
    """

    def __init__(self, exc_to_raise: Exception, *, treat_as_timeout: bool, treat_as_rate_limit: bool) -> None:
        self._exc = exc_to_raise
        self._treat_as_timeout = treat_as_timeout
        self._treat_as_rate_limit = treat_as_rate_limit
        self.call_count = 0

    async def _complete_once(self, system: str, user: str, max_tokens: int) -> str:
        self.call_count += 1
        raise self._exc

    async def _extract_images_once(self, images: list[bytes], prompt: str, max_tokens: int) -> dict[str, Any]:
        raise NotImplementedError

    def _is_rate_limit_error(self, exc: BaseException) -> bool:
        return self._treat_as_rate_limit

    def _is_timeout_error(self, exc: BaseException) -> bool:
        return self._treat_as_timeout

    @property
    def image_size_limit_bytes(self) -> int:
        return 0


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    # Don't actually sleep between retries during tests.
    monkeypatch.setattr(llm_base, "RETRY_WAIT_CAP_SECONDS", 0.0)


@pytest.mark.asyncio
async def test_timeout_retry_caps_at_max_timeout_retries() -> None:
    """A persistent timeout must give up at MAX_TIMEOUT_RETRIES, not MAX_RETRIES.

    With MAX_RETRIES=5 and MAX_TIMEOUT_RETRIES=2, a stuck OpenRouter would
    otherwise burn 5 × 180s = 15 min; the cap brings that to 2 × 180s.
    """
    provider = _StubProvider(
        TimeoutError("fake timeout"),
        treat_as_timeout=True,
        treat_as_rate_limit=False,
    )
    with pytest.raises(TimeoutError):
        await provider.complete("sys", "user")
    assert provider.call_count == llm_base.MAX_TIMEOUT_RETRIES, (
        f"expected {llm_base.MAX_TIMEOUT_RETRIES} attempts (MAX_TIMEOUT_RETRIES), "
        f"got {provider.call_count}"
    )


@pytest.mark.asyncio
async def test_rate_limit_retry_uses_full_max_retries_budget() -> None:
    """429s clear quickly — keep the full retry budget."""
    provider = _StubProvider(
        RuntimeError("429 too many requests"),
        treat_as_timeout=False,
        treat_as_rate_limit=True,
    )
    with pytest.raises(RuntimeError):
        await provider.complete("sys", "user")
    assert provider.call_count == llm_base.MAX_RETRIES


@pytest.mark.asyncio
async def test_non_retryable_error_propagates_on_first_attempt() -> None:
    """Anything other than rate-limit / timeout must NOT be retried."""
    provider = _StubProvider(
        ValueError("malformed prompt"),
        treat_as_timeout=False,
        treat_as_rate_limit=False,
    )
    with pytest.raises(ValueError):
        await provider.complete("sys", "user")
    assert provider.call_count == 1


# ── _resolve_request_timeout env-var parsing ────────────────────────────────


def test_request_timeout_default_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLM_REQUEST_TIMEOUT_SECONDS", raising=False)
    assert llm_base._resolve_request_timeout() == 180.0


def test_request_timeout_uses_env_when_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_REQUEST_TIMEOUT_SECONDS", "240")
    assert llm_base._resolve_request_timeout() == 240.0


def test_request_timeout_falls_back_on_garbage(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """A typo in deployment config (e.g. ``LLM_REQUEST_TIMEOUT_SECONDS=2m``)
    must not hard-fail container startup."""
    monkeypatch.setenv("LLM_REQUEST_TIMEOUT_SECONDS", "2m")
    assert llm_base._resolve_request_timeout() == 180.0


def test_request_timeout_rejects_non_positive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_REQUEST_TIMEOUT_SECONDS", "0")
    assert llm_base._resolve_request_timeout() == 180.0
    monkeypatch.setenv("LLM_REQUEST_TIMEOUT_SECONDS", "-30")
    assert llm_base._resolve_request_timeout() == 180.0
