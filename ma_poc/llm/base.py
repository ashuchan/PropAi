"""Abstract base class for LLM providers."""

from __future__ import annotations

import asyncio
import logging
import os
import random
from abc import ABC, abstractmethod
from typing import Any

log = logging.getLogger(__name__)

# Bug-hunt #13: random jitter on retry, NOT fixed backoff.
# Tests override RETRY_WAIT_CAP_SECONDS to 0 so unit tests don't sleep.
MAX_RETRIES = 5
RETRY_WAIT_CAP_SECONDS = 8.0

# Cap timeout-driven retries separately from rate-limit retries. A regional
# OpenRouter outage would otherwise burn 5 × 180s = 15 min on a single
# property. Two attempts is enough to ride out a transient blip without
# blowing the per-property budget.
MAX_TIMEOUT_RETRIES = 2

_DEFAULT_LLM_REQUEST_TIMEOUT_SECONDS = 180.0


def _resolve_request_timeout() -> float:
    """Parse LLM_REQUEST_TIMEOUT_SECONDS env override.

    Falls back to the 180s default with a warning when the env var is set
    but unparseable, so a typo in deployment config can never hard-fail
    container startup.
    """
    raw = os.getenv("LLM_REQUEST_TIMEOUT_SECONDS")
    if raw is None or raw == "":
        return _DEFAULT_LLM_REQUEST_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except (TypeError, ValueError):
        log.warning(
            "LLM_REQUEST_TIMEOUT_SECONDS=%r is not a number; using default %.0fs",
            raw, _DEFAULT_LLM_REQUEST_TIMEOUT_SECONDS,
        )
        return _DEFAULT_LLM_REQUEST_TIMEOUT_SECONDS
    if value <= 0:
        log.warning(
            "LLM_REQUEST_TIMEOUT_SECONDS=%r must be positive; using default %.0fs",
            raw, _DEFAULT_LLM_REQUEST_TIMEOUT_SECONDS,
        )
        return _DEFAULT_LLM_REQUEST_TIMEOUT_SECONDS
    return value


# Per-LLM-call HTTP timeout. Sized off prod data (2026-05-05, n=293):
# p95=161s, p99=292s, max=575s. 180s captures 95.5% of legitimate
# successful calls and cuts the pathological tail (the 9.5-min hang
# that wedged shard 17). Override via LLM_REQUEST_TIMEOUT_SECONDS.
LLM_REQUEST_TIMEOUT_SECONDS = _resolve_request_timeout()


# SDK-specific timeout exception classes. We import lazily and tolerate
# missing libraries / renamed classes — string-name fallback in
# ``_is_timeout_error`` keeps coverage even when an SDK upgrades and
# renames its timeout type. Tested against:
#   openai==1.30.0 (APITimeoutError)
#   anthropic==0.26.0 (APITimeoutError)
#   httpx 0.27.x (ReadTimeout / ConnectTimeout / WriteTimeout / PoolTimeout / TimeoutException)
_TIMEOUT_EXC_CLASSES: tuple[type, ...] = (asyncio.TimeoutError,)
try:  # pragma: no cover — best-effort
    from openai import APITimeoutError as _OpenAITimeout  # type: ignore[import-not-found]
    _TIMEOUT_EXC_CLASSES = (*_TIMEOUT_EXC_CLASSES, _OpenAITimeout)
except Exception:
    pass
try:  # pragma: no cover
    from anthropic import APITimeoutError as _AnthropicTimeout  # type: ignore[import-not-found]
    _TIMEOUT_EXC_CLASSES = (*_TIMEOUT_EXC_CLASSES, _AnthropicTimeout)
except Exception:
    pass
try:  # pragma: no cover
    import httpx as _httpx  # type: ignore[import-not-found]
    _TIMEOUT_EXC_CLASSES = (
        *_TIMEOUT_EXC_CLASSES,
        _httpx.ReadTimeout,
        _httpx.ConnectTimeout,
        _httpx.WriteTimeout,
        _httpx.PoolTimeout,
        _httpx.TimeoutException,
    )
except Exception:
    pass

_TIMEOUT_EXC_NAMES: frozenset[str] = frozenset(
    {
        "APITimeoutError",
        "ReadTimeout",
        "ConnectTimeout",
        "WriteTimeout",
        "PoolTimeout",
        "TimeoutException",
    }
)


class LLMProvider(ABC):
    """Unified interface for text completions and vision extraction."""

    @abstractmethod
    async def _complete_once(
        self,
        system: str,
        user: str,
        max_tokens: int,
    ) -> str:
        """Single attempt at a text completion. Must raise rate-limit errors."""
        ...

    @abstractmethod
    async def _extract_images_once(
        self,
        images: list[bytes],
        prompt: str,
        max_tokens: int,
    ) -> dict[str, Any]:
        """Single attempt at a vision extraction. Must raise rate-limit errors."""
        ...

    @abstractmethod
    def _is_rate_limit_error(self, exc: BaseException) -> bool:
        """Return True if *exc* is a rate-limit / 429 error for this provider."""
        ...

    def _is_timeout_error(self, exc: BaseException) -> bool:
        """Return True if *exc* is a timeout from the underlying HTTP stack.

        Covers httpx (raw) and OpenAI/Anthropic SDK wrappers via real
        ``isinstance`` checks against the imported classes. Falls back to
        a class-name match so a future SDK rename still degrades to
        retrying-as-timeout (preferable to silently grinding on a hang).
        """
        if isinstance(exc, _TIMEOUT_EXC_CLASSES):
            return True
        return type(exc).__name__ in _TIMEOUT_EXC_NAMES

    @property
    @abstractmethod
    def image_size_limit_bytes(self) -> int:
        """Max image payload size in bytes for this provider."""
        ...

    # ------------------------------------------------------------------
    # Public API — retry wrapper (bug-hunt #13)
    # ------------------------------------------------------------------

    async def complete(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int = 4096,
    ) -> str:
        """Text completion with jitter-based retry on rate limits."""
        result: str = await self._with_retry(self._complete_once, system, user, max_tokens)
        return result

    async def extract_from_images(
        self,
        images: list[bytes],
        prompt: str,
        *,
        max_tokens: int = 4096,
    ) -> dict[str, Any]:
        """Vision extraction with jitter-based retry on rate limits."""
        result: dict[str, Any] = await self._with_retry(self._extract_images_once, images, prompt, max_tokens)
        return result

    async def _with_retry(self, fn: Any, *args: Any) -> Any:
        # Retry caps differ by error class: rate-limit (429) gets the full
        # MAX_RETRIES budget because 429s clear quickly. Timeouts get a
        # tighter MAX_TIMEOUT_RETRIES cap — a regional outage would
        # otherwise burn 5 × 180s = 15 min on a single property.
        last_exc: BaseException | None = None
        timeout_attempts = 0
        for attempt in range(MAX_RETRIES):
            try:
                return await fn(*args)
            except Exception as exc:
                is_timeout = self._is_timeout_error(exc)
                is_rate_limit = self._is_rate_limit_error(exc)
                if not (is_rate_limit or is_timeout):
                    raise
                last_exc = exc
                if is_timeout:
                    timeout_attempts += 1
                    if timeout_attempts >= MAX_TIMEOUT_RETRIES:
                        log.warning(
                            "LLM call hit timeout-retry cap (%d) — re-raising %s",
                            MAX_TIMEOUT_RETRIES, type(exc).__name__,
                        )
                        raise
                if attempt + 1 >= MAX_RETRIES:
                    raise
                await asyncio.sleep(random.uniform(0, RETRY_WAIT_CAP_SECONDS))
        assert last_exc is not None
        raise last_exc
